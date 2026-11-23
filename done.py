"""The "Done this week" box: what was finished since Monday 00:00 (configured timezone), its own panel under the
scene in work hours (#shopspare).

Work hours only, the
task list's own rule (`tasks.shown()`, on the server clock); outside them `/api/done` is `show: false` and nothing
is read.

Sources (read-only, each fails closed and quiet: a source that has not answered adds nothing and the box says so in
amber, "GitHub as of 12:31" / "Linear has not answered"):
  1. the owner's MERGED PRs in the configured orgs, merged in the last 7 days (`MergedPRs`: one `gh api graphql` search,
     the gh CLI's own login, never a token read here, at most every 5 min in a background thread, the last good
     answer kept on disk with its age like the others, diskcache.py). Bodies are read only for ticket ids
     (derive.body_ids/branch_ids) and never kept.
  2. Linear issues assigned to the key's user COMPLETED in the last 7 days, every workspace, from the task list's own fetch
     (`tasks.QUERY`'s "done" field, the same read keys).
  3. "Done: ..." F lines of every IN FLIGHT block of the last 7 days in the notes file (a W line someone finished,
     moved to the F lines as "Done: ..."): work without a PR. The notes file is read only, tail first, parsed when it changes.

Merging duplicates: entries that share a ticket id or a PR count once (a PR naming ACME-1 and the Linear issue ACME-1
and a Done line about ACME-1 are one item); entries of the same local day whose words overlap by 60% or more (of the
shorter one) merge too, unless both carry their own ticket or both their own PR. An item's time is its
newest entry's; its title the Linear title, else the PR title, else the line's text.

The "can close" line: the owner's open In Review tickets whose linked PRs merged and none is open, the automatic waiting
line's own computation (inflight.auto_waiting), so the two never disagree.

All text is untrusted: plain text only, control characters and URLs stripped, cut (inflight.clean); the page puts it
in textContent only. Never raises.
"""
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import config
import derive
import inflight
import tasks

TZ = tasks.TZ
WINDOW_S = 7 * 86400


