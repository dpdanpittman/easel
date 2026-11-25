"""The In flight panel: a live answer to "what's waiting on me?", built on the server (read-only).

Work hours only: the same rule as the work task list, `tasks.shown()` (settings `time`, on the server clock), lifted
only when the remote chooses the "inflight" panel. Outside those hours `payload` returns HIDDEN (show false, nothing
in it, the notes file and GitHub not even read) and the easel shows its off-hours view in this spot. Four groups:

  waiting   things only you can do (notes "W." lines, plus your open non-draft PRs whose checks all pass, judged
            by the newest-run rule of checks.py: a superseded cancelled run does not count)
  running   background builds and long jobs the assistant has going (notes "R." lines with a start time, and the
            agents the hook recorded, agents.py)
  finished  the last few things that landed (notes "F." lines, newest first, at most 6)
  prs       your other open non-draft PRs (checks failing, running or absent), quieter, so nothing is lost

Source A, the notes file (the same file as tasks.py, `tasks.notes_path()`). The NEWEST block whose header line
starts at column 0 as "- YYYY-MM-DD HH:MM [TZ] IN FLIGHT" (then "(", ":" or the end of the line):

    - 2026-01-05 12:45 IN FLIGHT (current):
      W. OPS-577: apply the staging stack and create the CI tokens
      R. 12:33 ACME-4810 scripts build
      F. 12:41 easel task list fix live

Item lines are indented and start with "W.", "R." or "F." then text. R and F text starts with "HH:MM" (local time,
the day of the header; R = start, F = finish). A time later than the header (plus 10 minutes of slack) is taken as
the day before (a job started before midnight, the block written after). A line without a valid time still shows,
without one. Any other indented line is ignored; the block ends at the first empty or non-indented line. A block
whose header is older than 18 hours is treated as absent ("stale"). Text is untrusted: URLs, backticks and control
characters stripped, cut, and only ever displayed as text.

Source B, GitHub (`GitHub`): your open non-draft PRs in the configured orgs (settings `github`), one
`gh api graphql` query (the gh CLI already logged in on this box; this code never reads or passes a token) run as a
subprocess with a timeout, in a background thread, at most every 5 minutes, only while the panel is being requested.
The last good result is used for up to a day, shown with its age; the journal gets at most one line an hour (error
type only, never gh's output). Never raises. A W line naming the same repo#number as a PR shows once (the line's
words, with the PR's marks).

Source C, computed (`auto_waiting`): a ticket can sit In Review for days with its PRs merged, because only a hand W
line would put it on the list. So a ticket gets the line "<ID>: PRs merged, close or finish it" (the live title after
it when there is room), "source": "auto", when ALL hold: open and assigned to you in Linear (the task list's own
fetch); its Linear state is named "In Review"; it has at least one linked PR (the GitHub PR links among its Linear
attachments, and your PRs matched the way derive.py matches them: the id in the body or the branch); at least one of
those PRs is merged and none is open (a closed-unmerged PR, a superseded version, is ignored). Never written to the
notes file: it goes away by itself when the ticket leaves In Review or closes. A current hand W line naming the
ticket wins (no duplicate). Fails closed: no Linear answer for that workspace, no PR answer, attachments not known (an
older saved copy), or any linked PR whose state is not known -> no automatic line; the hand lines are never touched.

Source D, computed (`auto_approved`): a PR someone else wrote can sit unmerged for days after you approved it,
because only your own PRs were watched. The same GitHub query also searches "is:pr is:open reviewed-by:<author>
org:<each org>" (alias a0, first 50, no extra call). A PR gets the line "<ID> #<n> <repo>: approved <N>d ago, not
merged · behind|conflicting|blocked|clean" (no id: it starts with "#<n>"), "source": "auto", the CI verdict
(newest-run rule) as the line's pill, when: it is open and not a draft, your NEWEST opinionated review on it (APPROVED,
CHANGES_REQUESTED or DISMISSED; comments do not count) is APPROVED, and that approval is at least
`github.approved_after_h` hours old (judged at serve time, so the saved copy ages correctly; 0 turns the line off and
leaves the search out of the query). Never written to the notes file; a current hand W line naming the same repo#n or
ticket wins; no line when GitHub has not answered (the last good answer, up to 24 h, is used with its age like the PR
lists). A PR you approved that someone else drives can be muted (`github.approved_mute_file`, one "repo#n" per line):
it leaves Waiting on you and shows under Open PRs as "watching" (`watching_prs`) until it merges or closes.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

import checks
import config
import derive
import tasks

TZ = tasks.TZ
STALE_S = 18 * 3600
FINISHED_MAX = 6
TEXT_MAX = 90
ITEMS_MAX = 12                    # per group, before the finished cap (a runaway block cannot flood the page)
FUTURE_SLACK = timedelta(minutes=10)

# "- YYYY-MM-DD HH:MM [TZ] IN FLIGHT": the timezone label is optional and any 1-5 capitals ("UTC", "CET"); the time
# is read in the configured timezone
_HEADER = re.compile(r"^- (\d{4}-\d{2}-\d{2}) (\d{1,2}):(\d{2})(?:\s+[A-Z]{1,5})? IN FLIGHT(?:\s*\(|\s*:|\s*$)")
_ITEM = re.compile(r"^\s+([WRF])\.\s+(.*\S)\s*$")
_TIME = re.compile(r"^(\d{1,2}):(\d{2})(?=\s|$)\s*")
_REF = re.compile(r"\b(?:[A-Za-z0-9_.-]+/)?([A-Za-z0-9_.-]+)#(\d+)\b")


def clean(text, limit=TEXT_MAX):
    return tasks._cut(tasks._CTRL.sub(" ", tasks._clean(text)), limit)


def find_block(lines):
    """(header match, raw item lines) of the newest IN FLIGHT block, or (None, None)."""
    for i in range(len(lines) - 1, -1, -1):
        m = _HEADER.match(lines[i])
        if m:
            items = []
            for line in lines[i + 1:]:
                if not line.strip() or not line[0].isspace():
                    break
                if _ITEM.match(line):
                    items.append(line)
            return m, items
    return None, None


def header_time(m):
    """The block header's datetime (local), or None when it is not a real date and time."""
    try:
        h, mi = int(m.group(2)), int(m.group(3))
        d = datetime.strptime(m.group(1), "%Y-%m-%d")
        return datetime(d.year, d.month, d.day, h, mi, tzinfo=TZ)
    except ValueError:
        return None


