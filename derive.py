"""A work task's state line, derived from the systems that know.

A hand-written state in the notes file's WORK TASKS block goes stale quietly: it can sit unchanged for a day while
the ticket goes through a merge and an apply. So for a task that has a Linear ticket the line is built from, in this
order of trust:

  1. its live Linear state ("In Progress", "In Review"), from the task list's own Linear fetch (tasks.Linear);
  2. its pull requests (`TicketPRs`): the owner's PRs in the configured orgs (settings `github`) updated in the last 3 days, matched to the
     ticket by the id in the PR BODY ("Refs OPS-577"; also Closes/Fixes/Resolves/Part of; when the
     body has none of those words, any id in the body) or in the BRANCH name ("work/ops-577-apply-on-merge").
     Shown: the open non-draft PR first ("#30 checks passing|failing|running|no checks", judged by the newest-run
     rule of checks.py), else an open draft ("draft #31 ..."), else the newest PR merged in the last 48 h
     ("#28 merged 11:30", "merged Nov 19"); "+N PRs"
     when there are more open or just-merged ones. Closed-unmerged PRs are ignored; a merged PR's checks are not
     shown (the merge already happened).
  3. the newest In flight "finished" line that names the ticket (the hook's own record of a background agent
     finishing, or a hand F line): "11:32 <text>".
  4. the hand-written state text, ONLY while its block is fresh (HAND_FRESH_S, 4 h: half a work day, so a morning
     list lasts until about noon and an after-lunch list until the day ends). It comes last, after the facts, so
     even a fresh note never hides them.
A task with no ticket (the line does not start with an id) keeps its hand text exactly as before. A ticket task with
nothing derivable (Linear has not answered for that workspace and no PR or line names it) shows its hand text with
its age when the note is older than HAND_FRESH_S: "as of Nov 19 11:54: <text>".

Only what a source states is said: nothing is inferred ("applied", "no changes" never come from here).

`TicketPRs` runs one read-only `gh api graphql` (the gh CLI's own login; this code never reads a token) in a
background thread at most every 5 minutes, with a 30 s timeout; the last good result is used for an hour (then
"off"); at most one journal line an hour (error type only). Bodies are read only to find ticket ids and are never
kept or shown. Never raises.
"""
import json
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import checks
import config
import tasks

TZ = tasks.TZ
HAND_FRESH_S = 4 * 3600
MERGED_WINDOW_S = 48 * 3600
LOOKBACK_DAYS = 3
GH = config.get()["github"]["gh"]
ORGS = tuple(config.get()["github"]["orgs"])
AUTHOR = config.get()["github"]["author"]
FETCH_EVERY_S = 300
GOOD_FOR_S = 24 * 3600           # the last good result (memory, and on disk across restarts), shown with its age
LOG_EVERY_S = 3600
GH_TIMEOUT_S = 30
MAX_OUT = 16 * 1024 * 1024        # the check runs of up to 100 PRs (checks.ROLLUP)
BODY_SCAN = 20000                # characters of a PR body scanned for ids
_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_OWNER_REPO = re.compile(r"^(?:%s)/[A-Za-z0-9_.-]{1,100}$" % "|".join(re.escape(o) for o in ORGS))
_REFS = re.compile(r"\b(?:refs?|closes?|closed|fix(?:es|ed)?|resolves?|resolved|part\s+of)\b\s*:?\s*"
                   r"((?:[A-Za-z][A-Za-z0-9]*-\d+(?:\s*(?:,|&|/|\band\b)\s*)?)+)", re.I)
_BRANCH_ID = re.compile(r"(?<![A-Za-z0-9])([A-Za-z][A-Za-z0-9]*)-(\d+)(?![0-9])")