# "This week" means since Monday 00:00 in the configured timezone, not a rolling 7 days (on a Monday morning a rolling
# week reads as a huge count). The sources are still fetched for 7 days; the box cuts at the start of the week.
def week_start(now):
    """Monday 00:00 (configured timezone) of the week holding `now` (a datetime or epoch seconds), as an aware
    datetime."""
    if not isinstance(now, datetime):
        now = datetime.fromtimestamp(now, tz=timezone.utc)
    local = now.astimezone(TZ)
    return (local - timedelta(days=local.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)


def is_question(text):
    """A finished W line that was a question to you ("...?") is an answer you gave, not work done."""
    return "?" in (text or "")


SHOW = 6                         # the newest items sent; the page shows as many as fit and says "+N more"
TITLE_MAX = 80
OVERLAP = 0.6
MIN_WORDS = 3
ORGS = derive.ORGS
AUTHOR = derive.AUTHOR
FETCH_EVERY_S = 300
GOOD_FOR_S = 24 * 3600
LOG_EVERY_S = 3600
GH_TIMEOUT_S = 30
MAX_OUT = 8 * 1024 * 1024
NOTES_TAIL = 8 * 1024 * 1024   # bytes of the notes read from the end (about 7 days is well under 2 MB)
_DONE = re.compile(r"^\s*done\s*:\s*", re.I)
_WORDS = re.compile(r"[a-z0-9]+")
_STOP = set("""a an and are as at be by for from go has have in into is it its of on or that the then this to
up was with yes no not now new old your you done say""".split())


# --- 1. merged PRs ----------------------------------------------------------------------------------------------------
def query(today):
    since = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    fields = ("... on PullRequest { number title mergedAt headRefName body repository { name nameWithOwner } }")
    return "query { " + " ".join(
        '%s: search(query: "is:pr is:merged author:%s org:%s merged:>=%s", type: ISSUE, first: 100) { nodes { %s } }'
        % ("m%d" % i, AUTHOR, org, since, fields) for i, org in enumerate(ORGS)) + " }"


def parse_merged(doc):
    """gh's JSON -> [{"owner_repo", "repo", "number", "title", "merged_at", "ids"}]. Raises on a bad shape; drops
    malformed nodes and foreign orgs."""
    if not isinstance(doc, dict) or doc.get("errors") or not isinstance(doc.get("data"), dict):
        raise ValueError("graphql errors")
    out, seen = [], set()
    for i in range(len(ORGS)):
        for node in doc["data"]["m%d" % i]["nodes"]:
            try:
                repo, full, num = node["repository"]["name"], node["repository"]["nameWithOwner"], node["number"]
                title, at = node.get("title"), derive._epoch(node.get("mergedAt"))
                if (not isinstance(num, int) or isinstance(num, bool) or not isinstance(repo, str)
                        or not derive._REPO.match(repo) or not isinstance(full, str)
                        or not derive._OWNER_REPO.match(full) or (full, num) in seen or not isinstance(title, str)
                        or at is None):
                    continue
                seen.add((full, num))
                ids = derive.body_ids(node.get("body") if isinstance(node.get("body"), str) else "")
                ids |= derive.branch_ids(node.get("headRefName") if isinstance(node.get("headRefName"), str) else "")
                out.append({"owner_repo": full.lower(), "repo": repo, "number": num, "title": title[:500],
                            "merged_at": at, "ids": sorted(ids)})
            except (KeyError, TypeError, AttributeError):
                continue
    return out


class MergedPRs:
    """The owner's PRs merged in the last 7 days, cached in memory and on disk (the inflight.GitHub pattern)."""

    def __init__(self, run=derive.gh_run, clock=time.monotonic, wall=time.time, log=None, cache_file=None):
        self.run, self.clock, self.wall = run, clock, wall
        self.cache_file = cache_file
        self.log = log or (lambda line: print(line, file=sys.stderr, flush=True))
        self._lock = threading.Lock()
        self._running = False
        self._attempt = None
        self._logged = None
        self._good = None
        self._ok = None
        self._load_saved()

    def _load_saved(self):
        if not self.cache_file:
            return
        import diskcache
        doc = diskcache.load(self.cache_file) or {}
        try:
            wall = float(doc["wall"])
            prs = [p for p in doc["prs"] if isinstance(p, dict) and isinstance(p.get("number"), int)
                   and isinstance(p.get("merged_at"), int) and isinstance(p.get("title"), str)
                   and isinstance(p.get("owner_repo"), str) and isinstance(p.get("ids"), list)]
        except (KeyError, TypeError, ValueError):
            return
        age = self.wall() - wall
        if 0 <= age < GOOD_FOR_S:
            self._good = {"at": self.clock() - age, "wall": wall, "prs": prs}

    def _save(self):
        if self.cache_file and self._good:
            import diskcache
            diskcache.save(self.cache_file, {"v": 1, "wall": self._good["wall"], "prs": self._good["prs"]})

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
            code, out = self.run([derive.GH, "api", "graphql", "-f", "query=" + query(today)], GH_TIMEOUT_S)
            if code != 0:
                raise RuntimeError("exit %s" % code if isinstance(code, int) else "exit")
            if not isinstance(out, str) or len(out) > MAX_OUT:
                raise ValueError("bad output")
            prs = parse_merged(json.loads(out))
            with self._lock:
                self._good = {"at": self.clock(), "wall": self.wall(), "prs": prs}
                self._ok = True
            self._save()
        except Exception as e:
            with self._lock:
                self._ok = False
            detail = str(e) if isinstance(e, RuntimeError) and re.fullmatch(r"exit -?\d+", str(e)) else ""
            self._note("done: github merged PRs fetch failed (%s%s)" % (type(e).__name__,
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
        threading.Thread(target=go, name="merged-prs-fetch", daemon=True).start()

    def snapshot(self):
        """{"prs", "status": "ok"|"stale"|"off"|"pending", "at"} (the inflight.GitHub shape)."""
        now = self.clock()
        with self._lock:
            good = self._good if self._good and now - self._good["at"] < GOOD_FOR_S else None
            status = ("ok" if good and self._ok else "stale" if good else "pending" if self._ok is None else "off")
            return {"prs": [dict(p) for p in good["prs"]] if good else [], "status": status,
                    "at": int(good["wall"]) if good else None}


# --- 3. the notes's Done lines ----------------------------------------------------------------------------------------
def notes_done(lines, now):
    """Notes lines -> [{"ts", "text", "ids"}]: every "F. HH:MM Done: ..." line of every IN FLIGHT block whose header
    is within the last 7 days (and not in the future), once per (time, text). Never raises on odd input."""
    out, seen = [], set()
    start = week_start(now)
    at = None
    for line in lines or ():
        m = inflight._HEADER.match(line)
        if m:
            at = inflight.header_time(m)
            if at is not None and not (start - timedelta(days=1) <= at <= now + inflight.FUTURE_SLACK):
                at = None
            continue
        if at is None:
            continue
        if not line.strip() or not line[0].isspace():
            at = None
            continue
        im = inflight._ITEM.match(line)
        if not im or im.group(1) != "F":
            continue
        t, rest = inflight.item_time(im.group(2), at)
        if t is None or not _DONE.match(rest) or t < start or t > now + inflight.FUTURE_SLACK:
            continue
        raw = _DONE.sub("", rest, count=1)
        text = inflight.clean(raw, 500)
        if not text or is_question(text):
            continue
        key = (int(t.timestamp()), text)
        if key in seen:
            continue
        seen.add(key)
        out.append({"ts": int(t.timestamp()), "text": text, "ids": sorted(tasks.ticket_ids(text))})
    return out


def read_notes_tail(path, limit=NOTES_TAIL):
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        off = max(0, size - limit)
        f.seek(off)
        lines = f.read().decode("utf-8", errors="replace").split("\n")
    return lines[1:] if off > 0 else lines


# --- merging ----------------------------------------------------------------------------------------------------------
def words(text):
    return {w for w in _WORDS.findall(tasks.strip_ids(text or "").lower())
            if len(w) > 2 and w not in _STOP and not w.isdigit()}


def overlap(a, b):
    if len(a) < MIN_WORDS or len(b) < MIN_WORDS:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _day(ts):
    return datetime.fromtimestamp(ts, TZ).date()


def _work_ids(ids, prefixes):
    """Only ids whose prefix is a Linear team key ("M-320", "MAIN-2026" in a PR body are not tickets); all of them
    when the keys are not known."""
    ids = set(ids or ())
    return {i for i in ids if i.partition("-")[0] in prefixes} if prefixes else ids


def combine(tickets, prs, notes, prefixes=None):
    """Entries from the three sources -> merged items, newest first:
    [{"ts", "ref", "pr", "title", "kinds"}]. prefixes: the Linear team keys (ids with another prefix are ignored). ref: the ticket id(s) ("ACME-4892/4893") or "repo#n"; pr: "#n" when the
    item has a PR and a ticket ref, else ""."""
    entries = []
    for t in tickets:
        entries.append({"ts": t["completed_at"], "tickets": {t["identifier"]}, "prs": set(), "kind": "linear",
                        "title": t["title"], "rank": 0})
    for p in prs:
        entries.append({"ts": p["merged_at"], "tickets": _work_ids(p.get("ids"), prefixes),
                        "prs": {"%s#%d" % (p["owner_repo"], p["number"])}, "kind": "pr", "title": p["title"],
                        "rank": 1, "repo": p.get("repo") or p["owner_repo"].split("/")[-1], "number": p["number"]})
    for n in notes:
        entries.append({"ts": n["ts"], "tickets": _work_ids(n.get("ids"), prefixes), "prs": set(), "kind": "note",
                        "title": tasks._LEAD_IDS.sub("", n["text"], count=1).strip() or n["text"], "rank": 2})
    # union by shared ticket id or PR
    parent = list(range(len(entries)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    owner = {}
    for i, e in enumerate(entries):
        for k in ["t:" + x for x in e["tickets"]] + ["p:" + x for x in e["prs"]]:
            if k in owner:
                parent[find(i)] = find(owner[k])
            else:
                owner[k] = i
    groups = {}
    for i, e in enumerate(entries):
        groups.setdefault(find(i), []).append(e)
    clusters = []
    for members in groups.values():
        clusters.append({"members": members, "tickets": set().union(*(m["tickets"] for m in members)),
                         "prs": set().union(*(m["prs"] for m in members)),
                         "days": {_day(m["ts"]) for m in members},
                         "words": [words(m["title"]) for m in members]})
    # same day, heavy word overlap: merge (never two items that each have their own ticket, or each their own PR)
    merged = True
    while merged:
        merged = False
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                a, b = clusters[i], clusters[j]
                if (a["tickets"] and b["tickets"]) or (a["prs"] and b["prs"]) or not (a["days"] & b["days"]):
                    continue
                if any(overlap(x, y) >= OVERLAP for x in a["words"] for y in b["words"]):
                    a["members"] += b["members"]
                    for k in ("tickets", "prs", "days"):
                        a[k] |= b[k]
                    a["words"] += b["words"]
                    del clusters[j]
                    merged = True
                    break
            if merged:
                break
    items = []
    for c in clusters:
        best = min(c["members"], key=lambda m: (m["rank"], -m["ts"]))
        pr = min((m for m in c["members"] if m["kind"] == "pr"), key=lambda m: -m["ts"], default=None)
        ids = sorted(c["tickets"], key=tasks._id_sort)
        if ids:
            ref = tasks.shorthand(ids[:3]) + (" +%d" % (len(ids) - 3) if len(ids) > 3 else "")
            pr_word = "#%d" % pr["number"] if pr else ""
        elif pr:
            ref, pr_word = "%s#%d" % (pr["repo"], pr["number"]), ""
        else:
            ref, pr_word = "", ""
        items.append({"ts": max(m["ts"] for m in c["members"]), "ref": ref, "pr": pr_word,
                      "title": inflight.clean(best["title"], TITLE_MAX),
                      "kinds": sorted({m["kind"] for m in c["members"]})})
    return sorted(items, key=lambda x: -x["ts"])


def can_close(linear, ticket_prs):
    """The ticket ids the automatic waiting line names (inflight.auto_waiting, hand lines not considered)."""
    try:
        return [w["id"] for w in inflight.auto_waiting(linear, ticket_prs, ())]
    except Exception:
        return []


# --- the source and the payload -----------------------------------------------------------------------------------------
HIDDEN = {"show": False, "items": [], "count": 0, "more": 0, "can_close": [], "notes": []}
WS_NAME = config.workspace_labels()


def _hm(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%-I:%M") if ts else ""


class Source:
    """Joins the three sources. linear: tasks.Linear; merged: MergedPRs; ticket_prs: derive.TicketPRs (for the
    "can close" line); config: where the notes is named. Never raises."""

    def __init__(self, linear=None, merged=None, ticket_prs=None, notes=None, now=None):
        self.linear, self.merged, self.ticket_prs, self.notes = linear, merged, ticket_prs, notes
        self.now = now or (lambda: datetime.now(TZ))
        self._key, self._lines = None, None
        self._lock = threading.Lock()

    def notes_wanted(self):
        return bool(self.notes or config.get()["notes"]["path"])

    def _notes(self):
        try:
            path = tasks.notes_path(self.notes)
            st = os.stat(path)
            key = (path, st.st_mtime_ns, st.st_size)
            if key != self._key:
                self._lines = read_notes_tail(path)
                self._key = key
            return self._lines, True
        except Exception:
            self._key, self._lines = None, None
            return [], False

    def current(self):
        with self._lock:
            now = self.now()
            now_ts = int(now.timestamp())
            start = week_start(now_ts).timestamp()
            notes_out = []
            # Linear: completed issues of every workspace that answered with its "done" field
            tickets, lin = [], None
            if self.linear is not None:
                try:
                    self.linear.maybe_refresh()
                    lin = self.linear.snapshot()
                except Exception:
                    lin = None
            ws = (lin or {}).get("ws") or {}
            stale_at = []
            for name, label in WS_NAME.items():
                data = ws.get(name)
                if not data or data.get("done") is None:
                    notes_out.append("Linear %s has not answered" % label)
                    continue
                tickets += [t for t in data["done"] if t["completed_at"] >= start]
                if lin.get("status") != "ok" and data.get("wall"):
                    stale_at.append(data["wall"])
            if stale_at:
                notes_out.append("Linear as of %s" % _hm(min(stale_at)))
            # GitHub: merged PRs
            prs = []
            gh = {"status": "off"}
            if self.merged is not None:
                try:
                    self.merged.maybe_refresh()
                    gh = self.merged.snapshot()
                except Exception:
                    gh = {"status": "off"}
            if gh.get("status") in ("ok", "stale"):
                prs = [p for p in gh["prs"] if p["merged_at"] >= start]
                if gh["status"] == "stale":
                    notes_out.append("GitHub as of %s" % _hm(gh.get("at")))
            elif gh.get("status") == "pending":
                notes_out.append("GitHub loading")
            elif self.merged is not None:
                notes_out.append("GitHub has not answered")
            # the notes's Done lines
            lines, ok = self._notes()
            notes = notes_done(lines, now) if ok else []
            if not ok and self.notes_wanted():
                notes_out.append("notes file not readable")
            prefixes = set()
            for data in ws.values():
                prefixes |= set(data.get("teams") or ())
            items = combine(tickets, prs, notes, prefixes)
            closable = []
            if self.linear is not None and self.ticket_prs is not None and lin:
                try:
                    closable = can_close(lin, self.ticket_prs.snapshot())
                except Exception:
                    closable = []
            return {"show": True, "count": len(items), "items": items[:SHOW], "more": max(0, len(items) - SHOW),
                    "can_close": closable, "notes": notes_out, "now": now_ts}


def payload(source, now=None):
    """The /api/done JSON (a dict): HIDDEN outside work hours (tasks.shown, no remote override: the box lives in the
    work-hours layout only). Never raises."""
    if not tasks.shown(now):
        return dict(HIDDEN, now=int(time.time()))
    try:
        return source.current()
    except Exception:
        return dict(HIDDEN, show=True, notes=["not available"], now=int(time.time()))