def item_time(text, at):
    """("HH:MM" text prefix -> (datetime or None, rest of the text)). The day is the header's; a time after the
    header (plus slack) is the day before."""
    m = _TIME.match(text)
    if not m:
        return None, text
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None, text
    t = at.replace(hour=h, minute=mi, second=0, microsecond=0)
    if t > at + FUTURE_SLACK:
        t -= timedelta(days=1)
    return t, text[m.end():]


def parse(lines, now):
    """Notes lines -> {"state": "ok"|"stale"|"missing", "at": header ts or None, "waiting", "running",
    "finished", "counts": the real number of lines per group (before the caps), "w_all"/"r_all": every W text and R
    item of the block, uncapped (the task list's waiting/running flags, tasks.apply_flight)}. Never raises on odd
    input."""
    out = {"state": "missing", "at": None, "waiting": [], "running": [], "finished": [],
           "counts": {"waiting": 0, "running": 0, "finished": 0}, "w_all": [], "r_all": [], "f_all": []}
    m, raw = find_block(lines)
    if m is None:
        return out
    at = header_time(m)
    if at is None:
        return out
    out["at"] = int(at.timestamp())
    if (now - at).total_seconds() > STALE_S:
        out["state"] = "stale"
        return out
    out["state"] = "ok"
    for line in raw:
        kind, body = _ITEM.match(line).groups()
        if kind == "W":
            text = clean(body)
            if not text:
                continue
            out["counts"]["waiting"] += 1
            out["w_all"].append(text)
            if len(out["waiting"]) < ITEMS_MAX:
                out["waiting"].append({"text": text, "source": "notes", "ref": ref_of(body)})
            continue
        t, rest = item_time(body, at)
        text = clean(rest)
        if not text:
            continue
        item = {"text": text, "ts": int(t.timestamp()) if t else None, "clock": t.strftime("%H:%M") if t else ""}
        out["counts"]["running" if kind == "R" else "finished"] += 1
        if kind == "R":
            out["r_all"].append(dict(item))
        else:
            out["f_all"].append(dict(item))
        group = out["running"] if kind == "R" else out["finished"]
        if len(group) < ITEMS_MAX:
            group.append(item)
    # newest first; lines without a time keep their order after the timed ones
    out["finished"] = sorted(out["finished"], key=lambda x: (x["ts"] is None, -(x["ts"] or 0)))[:FINISHED_MAX]
    return out


def ref_of(text):
    """The first "repo#123" (or "owner/repo#123") a line names, as "repo#123" lower-cased, or ""."""
    m = _REF.search(tasks._clean(text))
    return (m.group(1) + "#" + m.group(2)).lower() if m else ""