def query(today):
    since = (today - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    fields = ("... on PullRequest { number state isDraft mergedAt headRefName body "
              "repository { name nameWithOwner } commits(last: 1) { nodes { commit { " + checks.ROLLUP + " } } } }")
    return "query { " + " ".join(
        '%s: search(query: "is:pr author:%s org:%s updated:>=%s", type: ISSUE, first: 50) { nodes { %s } }'
        % ("t%d" % i, AUTHOR, org, since, fields) for i, org in enumerate(ORGS)) + " }"


def body_ids(body):
    """Ticket ids a PR body names: those after Refs/Closes/Fixes/Resolves/Part of; when there are none, every id."""
    body = (body or "")[:BODY_SCAN]
    refs = set()
    for m in _REFS.finditer(body):
        refs |= tasks.ticket_ids(m.group(1))
    return refs or tasks.ticket_ids(body)


def branch_ids(branch):
    return {m.group(1).upper() + "-" + m.group(2) for m in _BRANCH_ID.finditer(branch or "")}


def _epoch(iso):
    try:
        return int(datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp())
    except (TypeError, ValueError):
        return None


def parse(doc, closed=None):
    """gh's JSON -> [{"repo", "owner_repo", "number", "state": "open"|"merged", "draft", "ci", "merged_at", "ids"}].
    Raises on a bad shape; drops malformed nodes, foreign orgs and closed-unmerged PRs. When `closed` is a list, the
    closed-unmerged ones are appended to it as {"owner_repo", "repo", "number", "state": "closed", "ids"} (the
    automatic waiting line must know a ticket has one; they never reach a state line)."""
    if not isinstance(doc, dict) or doc.get("errors") or not isinstance(doc.get("data"), dict):
        raise ValueError("graphql errors")
    out, seen = [], set()
    for i in range(len(ORGS)):
        for node in doc["data"]["t%d" % i]["nodes"]:
            try:
                repo, full, num = node["repository"]["name"], node["repository"]["nameWithOwner"], node["number"]
                if (not isinstance(num, int) or isinstance(num, bool) or not isinstance(repo, str)
                        or not _REPO.match(repo) or not isinstance(full, str) or not _OWNER_REPO.match(full)
                        or (full, num) in seen):
                    continue
                state = {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}.get(node.get("state"))
                if not state:
                    continue
                seen.add((full, num))
                ids = body_ids(node.get("body") if isinstance(node.get("body"), str) else "")
                ids |= branch_ids(node.get("headRefName") if isinstance(node.get("headRefName"), str) else "")
                if state == "closed":
                    if closed is not None:
                        closed.append({"owner_repo": full.lower(), "repo": repo, "number": num, "state": "closed",
                                       "ids": sorted(ids)})
                    continue
                out.append({"repo": repo, "owner_repo": full.lower(), "number": num, "state": state,
                            "draft": node.get("isDraft") is True,
                            "ci": checks.judge_commits(node.get("commits")), "merged_at": _epoch(node.get("mergedAt")),
                            "ids": sorted(ids)})
            except (KeyError, TypeError, AttributeError):
                continue
    return out


# --- the state of PRs a Linear issue links that the search above did not return (older than its 3 days) -------------
# For the automatic "Waiting on you" line (inflight.auto_waiting): a ticket's PRs can merge well before anyone notices
# it is left In Review. One more read-only `gh api graphql` in the same background refresh, only when there are such
# PRs, at most LOOKUP_MAX, only under the configured orgs. A repository gh cannot resolve leaves that PR unknown
# (the others still answer); an unknown PR means no automatic line for its ticket.
LOOKUP_MAX = 40
_REF = re.compile(r"^(%s)/([a-z0-9_.-]{1,100})#(\d{1,7})$" % "|".join(re.escape(o) for o in ORGS))
PR_STATE = {"OPEN": "open", "MERGED": "merged", "CLOSED": "closed"}


def lookup_query(refs):
    """["acme-inc/acme#12", ...] (already checked with _REF) -> one GraphQL query, one alias per PR."""
    parts = []
    for i, ref in enumerate(refs):
        m = _REF.match(ref)
        parts.append('l%d: repository(owner: "%s", name: "%s") { pullRequest(number: %d) { state } }'
                     % (i, m.group(1), m.group(2), int(m.group(3))))
    return "query { " + " ".join(parts) + " }"


def parse_lookup(doc, refs):
    """gh's JSON (errors allowed: a missing repository is just null) -> {ref: "open"|"merged"|"closed"} for the PRs
    that answered with a known state."""
    data = doc.get("data") if isinstance(doc, dict) else None
    if not isinstance(data, dict):
        raise ValueError("no data")
    out = {}
    for i, ref in enumerate(refs):
        node = data.get("l%d" % i)
        pull = node.get("pullRequest") if isinstance(node, dict) else None
        state = PR_STATE.get(pull.get("state")) if isinstance(pull, dict) else None
        if state:
            out[ref] = state
    return out


def gh_run(args, timeout):
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout


class TicketPRs:
    """The owner's recent PRs with the ticket ids they name, cached in memory (same pattern as inflight.GitHub)."""

    def __init__(self, run=gh_run, clock=time.monotonic, wall=time.time, log=None, cache_file=None, wanted=None):
        self.run, self.clock, self.wall = run, clock, wall
        self.cache_file = cache_file    # the last good result survives a restart (diskcache.py); None: memory only
        # a callable -> the "owner/repo#n" refs whose state the automatic waiting line needs (inflight.wanted_refs)
        self.wanted = wanted
        self._looked = {}               # ref -> "open"|"merged"|"closed" (the last answer for each), kept with _good
        self.log = log or (lambda line: print(line, file=sys.stderr, flush=True))
        self._lock = threading.Lock()
        self._running = False
        self._attempt = None
        self._logged = None
        self._good = None
        self._ok = None
        self._load_saved()

    def _load_saved(self):
        """The last good result an earlier run saved, with its real age (shown "stale" until a fetch answers)."""
        if not self.cache_file:
            return
        import diskcache
        doc = diskcache.load(self.cache_file) or {}
        try:
            wall, prs = float(doc["wall"]), [p for p in doc["prs"] if isinstance(p, dict)]
        except (KeyError, TypeError, ValueError):
            return
        age = self.wall() - wall
        if 0 <= age < GOOD_FOR_S:
            # an older saved copy may have neither "closed" nor "looked": no automatic line until a fetch
            closed = doc.get("closed")
            looked = doc.get("looked")
            self._good = {"at": self.clock() - age, "wall": wall, "prs": prs,
                          "closed": [p for p in closed if isinstance(p, dict)] if isinstance(closed, list) else None}
            if isinstance(looked, dict):
                self._looked = {k: v for k, v in looked.items()
                                if isinstance(k, str) and _REF.match(k) and v in PR_STATE.values()}

    def _save(self):
        if self.cache_file and self._good:
            import diskcache
            diskcache.save(self.cache_file, {"v": 1, "wall": self._good["wall"], "prs": self._good["prs"],
                                             "closed": self._good.get("closed"), "looked": dict(self._looked)})

    def _note(self, line):
        now = self.clock()
        if self._logged is None or now - self._logged >= LOG_EVERY_S:
            self._logged = now
            self.log(line)

    def refresh(self):
        with self._lock:
            self._attempt = self.clock()
        try:
            today = datetime.fromtimestamp(self.wall(), timezone.utc)
            code, out = self.run([GH, "api", "graphql", "-f", "query=" + query(today)], GH_TIMEOUT_S)
            if code != 0:
                raise RuntimeError("exit %s" % code if isinstance(code, int) else "exit")
            if not isinstance(out, str) or len(out) > MAX_OUT:
                raise ValueError("bad output")
            closed = []
            prs = parse(json.loads(out), closed)
            with self._lock:
                self._good = {"at": self.clock(), "wall": self.wall(), "prs": prs, "closed": closed}
                self._ok = True
            self._lookup(prs + closed)
            self._save()
        except Exception as e:
            with self._lock:
                self._ok = False
            detail = str(e) if isinstance(e, RuntimeError) and re.fullmatch(r"exit -?\d+", str(e)) else ""
            self._note("tasks: github ticket PRs fetch failed (%s%s)" % (type(e).__name__,
                                                                         " " + detail if detail else ""))

    def _lookup(self, found):
        """The state of each wanted PR the search did not return, in one more query. Never raises; a failure keeps
        the earlier answers and leaves the rest unknown."""
        if self.wanted is None:
            return
        try:
            have = {"%s#%d" % (p.get("owner_repo"), p["number"]) for p in found if p.get("owner_repo")}
            refs = sorted({r for r in (self.wanted() or ()) if isinstance(r, str) and _REF.match(r)} - have)
            refs = refs[:LOOKUP_MAX]
            if not refs:
                return
            code, out = self.run([GH, "api", "graphql", "-f", "query=" + lookup_query(refs)], GH_TIMEOUT_S)
            if not isinstance(out, str) or not out.strip() or len(out) > MAX_OUT:
                raise RuntimeError("exit %s" % code if isinstance(code, int) else "exit")
            got = parse_lookup(json.loads(out), refs)     # gh exits 1 when one repository is missing; the rest count
            with self._lock:
                self._looked.update(got)
        except Exception as e:
            detail = str(e) if isinstance(e, RuntimeError) and re.fullmatch(r"exit -?\d+", str(e)) else ""
            self._note("tasks: github linked PRs lookup failed (%s%s)" % (type(e).__name__,
                                                                          " " + detail if detail else ""))

    def maybe_refresh(self):
        with self._lock:
            if self._running or (self._attempt is not None and self.clock() - self._attempt < FETCH_EVERY_S):
                return
            self._running = True

        def go():
            try:
                self.refresh()
            finally:
                with self._lock:
                    self._running = False
        threading.Thread(target=go, name="ticket-prs-fetch", daemon=True).start()

    def snapshot(self):
        """{"prs", "status": "ok"|"stale"|"off"|"pending", "at": wall clock of the good result or None,
        "closed": the closed-unmerged PRs of the same search (None when not known), "looked": {ref: state} of the
        linked PRs looked up by hand}."""
        now = self.clock()
        with self._lock:
            good = self._good if self._good and now - self._good["at"] < GOOD_FOR_S else None
            status = ("ok" if good and self._ok else "stale" if good else "pending" if self._ok is None else "off")
            closed = good.get("closed") if good else None
            return {"prs": [dict(p) for p in good["prs"]] if good else [], "status": status,
                    "at": int(good["wall"]) if good else None,
                    "closed": [dict(p) for p in closed] if closed is not None else None,
                    "looked": dict(self._looked) if good else {}}


# --- composing the line -----------------------------------------------------------------------------------------------
def _clock(ts, now):
    t, n = datetime.fromtimestamp(ts, TZ), datetime.fromtimestamp(now, TZ)
    return t.strftime("%H:%M") if t.date() == n.date() else t.strftime("%b %-d")


def prs_for(ids, prs, now):
    """The PRs naming any of the ids: open ones, and those merged within MERGED_WINDOW_S."""
    out = []
    for p in prs or []:
        if not ids & set(p.get("ids") or ()):
            continue
        if p["state"] == "open" or (p["state"] == "merged" and p.get("merged_at")
                                    and now - p["merged_at"] <= MERGED_WINDOW_S):
            out.append(p)
    return out


def pr_text(prs, now):
    """The PR part of a state line, or ""."""
    if not prs:
        return ""
    ci_word = {"passing": "checks passing", "failing": "checks failing", "running": "checks running",
               "none": "no checks"}
    opened = sorted((p for p in prs if p["state"] == "open"), key=lambda p: (p["draft"], -p["number"]))
    merged = sorted((p for p in prs if p["state"] == "merged"), key=lambda p: -(p.get("merged_at") or 0))
    if opened:
        p = opened[0]
        text = ("draft " if p["draft"] else "") + "#%d %s" % (p["number"], ci_word.get(p["ci"], "no checks"))
    else:
        p = merged[0]
        text = "#%d merged %s" % (p["number"], _clock(p["merged_at"], now))
    more = len(prs) - 1
    return text + (" (+%d PR%s)" % (more, "s" if more > 1 else "") if more else "")


def flight_text(ids, finished, now):
    """The newest finished line naming any of the ids: "HH:MM <text without its leading ids>", or ""."""
    best = None
    for f in finished or []:
        if ids & tasks.ticket_ids(f.get("text", "")) and f.get("ts") and (best is None or f["ts"] > best["ts"]):
            best = f
    if not best:
        return ""
    text = tasks._LEAD_IDS.sub("", best["text"], count=1).strip() or best["text"]
    return "%s %s" % (_clock(best["ts"], now), text)


def state_line(linear_state, prs_part, flight_part, hand, hand_at, now):
    """The parts in order of trust -> (state text, source tag). Source tags: "derived" (at least one fact),
    "hand" (fresh note alone), "hand-old" (an old note, shown with its age), "" (nothing)."""
    fresh = bool(hand) and hand_at is not None and 0 <= now - hand_at <= HAND_FRESH_S
    facts = [x for x in (linear_state, prs_part, flight_part) if x]
    if facts:
        return " · ".join(facts + ([hand] if fresh else [])), "derived"
    if not hand:
        return "", ""
    if fresh:
        return hand, "hand"
    when = datetime.fromtimestamp(hand_at, TZ).strftime("%b %-d %H:%M") if hand_at else "an old list"
    return "as of %s: %s" % (when, hand), "hand-old"
