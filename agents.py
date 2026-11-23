"""Background agents on the kiosk, from the Claude Code harness itself.

"Running now" and "Just finished" could be lines the assistant types into the notes file, but it forgets or types
them late. So a hook script (scripts/agents-hook.py, a thin wrapper around `hook_main` here) records every agent
the Claude Code harness starts and stops, and the server reads that record. Nothing here reads a transcript's
content: the writer reads the small `<session>/subagents/agent-<id>.meta.json` the harness writes at spawn (its
description, type, spawn depth), the server reads the agent transcript's mtime only (is it still working?).

Hook events used (Claude Code hooks reference):
  SubagentStart   {agent_id, agent_type, transcript_path, session_id}: an agent started, or was resumed
  SubagentStop    {agent_id, agent_type, agent_transcript_path, ...}: an agent finished responding
  PostToolUse     tool_name "Agent": tool_input {description, run_in_background, ...}; tool_response {status:
                  "async_launched" (background, returned at once) or "completed" (foreground, at the end), agentId,
                  totalDurationMs}. A backup for the description and for the finish of a foreground agent.
SubagentStop also fires for Claude Code's own internal agents (prompt suggestions, /btw); those have no meta file and
no Agent tool call, so they are never recorded.

State file: settings `agents.state_file` (default <server.state_dir>/agents.json), {"v": 1, "agents": {id: entry}}, written under an flock with a
temp file and an atomic rename (several hooks can fire at once). Entry: id, desc (cut to 200 characters, control
characters removed), type, depth (1 = launched by the main session), shape ("background"/"foreground"/""),
state ("running"/"finished"), started, finished (epoch seconds or null), runs (1 + resumes), seen (last event),
tpath (the agent transcript path, only when it is under ~/.claude/projects). Entries older than 24 h and anything
past 200 entries (oldest first) are pruned on each write.

Rules the hook keeps: a resumed agent (SubagentStart for a known id) is running again with a new start time and runs
+1; its next stop finishes it again (one entry, the newest times). A second stop with no start in between changes
nothing (the first finish time stays). The hook never blocks or fails a tool call: any error is one line on stderr
and exit 0; a lock it cannot get within a second is given up the same way.

The server's view (`view`) is for WORK only: an entry shows only when its
description names a work ticket id (a prefix that is one of the Linear team keys the task list fetched, or one of
settings `agents.fallback_prefixes` when Linear has not answered) or carries the tag "[work]" (the assistant adds it to
the Agent description when work has no ticket). Agents started by other agents (depth 2+) never show: they are part of their parent's job.
Running: "no word since HH:MM" instead of an age once the agent's transcript has not been written for QUIET_S (or,
without a transcript, the start is older than QUIET_NO_FILE_S); dropped after EXPIRE_S of silence (a session that
crashed). Finished: within FINISHED_S, newest first.
"""
import fcntl
import json
import os
import re
import sys
import tempfile
import time

import config  # noqa: E402

_A = config.get()
STATE = os.path.expanduser(_A["agents"]["state_file"]) if _A["agents"]["state_file"] else config.state_path("agents.json")
STATE_DIR = os.path.dirname(STATE)
PROJECTS = os.path.expanduser(_A["session"]["projects_dir"])
DESC_MAX = 200
KEEP_S = 24 * 3600               # pruned from the file after this long without an event
MAX_ENTRIES = 200
LOCK_WAIT_S = 1.0
META_MAX = 64 * 1024
INPUT_MAX = 4 * 1024 * 1024      # the hook's stdin (a PostToolUse carries the prompt): read at most this much

QUIET_S = 45 * 60                # running, transcript untouched this long -> "no word since"
QUIET_NO_FILE_S = 4 * 3600       # running, no transcript to look at, started this long ago -> "no word since"
EXPIRE_S = 12 * 3600             # running, silent this long -> not shown at all (the session is gone)
FINISHED_S = 18 * 3600           # the same 18 h as a notes block
FALLBACK_PREFIXES = frozenset(p.upper() for p in _A["agents"]["fallback_prefixes"])

_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁦-⁩]")
_ID_OK = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_WORK_TAG = re.compile(r"\[\s*work\s*\]", re.I)
_TICKET = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9]*)-(\d+)(?![A-Za-z0-9_])")


# --- the writer (the hook) -------------------------------------------------------------------------------------------
def norm_id(raw):
    """The harness's agent id ("a702f71c89ec8b7df"; the docs also show "agent-abc123") -> the bare id, or None."""
    if not isinstance(raw, str):
        return None
    raw = raw.strip()
    if raw.startswith("agent-"):
        raw = raw[len("agent-"):]
    return raw if _ID_OK.match(raw) else None


def clean_desc(text):
    if not isinstance(text, str):
        return ""
    text = re.sub(r"\s+", " ", _CTRL.sub(" ", text)).strip()
    return text[:DESC_MAX]


def _under_projects(path):
    if not isinstance(path, str) or not path:
        return None
    real = os.path.realpath(os.path.expanduser(path))
    return real if real.startswith(os.path.realpath(PROJECTS) + os.sep) else None