# --- GitHub ---------------------------------------------------------------------------------------------------------
_G = config.get()["github"]
GH = _G["gh"]
ORGS = tuple(_G["orgs"])
AUTHOR = _G["author"]
FETCH_EVERY_S = 300
GOOD_FOR_S = 24 * 3600           # the last good result (memory, and on disk across restarts), shown with its age
LOG_EVERY_S = 3600
GH_TIMEOUT_S = 30
MAX_OUT = 16 * 1024 * 1024        # the check runs of up to 100 PRs (checks.ROLLUP)
_PR_FIELDS = ("... on PullRequest { number title isDraft reviewDecision repository { name nameWithOwner } "
              "commits(last: 1) { nodes { commit { " + checks.ROLLUP + " } } } }")
# Source D: open PRs you have reviewed, whoever wrote them, in the same query (alias "a0"): your own reviews (state,
# time), the merge state and the newest-run CI of the last commit. Bodies are read only for ticket ids. With author
# "@me" the reviews are not filtered in the query (GraphQL wants a login there): the viewer's login is asked for too
# and the reviews are filtered here.
APPROVED_AFTER_S = float(_G.get("approved_after_h", 48)) * 3600
_ME = AUTHOR == "@me"
_REVIEWS = ("reviews(last: 100) { nodes { state submittedAt author { login } } }" if _ME
            else 'reviews(author: "%s", last: 50) { nodes { state submittedAt author { login } } }' % AUTHOR)
_APPROVED_FIELDS = ("... on PullRequest { number title body headRefName isDraft state mergeable mergeStateStatus "
                    "repository { name nameWithOwner } " + _REVIEWS + " "
                    "commits(last: 1) { nodes { commit { " + checks.ROLLUP + " } } } }")
APPROVED_SEARCH = "is:pr is:open reviewed-by:%s %s" % (AUTHOR, " ".join("org:" + o for o in ORGS))
QUERY = "query { " + " ".join(
    '%s: search(query: "is:pr is:open draft:false author:%s org:%s", type: ISSUE, first: 50) { nodes { %s } }'
    % ("o%d" % i, AUTHOR, org, _PR_FIELDS) for i, org in enumerate(ORGS)) + (
    (' a0: search(query: "%s", type: ISSUE, first: 50) { nodes { %s } }' % (APPROVED_SEARCH, _APPROVED_FIELDS)
     + (" viewer { login }" if _ME else "")) if APPROVED_AFTER_S > 0 and ORGS else "") + " }"
REVIEW = {"APPROVED": "approved", "CHANGES_REQUESTED": "changes requested", "REVIEW_REQUIRED": "review required"}
_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_OWNER_REPO = re.compile(r"^(?:%s)/[A-Za-z0-9_.-]{1,100}$" % "|".join(re.escape(o) for o in ORGS))


def gh_run(args, timeout):
    """Run gh; (returncode, stdout). gh reads its own login; nothing here touches a token. stderr is dropped."""
    r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    return r.returncode, r.stdout


def parse_prs(doc):
    """gh's JSON -> [{"repo", "owner_repo", "number", "title", "ci", "review"}]. Raises on a bad shape.
    Everything is untrusted: only well-formed fields are kept."""
    if not isinstance(doc, dict) or doc.get("errors") or not isinstance(doc.get("data"), dict):
        raise ValueError("graphql errors")
    prs, seen = [], set()
    for i in range(len(ORGS)):
        for node in doc["data"]["o%d" % i]["nodes"]:
            try:
                repo, full = node["repository"]["name"], node["repository"]["nameWithOwner"]
                num, title = node["number"], node["title"]
                if (node.get("isDraft") is not False or not isinstance(num, int) or isinstance(num, bool)
                        or not isinstance(title, str) or not isinstance(repo, str) or not _REPO.match(repo)
                        or not isinstance(full, str) or not _OWNER_REPO.match(full) or (full, num) in seen):
                    continue
                seen.add((full, num))
                prs.append({"repo": repo, "owner_repo": full, "number": num, "title": clean(title[:500], 80),
                            "ci": checks.judge_commits(node.get("commits")),
                            "review": REVIEW.get(node.get("reviewDecision"), "")})
            except (KeyError, TypeError, AttributeError):
                continue
    return prs


MERGE_STATE = {"BEHIND": "behind", "DIRTY": "conflicting", "CLEAN": "clean", "HAS_HOOKS": "clean",
               "UNSTABLE": "clean", "BLOCKED": "blocked"}
OPINION = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}     # a COMMENTED or pending review changes nothing


def _latest_opinion(reviews, who=None):
    """Your reviews on a PR -> (state, epoch) of your newest APPROVED / CHANGES_REQUESTED / DISMISSED one, or None.
    `who`: your login, when the query could not filter by author (a review by anyone else is skipped)."""
    best = None
    for r in (reviews or {}).get("nodes") or []:
        if not isinstance(r, dict) or r.get("state") not in OPINION:
            continue
        login = ((r.get("author") or {}).get("login") or "") if isinstance(r.get("author"), dict) else ""
        if who is not None and login.lower() != who.lower():
            continue
        at = derive._epoch(r.get("submittedAt"))
        if at is not None and (best is None or at >= best[1]):
            best = (r["state"], at)
    return best