def agent_files(transcript_path, agent_id):
    """(meta path, agent transcript path) beside the main session's transcript, or (None, None)."""
    main = _under_projects(transcript_path)
    if not main or not main.endswith(".jsonl"):
        return None, None
    base = os.path.join(main[:-len(".jsonl")], "subagents", "agent-" + agent_id)
    return base + ".meta.json", base + ".jsonl"


def read_meta(path):
    """The harness's small meta file for an agent -> {"desc", "type", "depth", "shape"} or {}."""
    try:
        if not path or os.path.getsize(path) > META_MAX:
            return {}
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(m, dict):
        return {}
    depth = m.get("spawnDepth")
    shape = m.get("requestShape")
    return {"desc": clean_desc(m.get("description")),
            "type": clean_desc(m.get("agentType"))[:60],
            "depth": depth if isinstance(depth, int) and not isinstance(depth, bool) and 0 < depth < 50 else None,
            "shape": shape if shape in ("background", "foreground") else ""}


def _new(aid, now):
    return {"id": aid, "desc": "", "type": "", "depth": None, "shape": "", "state": "running", "started": now,
            "finished": None, "runs": 1, "seen": now, "tpath": None}


def _fill(entry, meta, tpath=None):
    for k in ("desc", "type", "shape"):
        if meta.get(k) and not entry.get(k):
            entry[k] = meta[k]
    if meta.get("depth") and not entry.get("depth"):
        entry["depth"] = meta["depth"]
    if tpath and not entry.get("tpath"):
        entry["tpath"] = tpath


def apply_event(state, ev, now):
    """One hook input (a dict) -> the state changed in place. Returns True when something changed."""
    agents = state.setdefault("agents", {})
    name = ev.get("hook_event_name")
    if name == "SubagentStart":
        aid = norm_id(ev.get("agent_id"))
        if not aid:
            return False
        meta_p, tpath = agent_files(ev.get("transcript_path"), aid)
        e = agents.get(aid)
        if e is None:
            e = agents[aid] = _new(aid, now)
        elif e["state"] == "finished":                 # resumed: running again
            e.update(state="running", started=now, finished=None, runs=int(e.get("runs") or 1) + 1)
        if not e.get("type"):
            e["type"] = clean_desc(ev.get("agent_type"))[:60]
        _fill(e, read_meta(meta_p), tpath)
        e["seen"] = now
        return True
    if name == "SubagentStop":
        aid = norm_id(ev.get("agent_id"))
        if not aid:
            return False
        e = agents.get(aid)
        if e is None:
            # not seen starting (the hook was installed mid-run): record it only when the harness has a meta file
            # for it, i.e. it came from the Agent tool, never one of Claude Code's internal agents
            meta_p, tpath = agent_files(ev.get("transcript_path"), aid)
            meta = read_meta(meta_p)
            if not meta:
                return False
            e = agents[aid] = _new(aid, now)
            e["started"] = None
            _fill(e, meta, tpath)
        if e["state"] != "finished":
            e.update(state="finished", finished=now)
        e["seen"] = now
        return True
    if name == "PostToolUse" and ev.get("tool_name") == "Agent":
        resp = ev.get("tool_response")
        tin = ev.get("tool_input") if isinstance(ev.get("tool_input"), dict) else {}
        if not isinstance(resp, dict):
            return False
        aid = norm_id(resp.get("agentId"))
        if not aid:
            return False
        status = resp.get("status")
        meta_p, tpath = agent_files(ev.get("transcript_path"), aid)
        e = agents.get(aid)
        created = e is None
        if created:
            e = agents[aid] = _new(aid, now)
        desc = clean_desc(tin.get("description") or resp.get("description"))
        if desc and not e.get("desc"):
            e["desc"] = desc
        if not e.get("type"):
            e["type"] = clean_desc(tin.get("subagent_type"))[:60]
        if ev.get("agent_id") and not e.get("depth"):
            e["depth"] = 2                             # launched from inside a subagent
        _fill(e, read_meta(meta_p), tpath)
        if status == "async_launched":
            e["shape"] = e.get("shape") or "background"
        elif status == "completed":
            e["shape"] = e.get("shape") or "foreground"
            if created:
                ms = resp.get("totalDurationMs")
                if isinstance(ms, (int, float)) and not isinstance(ms, bool) and 0 <= ms < 7 * 86400 * 1000:
                    e["started"] = now - int(ms / 1000)
            if e["state"] != "finished":
                e.update(state="finished", finished=now)
        e["seen"] = now
        return True
    return False


def prune(state, now):
    agents = state.get("agents") or {}
    for aid in [a for a, e in agents.items() if now - (e.get("seen") or 0) > KEEP_S]:
        del agents[aid]
    if len(agents) > MAX_ENTRIES:
        for aid in sorted(agents, key=lambda a: agents[a].get("seen") or 0)[:len(agents) - MAX_ENTRIES]:
            del agents[aid]


def load(path=STATE):
    """The state file -> {"v": 1, "agents": {...}} (empty on a missing or bad file). Only well-formed entries."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return {"v": 1, "agents": {}}
    out = {}
    for aid, e in ((doc or {}).get("agents") or {}).items() if isinstance(doc, dict) else ():
        if isinstance(e, dict) and norm_id(aid) == aid and e.get("state") in ("running", "finished"):
            out[aid] = e
    return {"v": 1, "agents": out}


def save(state, path=STATE):
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".agents-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, separators=(",", ":"))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record(ev, path=STATE, now=None):
    """Apply one hook input to the state file under an exclusive lock. Raises on lock timeout or I/O errors."""
    now = int(now if now is not None else time.time())
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    with open(path + ".lock", "a") as lk:
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError("state lock busy")
                time.sleep(0.01)
        try:
            state = load(path)
            if apply_event(state, ev, now):
                prune(state, now)
                save(state, path)
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)


def hook_main(stdin=None, path=None):
    """The hook: read one JSON input, record it, exit 0 whatever happens (one stderr line on an error)."""
    try:
        raw = (stdin or sys.stdin).read(INPUT_MAX + 1)
        if len(raw) > INPUT_MAX:
            raise ValueError("input too large")
        ev = json.loads(raw)
        if not isinstance(ev, dict):
            raise ValueError("input is not an object")
        if ev.get("hook_event_name") not in ("SubagentStart", "SubagentStop", "PostToolUse"):
            return 0
        record(ev, path or STATE)
    except Exception as e:                                  # never block or fail the tool call
        print("kiosk-agents-hook: %s" % type(e).__name__, file=sys.stderr)
    return 0


# --- the reader (the server) -----------------------------------------------------------------------------------------
def work_ids(text, prefixes):
    """Ticket ids in the text whose prefix is a work prefix, upper-cased."""
    return {m.group(1).upper() + "-" + m.group(2) for m in _TICKET.finditer(text or "")
            if m.group(1).upper() in prefixes}


def is_work(desc, prefixes):
    return bool(_WORK_TAG.search(desc or "")) or bool(work_ids(desc, prefixes))


def shown_text(desc):
    """The description as shown: the [work] tag removed."""
    return re.sub(r"\s+", " ", _WORK_TAG.sub(" ", desc or "")).strip(" -:·")


def last_word(e, stat=os.stat):
    """When the agent was last heard from: its transcript's mtime (when known and under ~/.claude/projects), else
    its last hook event. (epoch, from_file)."""
    seen = e.get("seen") or e.get("started") or 0
    p = _under_projects(e.get("tpath"))
    if p:
        try:
            return max(seen, int(stat(p).st_mtime)), True
        except OSError:
            pass
    return seen, False


def view(state, now, prefixes=None, stat=os.stat):
    """The state -> {"running": [...], "finished": [...], "hidden": n not work} for the kiosk (work only, depth 1).
    running item: {"text", "ts" (start), "id", "quiet_since" (epoch, only when quiet), "source": "harness"};
    finished item: {"text", "ts" (finish), "id", "source": "harness"}, newest first. Never raises on odd entries."""
    prefixes = frozenset(prefixes) if prefixes else FALLBACK_PREFIXES
    running, finished, hidden = [], [], 0
    for e in (state.get("agents") or {}).values():
        try:
            if (e.get("depth") or 1) > 1:
                continue
            desc = e.get("desc") or ""
            if not is_work(desc, prefixes):
                hidden += 1
                continue
            text = shown_text(desc)
            if not text:
                continue
            if e["state"] == "running":
                word, from_file = last_word(e, stat)
                silent = now - word
                if silent > EXPIRE_S:
                    continue
                item = {"text": text, "ts": e.get("started"), "id": e["id"], "source": "harness",
                        "ids": sorted(work_ids(desc, prefixes))}
                if silent > (QUIET_S if from_file else QUIET_NO_FILE_S):
                    item["quiet_since"] = int(word)
                running.append(item)
            elif e.get("finished") and now - e["finished"] <= FINISHED_S:
                finished.append({"text": text, "ts": e["finished"], "id": e["id"], "source": "harness",
                                 "ids": sorted(work_ids(desc, prefixes))})
        except (KeyError, TypeError, AttributeError):
            continue
    running.sort(key=lambda x: -(x.get("ts") or 0))
    finished.sort(key=lambda x: -(x.get("ts") or 0))
    return {"running": running, "finished": finished, "hidden": hidden}


class Reader:
    """Caches the state file by (mtime, size). `current()` never raises."""

    def __init__(self, path=STATE):
        self.path = path
        self._key, self._state = None, {"v": 1, "agents": {}}

    def current(self):
        try:
            st = os.stat(self.path)
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            self._key, self._state = None, {"v": 1, "agents": {}}
            return self._state
        if key != self._key:
            self._state, self._key = load(self.path), key
        return self._state


if __name__ == "__main__":
    sys.exit(hook_main())