def parse_approved(doc):
    """gh's JSON -> the open, non-draft PRs whose newest review by you is APPROVED:
    [{"repo", "owner_repo", "number", "title", "ids", "approved_at", "merge", "ci"}], or None when the answer has no
    usable "a0" part (unknown: no lines). Malformed nodes are dropped. Never raises."""
    try:
        nodes = doc["data"]["a0"]["nodes"]
        if not isinstance(nodes, list):
            return None
        who = None
        if _ME:
            who = doc["data"]["viewer"]["login"]
            if not isinstance(who, str) or not who:
                return None
    except (KeyError, TypeError):
        return None
    out, seen = [], set()
    for node in nodes:
        try:
            repo, full = node["repository"]["name"], node["repository"]["nameWithOwner"]
            num, title = node["number"], node["title"]
            if (node.get("isDraft") is not False or node.get("state") != "OPEN" or not isinstance(num, int)
                    or isinstance(num, bool) or not isinstance(title, str) or not isinstance(repo, str)
                    or not _REPO.match(repo) or not isinstance(full, str) or not _OWNER_REPO.match(full)
                    or (full, num) in seen):
                continue
            latest = _latest_opinion(node.get("reviews"), who)
            if not latest or latest[0] != "APPROVED":
                continue
            seen.add((full, num))
            ids = tasks.ticket_ids(title)
            ids |= derive.body_ids(node.get("body") if isinstance(node.get("body"), str) else "")
            ids |= derive.branch_ids(node.get("headRefName") if isinstance(node.get("headRefName"), str) else "")
            merge = MERGE_STATE.get(node.get("mergeStateStatus"), "")
            if node.get("mergeable") == "CONFLICTING":
                merge = "conflicting"
            out.append({"repo": repo, "owner_repo": full.lower(), "number": num, "title": clean(title[:500], 80),
                        "ids": sorted(ids, key=tasks._id_sort), "approved_at": latest[1], "merge": merge,
                        "ci": checks.judge_commits(node.get("commits"))})
        except (KeyError, TypeError, AttributeError):
            continue
    return out


class GitHub:
    """Your open PRs, cached in memory. `maybe_refresh()` (non-blocking) starts a background fetch when the last
    attempt is 5 minutes old; `snapshot()` gives what may be used now."""

    def __init__(self, run=gh_run, clock=time.monotonic, wall=time.time, log=None, cache_file=None):
        self.run, self.clock, self.wall = run, clock, wall
        self.cache_file = cache_file    # the last good result survives a restart (diskcache.py); None: memory only
        self.log = log or (lambda line: print(line, file=sys.stderr, flush=True))
        self._lock = threading.Lock()
        self._running = False
        self._attempt = None
        self._logged = None
        self._good = None                       # {"at", "wall", "prs", "approved": list or None (unknown)}
        self._ok = None                         # did the last attempt succeed (None: none finished yet)
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
        approved = doc.get("approved")
        approved = [p for p in approved if isinstance(p, dict)] if isinstance(approved, list) else None
        age = self.wall() - wall
        if 0 <= age < GOOD_FOR_S:
            self._good = {"at": self.clock() - age, "wall": wall, "prs": prs, "approved": approved}

    def _save(self):
        if self.cache_file and self._good:
            import diskcache
            diskcache.save(self.cache_file, {"v": 1, "wall": self._good["wall"], "prs": self._good["prs"],
                                             "approved": self._good.get("approved")})

    def _note(self, line):
        now = self.clock()
        if self._logged is None or now - self._logged >= LOG_EVERY_S:
            self._logged = now
            self.log(line)

    def refresh(self):
        """Fetch now (blocking). Never raises."""
        with self._lock:
            self._attempt = self.clock()
        try:
            code, out = self.run([GH, "api", "graphql", "-f", "query=" + QUERY], GH_TIMEOUT_S)
            if code != 0:
                raise RuntimeError("exit %s" % code if isinstance(code, int) else "exit")
            if not isinstance(out, str) or len(out) > MAX_OUT:
                raise ValueError("bad output")
            doc = json.loads(out)
            prs = parse_prs(doc)
            approved = parse_approved(doc)
            with self._lock:
                self._good = {"at": self.clock(), "wall": self.wall(), "prs": prs, "approved": approved}
                self._ok = True
            self._save()
        except Exception as e:
            with self._lock:
                self._ok = False
            detail = str(e) if isinstance(e, RuntimeError) and re.fullmatch(r"exit -?\d+", str(e)) else ""
            self._note("inflight: github fetch failed (%s%s)" % (type(e).__name__, " " + detail if detail else ""))

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
        threading.Thread(target=go, name="github-fetch", daemon=True).start()

    def snapshot(self):
        """{"prs": [...], "approved": [...] or None (not known), "status": "ok"|"stale"|"off"|"pending",
        "at": wall clock of the good result or None}."""
        now = self.clock()
        with self._lock:
            good = self._good if self._good and now - self._good["at"] < GOOD_FOR_S else None
            if good and self._ok:
                status = "ok"
            elif good:
                status = "stale"
            elif self._ok is None:
                status = "pending"
            else:
                status = "off"
            approved = good.get("approved") if good else None
            return {"prs": [dict(p) for p in good["prs"]] if good else [],
                    "approved": [dict(p) for p in approved] if isinstance(approved, list) else None,
                    "status": status, "at": int(good["wall"]) if good else None}


# --- the automatic waiting line --------------------------------------------------------------------
REVIEW_STATE = "in review"
AUTO_TEXT = "PRs merged, close or finish it"
AUTO_TITLE_MIN = 12               # the live title is added only when at least this much of it fits


def _in_review(issue):
    return isinstance(issue.get("state"), str) and issue["state"].strip().lower() == REVIEW_STATE


def wanted_refs(linear):
    """The linked PRs ("owner/repo#n", from Linear attachments) of your In Review issues: the PRs whose state
    derive.TicketPRs looks up when its own search did not return them. Never raises."""
    refs = set()
    try:
        for data in (linear or {}).get("ws", {}).values():
            for issue in data.get("issues") or []:
                if _in_review(issue) and isinstance(issue.get("prs"), list):
                    refs |= set(issue["prs"])
    except (AttributeError, TypeError):
        return set()
    return refs


def _pr_key(p):
    owner_repo = p.get("owner_repo") or ("?/" + str(p.get("repo")))
    return "%s#%s" % (str(owner_repo).lower(), p.get("number"))


def auto_line(ident, title):
    """The fixed text, with the live title shortened after it when there is room."""
    text = "%s: %s" % (ident, AUTO_TEXT)
    room = TEXT_MAX - len(text) - 3
    title = clean(title or "", 500)
    if title and room >= AUTO_TITLE_MIN:
        text += " · " + tasks._cut(title, room)
    return text


def auto_waiting(linear, prs, hand_w=()):
    """Linear snapshot (tasks.Linear.snapshot) + PR snapshot (derive.TicketPRs.snapshot) + the current block's hand
    W texts -> [{"text", "source": "auto", "id"}], one per ticket that meets every condition (module docstring).
    Fails closed and quiet: [] whenever a source has not answered. Never raises."""
    try:
        if not linear or not prs or prs.get("status") not in ("ok", "stale") or prs.get("closed") is None:
            return []
        hand_ids = set()
        for text in hand_w or ():
            hand_ids |= tasks.ticket_ids(text)
        known = {}                                   # every PR the search returned (open, merged, closed), by ref
        searched = list(prs.get("prs") or []) + list(prs.get("closed") or [])
        for p in searched:
            known[_pr_key(p)] = p.get("state")
        looked = prs.get("looked") or {}
        out = []
        for name in sorted((linear.get("ws") or {})):
            for issue in linear["ws"][name].get("issues") or []:
                ident = issue.get("identifier")
                if not isinstance(ident, str) or not _in_review(issue) or ident in hand_ids:
                    continue
                refs = issue.get("prs")
                if not isinstance(refs, list):       # attachments not known: never guess
                    continue
                linked = {r: known.get(r) or looked.get(r) for r in refs}
                for p in searched:
                    if ident in (p.get("ids") or ()):
                        linked[_pr_key(p)] = p.get("state")
                # : a closed-unmerged PR (a superseded version) no longer blocks the line; at least
                # one merged, none open, and every state known (anything else fails closed)
                states = set(linked.values())
                if "merged" in states and states <= {"merged", "closed"}:
                    out.append({"text": auto_line(ident, issue.get("title")), "source": "auto", "id": ident})
        return sorted(out, key=lambda w: tasks._id_sort(w["id"]))
    except Exception:
        return []


# --- approved by you, not merged -------------------------------------------------------------------------------------
APPROVED_TEXT = "approved %dd ago, not merged"


def approved_line(p, now):
    """"<TICKET> #<n> <repo>: approved <N>d ago, not merged · <merge state>" (+ the title when there is room)."""
    lead = (p["ids"][0] + " ") if p.get("ids") else ""
    text = "%s#%d %s: %s" % (lead, p["number"], p["repo"], APPROVED_TEXT % int((now - p["approved_at"]) // 86400))
    if p.get("merge"):
        text += " · " + p["merge"]
    room = TEXT_MAX - len(text) - 3
    title = clean(p.get("title") or "", 500)
    if title and room >= AUTO_TITLE_MIN:
        text += " · " + tasks._cut(title, room)
    return clean(text)


_MUTE_REF = re.compile(r"^(?:[A-Za-z0-9_.-]+/)?([A-Za-z0-9_.-]{1,100})#(\d+)$")


def approved_mute(path=None):
    """The muted "repo#n" refs from `github.approved_mute_file`: PRs you approved that someone else drives, kept off
    Waiting on you. One ref per line ("repo#12" or "owner/repo#12"), "#" starts a comment at the start of a line or
    after a space. Missing or unreadable file -> empty set. Never raises."""
    try:
        path = os.path.expanduser(path or config.get()["github"]["approved_mute_file"] or "")
        if not path:
            return set()
        refs = set()
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = re.split(r"(?:^|\s)#(?!\d)", line, maxsplit=1)[0].strip()
                m = _MUTE_REF.match(line)
                if m:
                    refs.add((m.group(1) + "#" + m.group(2)).lower())
        return refs
    except Exception:
        return set()


def _muted(mute):
    return approved_mute() if mute is None else {str(m).split("/")[-1].lower() for m in mute}


def auto_approved(gh, hand_w=(), prefixes=None, now=None, mute=None):
    """A GitHub snapshot (GitHub.snapshot) + the current block's hand W texts -> [{"text", "source": "auto", "id",
    "ci"}], one per open PR (any author) whose newest review by you is APPROVED and older than APPROVED_AFTER_S,
    oldest approval first. Ticket ids come from the title, the body (derive.body_ids) and the branch, kept only when
    their prefix is a work team key (when those are known); the first one leads the line so tasks.apply_flight
    attaches it. Your own PRs are left out (they have their own lines), and so are the muted ones (approved_mute;
    `mute` overrides the file, for tests): watching_prs shows those. A hand W line naming the same repo#n or one of
    its ids wins. Fails closed: [] when GitHub has not answered or the answer did not include the approved search.
    Never written anywhere. Never raises."""
    try:
        if APPROVED_AFTER_S <= 0:
            return []
        if not gh or gh.get("status") not in ("ok", "stale") or not isinstance(gh.get("approved"), list):
            return []
        now = time.time() if now is None else now
        hand_ids, hand_refs = set(), set()
        for text in hand_w or ():
            hand_ids |= tasks.ticket_ids(text)
            for m in _REF.finditer(tasks._clean(text)):
                hand_refs.add((m.group(1) + "#" + m.group(2)).lower())
        mine = {(p.get("repo", "") + "#" + str(p.get("number"))).lower() for p in gh.get("prs") or []}
        prefixes = set(prefixes or ())
        muted = _muted(mute)
        out = []
        for p in sorted(gh["approved"], key=lambda x: x.get("approved_at") or 0):
            if not isinstance(p.get("approved_at"), (int, float)) or now - p["approved_at"] < APPROVED_AFTER_S:
                continue
            ref = (str(p.get("repo")) + "#" + str(p.get("number"))).lower()
            ids = [i for i in p.get("ids") or [] if not prefixes or i.split("-")[0] in prefixes]
            if ref in hand_refs or ref in mine or set(ids) & hand_ids or ref in muted:
                continue
            out.append({"text": approved_line(dict(p, ids=ids), now), "source": "auto",
                        "id": ids[0] if ids else ref, "ci": p.get("ci") or "none"})
        return out
    except Exception:
        return []


def watching_prs(gh, mute=None):
    """Muted approved PRs (approved_mute) -> Open PRs entries marked "watching": someone else's PR you approved, kept
    in sight without asking anything of you. Any age; it drops off by itself when the PR merges or closes (the
    approved search only holds open PRs). Never raises."""
    try:
        if not gh or not isinstance(gh.get("approved"), list):
            return []
        muted = _muted(mute)
        out = []
        for p in gh["approved"]:
            ref = (str(p.get("repo")) + "#" + str(p.get("number"))).lower()
            if ref in muted:
                out.append({"repo": p.get("repo"), "number": p.get("number"), "title": p.get("title") or "",
                            "ci": p.get("ci") or "none", "review": "", "watching": True,
                            "merge": p.get("merge") or ""})
        return out
    except Exception:
        return []


# --- the merge ------------------------------------------------------------------------------------------------------
def merge(block, gh, auto=(), watching=()):
    """The parsed block + a GitHub snapshot (+ the automatic waiting lines, + the watched PRs) -> the API payload's
    groups. Order in Waiting on you: the hand W lines, the automatic lines, then the green PRs. Open PRs: your other
    PRs, then the watched ones."""
    waiting = [dict(w) for w in block["waiting"]] + [dict(w) for w in auto or ()]
    by_ref = {w["ref"]: w for w in waiting if w.get("ref")}
    prs_out = []
    for p in gh["prs"]:
        ref = (p["repo"] + "#" + str(p["number"])).lower()
        mark = {"ci": p["ci"], "review": p["review"]}
        if ref in by_ref:                        # the notes names it: show it once, with the PR's marks
            by_ref[ref].update(mark)
            continue
        if p["ci"] == "passing":
            waiting.append({"text": clean("merge %s#%d: %s" % (p["repo"], p["number"], p["title"])),
                            "source": "github", "ref": ref, **mark})
        else:
            prs_out.append({"repo": p["repo"], "number": p["number"], "title": p["title"], **mark})
    prs_out += [dict(w) for w in watching or ()]
    for w in waiting:
        w.pop("ref", None)
    # the real counts: W lines past the cap still count
    counts = block.get("counts") or {}
    hidden_w = max(0, counts.get("waiting", 0) - len(block["waiting"]))
    return {"waiting": waiting, "running": block["running"], "finished": block["finished"], "prs": prs_out,
            "counts": {"waiting": len(waiting) + hidden_w,
                       "running": max(counts.get("running", 0), len(block["running"])),
                       "finished": max(counts.get("finished", 0), len(block["finished"])),
                       "prs": len(prs_out)}}


# --- the harness's own record of agents (agents.py), merged with the hand R/F lines -------------------
SAME_START_S = 5 * 60
_WORK_TAG = re.compile(r"\[\s*work\s*\]", re.I)
_WORDS = re.compile(r"[a-z0-9]+")


def _norm(text):
    """Lower-case words without ticket ids, tags and times: what two descriptions of one agent share."""
    text = re.sub(r"\[\s*work\s*\]", " ", (text or "").lower())
    text = tasks._LEAD_IDS.sub(" ", text, count=1)
    return " ".join(_WORDS.findall(text))


def same_agent(hand, h):
    """True when a hand R/F line describes the harness entry `h`: one normalised text contains the other (at least
    12 characters), or both name the same ticket and their times are within 5 minutes."""
    a, b = _norm(hand.get("text")), _norm(h.get("text"))
    if a and b and min(len(a), len(b)) >= 12 and (a in b or b in a):
        return True
    shared = tasks.ticket_ids(hand.get("text", "")) & tasks.ticket_ids(h.get("text", ""))
    return bool(shared) and bool(hand.get("ts")) and bool(h.get("ts")) and abs(hand["ts"] - h["ts"]) <= SAME_START_S


def merge_agents(block, harness):
    """The parsed hand block + agents.view() -> a block whose running/finished come from both: the harness entries
    are authoritative for agents; a hand R or F line stays unless it describes an agent the harness already lists
    (running or finished: a hand "R." for an agent the harness saw finish is dropped, not shown running). W lines
    are untouched (only the notes file's writer adds those). Hand lines count only from a current block (state "ok").
    r_all/f_all (uncapped) feed the task list's flags and state lines."""
    out = dict(block)
    hr = [dict(x) for x in (harness or {}).get("running") or []]
    hf = [dict(x) for x in (harness or {}).get("finished") or []]
    every = hr + hf
    ok = block.get("state") == "ok"
    hand_r = [dict(x, source="notes") for x in (block.get("r_all") or [])] if ok else []
    hand_f = [dict(x, source="notes") for x in (block.get("f_all") or [])] if ok else []
    hand_r = [x for x in hand_r if not any(same_agent(x, h) for h in every)]
    hand_f = [x for x in hand_f if not any(same_agent(x, h) for h in every)]
    for x in hand_r + hand_f:                          # the "[work]" tag is for the hook's rule, not for display
        x["text"] = re.sub(r"\s+", " ", _WORK_TAG.sub(" ", x["text"])).strip(" -:·") or x["text"]
    running = hr + hand_r
    finished = sorted(hf + hand_f, key=lambda x: (x.get("ts") is None, -(x.get("ts") or 0)))
    for x in running + finished:
        x.setdefault("clock", datetime.fromtimestamp(x["ts"], TZ).strftime("%H:%M") if x.get("ts") else "")
    out["r_all"], out["f_all"] = running, finished
    out["running"] = running[:ITEMS_MAX]
    out["finished"] = finished[:FINISHED_MAX]
    counts = dict(block.get("counts") or {})
    counts["running"], counts["finished"] = len(running), len(finished)
    out["counts"] = counts
    out["harness_hidden"] = (harness or {}).get("hidden", 0)
    return out


EMPTY = {"waiting": [], "running": [], "finished": [], "prs": [], "notes": "missing", "block_at": None,
         "github": "off", "github_at": None, "agents": "off",
         "counts": {"waiting": 0, "running": 0, "finished": 0, "prs": 0}}
HIDDEN = dict(EMPTY, show=False, notes="hidden")     # outside work hours: nothing from the notes or GitHub


class Source:
    """Caches the notes's lines by (path, mtime, size); merges the GitHub snapshot on every call. Never raises."""

    def __init__(self, notes=None, github=None, now=None, agents=None, prefixes=None, linear=None,
                 ticket_prs=None):
        self.notes, self.github = notes, github
        # the automatic waiting line's sources (auto_waiting): tasks.Linear and derive.TicketPRs, or None (off)
        self.linear, self.ticket_prs = linear, ticket_prs
        self.now = now or (lambda: datetime.now(TZ))
        self.agents = agents              # agents.Reader (the hook-written state file), or None
        self.prefixes = prefixes          # a callable -> the work ticket prefixes (Linear team keys), or None
        self._key, self._lines = None, None
        self._lock = threading.Lock()

    def _read(self):
        path = tasks.notes_path(self.notes)
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
        if key != self._key:
            self._lines = read_tail_lines(path)
            self._key = key
        return self._lines

    def _block(self):
        try:
            lines = self._read()
        except Exception:
            self._key, self._lines, lines = None, None, []
        try:
            return parse(lines or [], self.now())
        except Exception:
            return parse([], self.now())

    def block(self):
        """The parsed notes block alone (no GitHub, no harness). Never raises."""
        with self._lock:
            return self._block()

    def _merged_block(self):
        block = self._block()
        if self.agents is None:
            return dict(block, agents="off")
        try:
            import agents as agents_mod
            prefixes = None
            if self.prefixes is not None:
                try:
                    prefixes = self.prefixes() or None
                except Exception:
                    prefixes = None
            view = agents_mod.view(self.agents.current(), int(self.now().timestamp()), prefixes)
            return dict(merge_agents(block, view), agents="ok")
        except Exception:
            return dict(block, agents="off")

    def _auto(self, block):
        """The automatic waiting lines (auto_waiting); [] when either source is not wired or not answering."""
        if self.linear is None or self.ticket_prs is None:
            return []
        try:
            for src in (self.linear, self.ticket_prs):
                src.maybe_refresh()                  # both rate-limited to one fetch per 5 minutes
            hand_w = block.get("w_all") if block.get("state") == "ok" else []
            return auto_waiting(self.linear.snapshot(), self.ticket_prs.snapshot(), hand_w)
        except Exception:
            return []

    def _approved(self, block, gh):
        """The approved-not-merged lines (auto_approved) from the GitHub snapshot already taken. Never raises."""
        try:
            prefixes = None
            if self.prefixes is not None:
                try:
                    prefixes = self.prefixes() or None
                except Exception:
                    prefixes = None
            hand_w = block.get("w_all") if block.get("state") == "ok" else []
            return auto_approved(gh, hand_w, prefixes, self.now().timestamp())
        except Exception:
            return []

    def flight(self):
        """The hand block merged with the harness's agents: what the task list's flags and state lines are joined
        with (tasks.apply_flight, tasks.derive_states). Never raises."""
        with self._lock:
            return self._merged_block()

    def current(self):
        with self._lock:
            block = self._merged_block()
            gh = {"prs": [], "status": "off", "at": None}
            if self.github is not None:
                try:
                    self.github.maybe_refresh()
                    gh = self.github.snapshot()
                except Exception:
                    pass
            auto = self._auto(block) + self._approved(block, gh)
            try:
                groups = merge(block, gh, auto, watching_prs(gh))
            except Exception:
                groups = merge(block, {"prs": []}, auto)
            return dict(groups, notes=block["state"], block_at=block["at"], github=gh["status"],
                        github_at=gh.get("at"), agents=block.get("agents", "off"))


def read_tail_lines(path, start=tasks.TAIL_START):
    """Lines from the end of the file, enough to contain the newest IN FLIGHT block (whole file at worst)."""
    size = os.path.getsize(path)
    want = start
    with open(path, "rb") as f:
        while True:
            off = max(0, size - want)
            f.seek(off)
            lines = f.read(size - off).decode("utf-8", errors="replace").split("\n")
            if off > 0:
                lines = lines[1:]
            if off == 0 or find_block(lines)[0] is not None:
                return lines
            want *= 2


def payload(source, now=None, force=False):
    """The /api/inflight JSON (a dict). Outside work hours (tasks.shown) HIDDEN, unless `force` (the remote's
    "inflight" panel, decided on the server); the source is not touched then. Never raises."""
    if not tasks.shown(now, force):
        return dict(HIDDEN, now=int(time.time()))
    try:
        data = source.current()
    except Exception:
        data = dict(EMPTY)
    data["show"] = True
    data["now"] = int(time.time())
    return data
