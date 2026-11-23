"""The work task list, from Linear and the notes file's WORK TASKS block (read-only), built on the server.

Shown in work hours only (settings `time`: days, start, end, timezone); outside them the API gives an empty list (the
hours are decided here, on the server clock, not by the browser).

Source A, the notes file (settings `notes.path`, the sketchbook). The NEWEST block whose first line is a work list
heading (settings `notes.tasks_heading`, default "WORK TASKS", also "<NAME>'S WORK TASKS"; the words followed by
"(", ":" or the end of the line, not in quotes, so a line that only mentions such a block does not count). Its items
are the indented lines that start with a number and a dot ("  3. OPS-573 title: state"); the block stops at the first
line that is not indented (the next entry of the file). An "off my plate" line is skipped. The file can be large:
it is read tail-first and parsed only when its mtime or size changes.

Each item is rendered short: the ticket id(s), the title (text before the first ": "), and a short state (the first
clause after it, cut to about 70 characters). URLs and backticks are stripped. An item whose state is just "DONE" is
done and not shown.

Source B, Linear: the open issues assigned to the key's user in every configured workspace are the source of truth;
the notes block becomes OVERRIDES (see `merge`). `Linear` fetches them at most every 5 minutes, in a background
thread, only while the list is being shown. Each workspace's read-only key comes from its `key_cmd` (a command that
prints it), is held in memory only and is never logged. The last good answer is saved by diskcache.py (no secrets).
Each issue also carries the GitHub PR links among its Linear attachments ("prs"), read only for the automatic
"Waiting on you" line (inflight.auto_waiting).

No word filter on the task list: the board is meant for a private office. Every other rule holds: Linear titles are
untrusted text, cleaned, cut and only ever shown as text; the keys never reach a log or the output; the board never
raises. If the notes file or its block is missing or unreadable only the Linear issues are shown (and nothing when
Linear is off too).

Numbering: after sorting, the shown items are numbered 1..N in display order, with no gaps (the notes' own numbers
only set the notes order). A grouped row (`group_item`: a line whose title names several tickets cycles through its
open ones on the page) takes one number per ticket, and "total" counts tickets.
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import config

_C = config.get()
TZ = ZoneInfo(_C["time"]["timezone"])
HOME = os.path.expanduser("~")
HOURS = (config.hm(_C["time"]["work_start"]), config.hm(_C["time"]["work_end"]))   # start inclusive, end exclusive
WORKDAYS = tuple(_C["time"]["work_days"])
ALWAYS = bool(_C["time"]["always_show"])
STATE_MAX = 70
TITLE_MAX = 70
TAIL_START = 256 * 1024          # first tail read; doubled until a block is found or the whole file is read
# test-only: show the list (and the In flight panel) at any hour. Off by default and never set in the unit.
FORCE_ENV = "EASEL_TASKS_ANYTIME"
EMPTY = {"show": False, "items": [], "total": 0, "done": 0}

_HEADING = re.escape(_C["notes"]["tasks_heading"].strip().upper())
_HEADER = re.compile(r"""(?<!["'“‘])(?:[A-Z][A-Za-z]*'S\s+)?%s(?!["'”’])\s*(?:\(|:|$)""" % _HEADING)
_ITEM = re.compile(r"^\s+(\d{1,3})\.\s+(.*\S)\s*$")
_OFF = re.compile(r"off\s+(?:my|[a-z]+'?s)\s+plate", re.I)
_URL = re.compile(r"(?:https?|ftp)://\S+|\bwww\.\S+", re.I)
# "[P1]" (or P2, P3) right after the item number sets its priority: P1 now, P2 this week, P3 later. Higher first,
# same level in list order, done items last.
_PRIO = re.compile(r"^\[\s*P([1-3])\s*\]\s*", re.I)
_IDS = re.compile(r"^([A-Z][A-Z0-9]*-\d+(?:\s*[,/&]\s*(?:[A-Z][A-Z0-9]*-)?\d+)*)\b\s*")
_CLAUSE_END = re.compile(r";|\.(?:\s|$)|\s[—–]\s")


def in_hours(now):
    """True inside the configured work hours on a work day (local time of the configured timezone)."""
    local = now.astimezone(TZ)
    minutes = local.hour * 60 + local.minute
    return (local.weekday() in WORKDAYS
            and HOURS[0][0] * 60 + HOURS[0][1] <= minutes < HOURS[1][0] * 60 + HOURS[1][1])


def shown(now=None, force=False):
    """The one work-hours rule for the screen's work panels (this task list and inflight.py's In flight panel):
    in work hours, or `force` (the remote chose that panel; decided on the server), or `time.always_show`, or the
    test-only FORCE_ENV."""
    return bool(force or ALWAYS or in_hours(now or datetime.now(TZ)) or os.environ.get(FORCE_ENV) == "1")


def notes_path(path=None):
    """The notes file: `path` when given (tests), else settings `notes.path`. Raises when none is set."""
    path = path or config.get()["notes"]["path"]
    if not path:
        raise FileNotFoundError("notes.path is not set")
    return os.path.expanduser(path)


# a block header: "- YYYY-MM-DD HH:MM" then an optional timezone label ("UTC", "CET", "PST"); the time is read in the
# configured timezone whatever the label says
_BLOCK_AT = re.compile(r"^- (\d{4}-\d{2}-\d{2}) (\d{1,2}):(\d{2})(?:\s+[A-Z]{1,5})?\b")


def block_time(lines):
    """When the newest task block was written: its header's "- YYYY-MM-DD HH:MM [TZ]" (local) as epoch seconds, or
    None (no block, or a header without a time: then the hand text counts as old)."""
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        if line and not line[0].isspace() and _HEADER.search(line):
            m = _BLOCK_AT.match(line)
            if not m:
                return None
            try:
                d = datetime.strptime(m.group(1), "%Y-%m-%d")
                return int(datetime(d.year, d.month, d.day, int(m.group(2)), int(m.group(3)), tzinfo=TZ).timestamp())
            except ValueError:
                return None
    return None


def find_block(lines):
    """The item lines (raw) of the newest task block, or None when there is no block."""
    start = None
    for i in range(len(lines) - 1, -1, -1):
        line = lines[i]
        if line and not line[0].isspace() and _HEADER.search(line):
            start = i
            break
    if start is None:
        return None
    items = []
    for line in lines[start + 1:]:
        if not line.strip() or not line[0].isspace():
            break
        if _OFF.search(line):
            continue
        if _ITEM.match(line):
            items.append(line)
    return items


# An item blocked on someone or something else carries "WAITING ON: <who or what>" in its line, ended by ". ", ";" or
# the end. The screen shows it as its own state. 
_WAITING = re.compile(r"\s*\bWAITING ON:\s*([^;]+?)\s*(?:;|\.(?:\s|$)|$)", re.I)
WAIT_MAX = 48
# "IN PROGRESS: <who>" marks an item being worked on now (you, the assistant, a background agent). Shown in green.
# 
_ACTIVE = re.compile(r"\s*\bIN PROGRESS:\s*([^;]+?)\s*(?:;|\.(?:\s|$)|$)", re.I)


def _clean(text):
    text = _URL.sub("", text).replace("`", "")
    text = re.sub(r"\*\*|__", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _cut(text, limit):
    text = text.strip().rstrip(".,;:")
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    if " " in cut[limit // 2:]:
        cut = cut[:cut.rindex(" ")]
    if cut.count("(") > cut.count(")"):          # do not end inside a bracket: "... 11-18 (staging…"
        cut = cut[:cut.rindex("(")]
    return cut.rstrip(" .,;:(-—–") + "…"


def render(line):
    """One raw item line -> {"n", "ids", "title", "state", "done", "full"} ("full", the whole cleaned line, is used
    to find every ticket id the line names; it never reaches the output)."""
    m = _ITEM.match(line)
    n, body = m.group(1), _clean(m.group(2))
    prio = None
    mp = _PRIO.match(body)
    if mp:
        prio = int(mp.group(1))
        body = body[mp.end():]
    full = body
    waiting = ""
    active = ""
    ma = _ACTIVE.search(body)
    if ma:
        active = _cut(ma.group(1), WAIT_MAX)
        body = (body[:ma.start()] + body[ma.end():]).strip()
    mw = _WAITING.search(body)                 # "WAITING ON: Priya (credential upload)." marks a blocked item
    if mw:
        waiting = _cut(mw.group(1), WAIT_MAX)
        body = (body[:mw.start()] + body[mw.end():]).strip()
    colon = re.search(r":(?:\s|$)", body)     # not the colon of a time (08:30)
    title, rest = (body[:colon.start()], body[colon.end():]) if colon else (body, "")
    title, rest = title.strip(), rest.strip()
    tids = ordered_ids(title)                 # every id the title names, in order (a grouped row when 2 or more)
    label = _cut(strip_ids(title), TITLE_MAX)
    ids = ""
    mi = _IDS.match(title)
    if mi:
        ids = re.sub(r"\s+", " ", mi.group(1)).strip()
        title = title[mi.end():].strip()
    ce = _CLAUSE_END.search(rest)
    state = rest[:ce.start()] if ce else rest
    done = bool(re.fullmatch(r"DONE(?:\s*\([^)]*\))?[\s.!]*", state.strip()))
    return {"n": int(n), "ids": ids, "title": _cut(title, TITLE_MAX), "state": _cut(state, STATE_MAX), "done": done,
            "waiting": "" if done else waiting, "active": "" if done else active, "prio": prio, "full": full,
            "tids": tids, "label": label}


# --- Linear merge --------------------------------------------------------------------------------
# Every ticket id an item line names: "ACME-2410 follow-ons ACME-2420, ACME-4726" and the shorthand "ACME-4892/4893".
_ANY_IDS = re.compile(r"\b([A-Z][A-Z0-9]*)-(\d+)((?:\s*[,/&]\s*\d+\b)*)")
_IDENT = re.compile(r"^([A-Z][A-Z0-9]*)-\d{1,7}$")
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2066-\u2069]")
LINEAR_PRIO = {1: 1, 2: 2}       # Urgent -> P1, High -> P2; Medium, Low and No priority -> P3


def all_ids(text):
    """The set of ticket ids named anywhere in a piece of text, shorthand expanded."""
    out = set()
    for m in _ANY_IDS.finditer(text or ""):
        out.add(m.group(1) + "-" + m.group(2))
        for num in re.findall(r"\d+", m.group(3)):
            out.add(m.group(1) + "-" + num)
    return out


def ordered_ids(text):
    """The ticket ids a piece of text names, in order, shorthand expanded, each once: "ACME-4892/4893/4894" and
    "ACME-1, ACME-2" both give every id."""
    out = []
    for m in _ANY_IDS.finditer(text or ""):
        for num in [m.group(2)] + re.findall(r"\d+", m.group(3)):
            ident = m.group(1) + "-" + num
            if ident not in out:
                out.append(ident)
    return out


def strip_ids(text):
    """The text without its ticket ids and the separators left between them ("Acme CI speed ACME-4892/4893" ->
    "Acme CI speed")."""
    text = _ANY_IDS.sub(" ", text or "")
    text = re.sub(r"(?:(?<=\s)|^)[,/&](?=\s|$)", " ", text)
    return re.sub(r"\s+", " ", text).strip(" ,/&:;-—–")


def shorthand(idents):
    """["ACME-4892", "ACME-4893", "OPS-1"] -> "ACME-4892/4893/OPS-1" (the prefix is dropped while it repeats)."""
    out, prev = [], None
    for ident in idents:
        prefix, _, num = ident.partition("-")
        out.append(num if prefix == prev else ident)
        prev = prefix
    return "/".join(out)


def item_key(item):
    """The notes item's key: the FIRST id, only when the item starts with an id (after "[Px]")."""
    m = _ANY_IDS.match(item.get("ids") or "")
    return m.group(1) + "-" + m.group(2) if m else None


def ticket_owner(ident, workspaces, missing=()):
    """Which configured workspace decides about a ticket id: (name of an answered workspace whose teams include the
    id's prefix, or None; True when some configured workspace has not answered, so the id may still be its ticket).
    `workspaces` is a snapshot's "ws" (answered ones only), `missing` the names of those that have not answered."""
    prefix = ident.split("-")[0]
    for name in sorted(workspaces):
        if prefix in (workspaces[name].get("teams") or ()):
            return name, bool(missing)
    return None, bool(missing)


def _id_sort(ident):
    prefix, _, num = ident.partition("-")
    return (prefix, int(num) if num.isdigit() else 0)


def _sort_key(it):
    linear = it.get("source") == "linear"
    return (it.get("prio") or 9, 1 if linear else 0, it.get("_order", 0), _id_sort(it["ids"]) if linear else ("", 0))


def linear_item(issue):
    """One open Linear issue (already validated) -> an item like render()'s. The title is untrusted: cleaned, cut,
    and only ever displayed (as text), never interpreted."""
    title = _cut(_CTRL.sub(" ", _clean(issue["title"])), TITLE_MAX)
    state = _cut(_CTRL.sub(" ", _clean(issue["state"])), 30)
    prio = LINEAR_PRIO.get(issue["priority"], 3)
    return {"n": 0, "ids": issue["identifier"], "title": title, "state": state, "done": False, "waiting": "",
            "active": "", "prio": prio, "source": "linear",
            "full": issue["identifier"] + " " + issue["title"] + ": " + issue["state"]}


def merge(rendered, snap):
    """The notes's rendered items (overrides) + the open Linear issues -> one list (before sorting).

    - A notes item whose key (first id) is an open Linear issue keeps everything from the notes.
    - A notes item whose key is NOT an open issue is dropped (closed, canceled or no longer assigned), but only when
      the key's workspace was fetched (fresh, or the last good result within the hour) and that workspace has a team
      with the key's prefix; otherwise it stays as written.
    - Items that do not start with an id stay as written.
    - Every id a kept notes line names (done or not) covers that Linear issue, so it never shows twice.
      A line dropped for a closed key covers nothing: the open tickets it names show on their own (a canceled
      parent's line must not hide its open follow-ons).
    - Every open Linear issue not covered shows on its own (`linear_item`), EXCEPT a backlog issue: backlog shows
      only when a notes line names it. A line dropped for a closed key still names its
      other tickets."""
    workspaces = snap.get("ws") or {}
    missing = tuple(snap.get("missing") or ())
    open_issues = {}
    for data in workspaces.values():
        for issue in data["issues"]:
            open_issues[issue["identifier"]] = issue
    covered = set()
    named = set()                               # ids a dropped notes line names: shown even when backlog
    out = []
    for i, it in enumerate(rendered):
        key = item_key(it)
        it = dict(it, source="notes", _order=i)
        grouped = group_item(it, open_issues, workspaces, missing)
        if grouped == "drop":
            named |= all_ids(it.get("full", ""))
            continue
        if grouped is not None:
            covered |= all_ids(it.get("full", ""))
            out.append(grouped)
            continue
        if key and key not in open_issues:
            owner, _ = ticket_owner(key, workspaces, missing)
            if owner is not None:
                named |= all_ids(it.get("full", ""))
                continue
        covered |= all_ids(it.get("full", ""))
        if key:
            covered.add(key)
            if key in open_issues:
                it["_lin"] = open_issues[key]["state"]      # its live Linear state (derive_states)
        out.append(it)
    for ident in sorted(open_issues, key=_id_sort):
        if ident not in covered and (ident in named or not open_issues[ident].get("backlog")):
            out.append(dict(linear_item(open_issues[ident]), _order=len(rendered)))
    return out


def group_item(it, open_issues, workspaces, missing=()):
    """Grouped rows. A notes line whose TITLE (the text before its first ": ") names two or more tickets used to show
    the hand note in place of every ticket's live state (ACME-4896 sat as "parked" while Linear had it In Review).
    Each named ticket is now its own step of the row, with its live Linear title and its own derived state; the hand
    title and note stay on the row as its label. Ids in the note (after the colon) never group a line.

    Returns None (not a group: the usual rules decide), "drop" (every ticket it names is closed), or the item:
    - a ticket open in Linear is a step; one Linear no longer has open drops out (its workspace was fetched and has
      the team); one whose workspace has not answered stays as written; an id whose prefix no team has is not a
      ticket and is ignored;
    - with no ticket known open in Linear the usual rules decide (exactly as before grouping existed);
    - one step left: a normal notes row for that ticket (hand title without the ids, hand note, live state);
    - two or more: "_group" holds the steps; "ids" lists them in shorthand ("ACME-4892/4893/4894")."""
    tickets, known_open = [], False
    candidates = 0
    for ident in it.get("tids") or []:
        if ident in open_issues:
            candidates += 1
            known_open = True
            tickets.append(ident)
            continue
        owner, unanswered = ticket_owner(ident, workspaces, missing)
        if owner is not None:
            candidates += 1                         # closed, canceled or no longer assigned: drops out
        elif unanswered or not workspaces:
            candidates += 1
            tickets.append(ident)                   # its workspace may not have answered: kept as written
    if candidates < 2:
        return None
    if not tickets:
        return "drop"
    if not known_open:
        return None
    title = it.get("label") or it.get("title", "")
    if len(tickets) == 1:
        one = tickets[0]
        if not it.get("label") and one in open_issues:
            title = linear_item(open_issues[one])["title"]
        out = dict(it, ids=one, title=title)
        if one in open_issues:
            out["_lin"] = open_issues[one]["state"]
        return out
    steps = []
    for ident in tickets:
        issue = open_issues.get(ident)
        li = linear_item(issue) if issue else None
        steps.append({"id": ident, "title": li["title"] if li else "", "lin": li["state"] if li else ""})
    return dict(it, ids=shorthand(tickets), title=title, _group=steps)


DERIVED_MAX = 90
GROUP_STATE_MAX = 70
_INTERNAL = ("full", "_order", "_lin", "_group", "tids", "label")


def derive_states(items, facts, hand_at):
    """Each ticket task's state line from Linear, its PRs and the In flight finished lines (derive.py has the rules
    and the order of trust). `facts`: {"prs": TicketPRs.snapshot()["prs"], "finished": [{"text", "ts"}], "now"}.
    Items that do not start with a ticket id keep their hand text exactly as before."""
    import derive
    now = facts.get("now") or int(time.time())
    out = []
    for it in items:
        if it.get("_group"):
            # a grouped row: each step's own state from its own ticket's facts; the hand note is the row's label
            steps = []
            for s in it["_group"]:
                one = {s["id"]}
                text, src = derive.state_line(s["lin"], derive.pr_text(derive.prs_for(one, facts.get("prs"), now), now),
                                              derive.flight_text(one, facts.get("finished"), now), "", None, now)
                steps.append(dict(s, state=_cut(text, GROUP_STATE_MAX) if text else "", state_src=src))
            out.append(dict(it, _group=steps, state_src="group"))
            continue
        linear = it.get("source") == "linear"
        key = it["ids"] if linear else item_key(it)
        if not key:
            out.append(dict(it, state_src="hand"))
            continue
        ids = ticket_ids(it.get("ids", "")) | {key}
        lin = it["state"] if linear else it.get("_lin", "")
        hand = "" if linear else it.get("state", "")
        text, src = derive.state_line(lin, derive.pr_text(derive.prs_for(ids, facts.get("prs"), now), now),
                                      derive.flight_text(ids, facts.get("finished"), now), hand,
                                      None if linear else hand_at, now)
        out.append(dict(it, state=_cut(text, DERIVED_MAX) if text else "", state_src=src))
    return out


def build(lines, linear=None, facts=None):
    """Lines of the notes (and a `Linear.snapshot()`, or None for the notes alone) -> the API payload (without
    the hours decision). With `facts` (the live path, `Source`) each ticket task's state line is derived
    (`derive_states`); without it the state lines are the hand text, as before."""
    raw = find_block(lines) or []
    if not raw and not (linear and linear.get("ws")):
        return dict(EMPTY)
    rendered = [render(x) for x in raw]
    if linear is not None:
        rendered = merge(rendered, linear)
    if facts is not None:
        rendered = derive_states(rendered, facts, block_time(lines))
    # Done items are not shown. Then P1, P2, P3, untagged; inside a
    # level the notes's list order, then Linear-only issues by identifier.
    done = sum(1 for it in rendered if it["done"])
    items = sorted((it for it in rendered if not it["done"]), key=_sort_key)
    # Numbered 1..N in display order, no gaps. A grouped row
    # takes one number per ticket (its "n" is the first; each step has its own), so the last number is the total.
    out, n = [], 0
    for it in items:
        row = {k: v for k, v in it.items() if k not in _INTERNAL}
        steps = it.get("_group")
        row["n"] = n + 1
        if steps:
            row["group"] = {"title": it.get("title", ""), "note": it.get("state", ""),
                            "tickets": [{"n": n + j, "id": s["id"], "title": s["title"],
                                         "state": s.get("state", s["lin"]), "state_src": s.get("state_src", "")}
                                        for j, s in enumerate(steps, 1)]}
        n += len(steps) if steps else 1
        out.append(row)
    items = out
    # "total" counts tickets (a grouped row counts each open ticket it cycles through); "rows" counts rows
    return {"show": bool(items), "items": items, "total": n, "rows": len(items),
            "done": done, "waiting": sum(1 for it in items if it.get("waiting")),
            "active": sum(1 for it in items if it.get("active") and not it.get("waiting"))}


# --- waiting / running flags from the In flight block ----------------------------------------------
# The hand-written task block goes stale; the In flight block is kept current all day. So with a CURRENT block (inflight.parse
# state "ok", under 18 hours old):
#   - a task is waiting on you when a W line names one of its ticket ids (whole token, any case; OPS-57 is not
#     OPS-577); the flag shows that W line's text (the leading id dropped), "+N more" when several lines name it;
#   - a task is running now when an R line names one of its ids; the flag shows that R line's text;
#   - the hand "WAITING ON: <who>" stays only when <who> is someone other than you ("WAITING ON: Priya"); a hand
#     "WAITING ON: <you>" with no W line is stale by definition and not shown; a hand "IN PROGRESS: ..." with no R line
#     is not shown either (the R lines are the running work);
#   - a W or R line that names no ticket on the list stays only in the In flight panel.
# With a stale or missing block nothing comes from it and the hand text decides exactly as before.
_TOKEN_IDS = re.compile(r"(?<![A-Z0-9_])([A-Z][A-Z0-9]*)-(\d+)(?![A-Z0-9_])((?:\s*[,/&]\s*\d+(?![A-Z0-9_]))*)")
def _owner_re():
    names = [n for n in [_C["branding"]["owner"]] + list(_C["branding"]["owner_aliases"]) if n and n.strip()]
    alts = "|".join(re.escape(n.strip()) + r"(?:'s)?" for n in names) or "you"
    return re.compile(r"^\W*(?:%s)\b" % alts, re.I)


_OWNER = _owner_re()
_LEAD_IDS = re.compile(r"^\s*[A-Za-z][A-Za-z0-9]*-\d+(?:\s*[,/&]\s*(?:[A-Za-z][A-Za-z0-9]*-)?\d+)*\s*(?::|[—–]|-\s)?\s*")
FLAG_MAX = 90


def ticket_ids(text):
    """Every ticket id named in a piece of text as a whole token, case-insensitive, upper-cased; shorthand
    ("ACME-4892/4893") expanded. "OPS-577" names OPS-577, never OPS-57; "XOPS-577" and "OPS-577x" name nothing."""
    out = set()
    for m in _TOKEN_IDS.finditer((text or "").upper()):
        out.add(m.group(1) + "-" + m.group(2))
        for num in re.findall(r"\d+", m.group(3)):
            out.add(m.group(1) + "-" + num)
    return out


def is_owner(who):
    """True when a hand "WAITING ON: <who>" names the owner (settings `branding.owner` or one of `owner_aliases`: "you", "me")."""
    return bool(_OWNER.match(who or ""))


def _flag_text(texts):
    """The shown flag: the first line's text without its leading ticket id(s), "+N more" for the rest."""
    first = _LEAD_IDS.sub("", texts[0], count=1).strip() or texts[0]
    more = " · +%d more" % (len(texts) - 1) if len(texts) > 1 else ""
    return _cut(first, FLAG_MAX - len(more)) + more


def apply_flight(data, block):
    """The task payload + a parsed In flight block (inflight.parse, or None) -> the payload with the waiting/running
    flags decided (rules above). Adds "flags": "inflight" (a current block decided) or "hand" (stale or missing:
    the hand text, as before), and per item "waiting_text" and "running". Never raises on odd input."""
    data = dict(data)
    items = [dict(it) for it in data.get("items") or []]
    current = bool(block) and block.get("state") == "ok"
    w_by, r_by = {}, {}
    if current:
        for text in block.get("w_all") or [x.get("text", "") for x in block.get("waiting") or []]:
            for ident in ticket_ids(text):
                w_by.setdefault(ident, []).append(text)
    # running: the merged R list (the harness's own record of running agents, plus the hand R lines of a current
    # block, inflight.merge_agents); the harness entries count even when the hand block is stale
    runs = (block or {}).get("r_all")
    if runs is None and current:
        runs = (block or {}).get("running") or []
    for r in runs or []:
        if current or r.get("source") == "harness":
            for ident in ticket_ids(r.get("text", "")):
                r_by.setdefault(ident, []).append(r.get("text", ""))
    for it in items:
        it.setdefault("waiting", "")
        it.setdefault("active", "")
        it["waiting_text"], it["running"] = "", False
        if it.get("done"):
            continue
        if isinstance(it.get("group"), dict):
            # each step of a grouped row carries only the W and R lines naming its own ticket
            g = dict(it["group"])
            steps = []
            for s in g.get("tickets") or []:
                s = dict(s)
                sw = list(dict.fromkeys(w_by.get(s.get("id"), [])))
                sr = list(dict.fromkeys(r_by.get(s.get("id"), [])))
                s["waiting_text"] = _flag_text(sw) if sw else ""
                s["running_text"] = _flag_text(sr) if sr else ""
                steps.append(s)
            g["tickets"] = steps
            it["group"] = g
        ids = ticket_ids(it.get("ids", ""))
        r = list(dict.fromkeys(t for ident in sorted(ids) for t in r_by.get(ident, [])))
        if not current:
            if r:
                it["active"], it["running"] = _flag_text(r), True
            continue
        w = [t for ident in sorted(ids) for t in w_by.get(ident, [])]
        w = list(dict.fromkeys(w))                                  # one line naming two of its ids counts once
        if w:
            it["waiting"], it["waiting_text"] = _C["branding"]["owner"] or "you", _flag_text(w)
        elif it["waiting"] and is_owner(it["waiting"]):
            it["waiting"] = ""                                     # stale: no current W line says so
        it["active"], it["running"] = (_flag_text(r), True) if r else ("", False)
    data["items"] = items
    data["flags"] = "inflight" if current else "hand"
    data["waiting"] = sum(1 for it in items if it.get("waiting") and not it.get("done"))
    data["active"] = sum(1 for it in items if it.get("active") and not it.get("waiting") and not it.get("done"))
    return data


def read_tail_lines(path, start=TAIL_START):
    """Lines from the end of the file, enough to contain the newest task block (whole file at worst)."""
    size = os.path.getsize(path)
    want = start
    with open(path, "rb") as f:
        while True:
            off = max(0, size - want)
            f.seek(off)
            data = f.read(size - off)
            text = data.decode("utf-8", errors="replace")
            lines = text.split("\n")
            if off > 0:
                lines = lines[1:]          # the first line is probably cut
            if off == 0 or find_block(lines) is not None:
                return lines
            want *= 2


# --- Linear fetch -------------------------------------------------------------------------------------------------
LINEAR_URL = "https://api.linear.app/graphql"
# ((name, key_cmd), ...) from settings `linear.workspaces`
WORKSPACES = config.workspaces()
FETCH_EVERY_S = 300              # at most one fetch per 5 minutes
# the last good result (memory, and on disk across restarts: diskcache.py) is used for up to 7 days after it was
# fetched, always shown with its age when a newer fetch has failed ("Linear as of HH:MM"); dropping it sooner
# would make the list silently shrink to the hand-written items
GOOD_FOR_S = 7 * 24 * 3600
LINEAR_CACHE = "linear-last-good.json"
KEY_RETRY_S = 900                # a missing key is looked for again after 15 minutes
LOG_EVERY_S = 3600               # one journal line per hour at most
MAX_PAGES = 5
MAX_BODY = 2 * 1024 * 1024
# Linear state types that mean closed. "duplicate" is its own type.
CLOSED_TYPES = ("completed", "canceled", "duplicate")
QUERY = """query($after: String) {
  viewer { assignedIssues(filter: {state: {type: {nin: ["completed", "canceled", "duplicate"]}}}, first: 100, after: $after) {
    nodes { identifier title priority state { name type } attachments(first: 25) { nodes { url } } }
    pageInfo { hasNextPage endCursor } }
    done: assignedIssues(filter: {completedAt: {gte: "-P7D"}}, first: 100) {
    nodes { identifier title completedAt state { type } } } }
  teams(first: 250) { nodes { key } }
}"""
# "done": the key user's issues completed in the last 7 days, read from the
# first page's answer only. Missing or malformed: None (unknown), and the box says Linear has not answered.
DONE_MAX = 100


def _epoch_iso(value):
    """Linear's "2026-01-07T19:14:02.123Z" -> epoch seconds, or None."""
    if not isinstance(value, str):
        return None
    try:
        return int(datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return None


def parse_done(doc):
    """One GraphQL response -> [{"identifier", "title", "completed_at"}] of the completed issues, or None when the
    answer has no well-formed "done" field. Never raises."""
    try:
        nodes = doc["data"]["viewer"]["done"]["nodes"]
        if not isinstance(nodes, list):
            return None
    except (KeyError, TypeError):
        return None
    out = []
    for node in nodes[:DONE_MAX]:
        if not isinstance(node, dict):
            continue
        ident, title = node.get("identifier"), node.get("title")
        at = _epoch_iso(node.get("completedAt"))
        if (not isinstance(ident, str) or not _IDENT.match(ident) or not isinstance(title, str) or at is None
                or (node.get("state") or {}).get("type") != "completed"):
            continue
        out.append({"identifier": ident, "title": title[:500], "completed_at": at})
    return out


def valid_done(done):
    """A saved "done" list -> the well-formed entries, or None (unknown) when it is not a list."""
    if not isinstance(done, list):
        return None
    return [d for d in done[:DONE_MAX] if isinstance(d, dict) and isinstance(d.get("identifier"), str)
            and _IDENT.match(d["identifier"]) and isinstance(d.get("title"), str)
            and isinstance(d.get("completed_at"), int) and not isinstance(d.get("completed_at"), bool)]


def cmd_key(cmd):
    """A Linear key printed by the workspace's `key_cmd` (a list: the program and its arguments, never a shell), or
    None. The key is checked for shape; its output never reaches a log, stderr is dropped."""
    try:
        r = subprocess.run(list(cmd), capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    except Exception:
        return None
    key = r.stdout.strip() if r.returncode == 0 else ""
    if not key or len(key) > 400 or any(c.isspace() or ord(c) < 32 for c in key):
        return None
    return key


def http_post(key, body, timeout=20):
    """POST one GraphQL request; urllib honours HTTPS_PROXY. The key goes only in Authorization."""
    req = urllib.request.Request(LINEAR_URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"Authorization": key, "Content-Type": "application/json",
                                          "User-Agent": "easel"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read(MAX_BODY + 1)
    if len(data) > MAX_BODY:
        raise ValueError("response too large")
    return json.loads(data)


def is_backlog(state):
    """True for a Linear state that is backlog by type or by name. Backlog issues are never on the screen unless
    a notes line names them."""
    name = state.get("name")
    return state.get("type") == "backlog" or (isinstance(name, str) and name.strip().lower() == "backlog")


# A GitHub pull request link among an issue's Linear attachments (the GitHub integration adds one per linked PR):
# "https://github.com/acme-inc/acme/pull/123" -> "acme-inc/acme#123" (lower-cased). Used only for the automatic
# "Waiting on you" line (inflight.auto_waiting); never shown.
_PR_URL = re.compile(r"^https://github\.com/([A-Za-z0-9_.-]{1,100})/([A-Za-z0-9_.-]{1,100})/pull/(\d{1,7})(?:[/?#].*)?$")
ATTACH_MAX = 25


def pr_refs(attachments):
    """An issue's `attachments` connection -> the sorted "owner/repo#n" of its GitHub PR links, or None when the
    field is missing or malformed (unknown: the automatic waiting line is then never added for that issue)."""
    nodes = (attachments or {}).get("nodes") if isinstance(attachments, dict) else None
    if not isinstance(nodes, list):
        return None
    refs = set()
    for node in nodes[:ATTACH_MAX]:
        url = node.get("url") if isinstance(node, dict) else None
        m = _PR_URL.match(url) if isinstance(url, str) else None
        if m:
            refs.add(("%s/%s#%d" % (m.group(1), m.group(2), int(m.group(3)))).lower())
    return sorted(refs)


def valid_refs(refs):
    """A saved "prs" list -> the same list when every entry is a well-formed ref, else None (unknown)."""
    if not isinstance(refs, list) or len(refs) > ATTACH_MAX:
        return None
    ok = all(isinstance(r, str) and re.fullmatch(r"[a-z0-9_.-]{1,100}/[a-z0-9_.-]{1,100}#\d{1,7}", r) for r in refs)
    return list(refs) if ok else None


def parse_page(doc):
    """One GraphQL response -> (issues, team keys, next cursor or None). Raises on anything unexpected.
    Everything is untrusted: only well-formed fields are kept."""
    if not isinstance(doc, dict) or doc.get("errors") or not isinstance(doc.get("data"), dict):
        raise ValueError("graphql errors")
    conn = doc["data"]["viewer"]["assignedIssues"]
    issues = []
    for node in conn["nodes"]:
        try:
            ident, title = node["identifier"], node["title"]
            state = node["state"] or {}
            if (not isinstance(ident, str) or not _IDENT.match(ident) or not isinstance(title, str)
                    or state.get("type") in CLOSED_TYPES):
                continue
            prio = node.get("priority")
            issues.append({"identifier": ident, "title": title[:500],
                           "priority": prio if isinstance(prio, int) and 0 <= prio <= 4 else 0,
                           "state": state.get("name") if isinstance(state.get("name"), str) else "",
                           "backlog": is_backlog(state), "prs": pr_refs(node.get("attachments"))})
        except (KeyError, TypeError, AttributeError):
            continue
    teams = {t["key"] for t in (doc["data"].get("teams") or {}).get("nodes") or []
             if isinstance(t, dict) and isinstance(t.get("key"), str)}
    info = conn.get("pageInfo") or {}
    nxt = info.get("endCursor") if info.get("hasNextPage") else None
    return issues, teams, nxt if isinstance(nxt, str) else None


class Linear:
    """The key user's open assigned issues in every workspace, cached in memory. `maybe_refresh()` (non-blocking) starts a
    background fetch when the last attempt is 5 minutes old; `snapshot()` gives what may be used now."""

    def __init__(self, get_key=cmd_key, post=http_post, clock=time.monotonic, log=None, workspaces=WORKSPACES,
                 wall=time.time, cache_file=None):
        self.get_key, self.post, self.clock, self.wall = get_key, post, clock, wall
        self.cache_file = cache_file            # where the last good results survive a restart (None: memory only)
        self.log = log or (lambda line: print(line, file=sys.stderr, flush=True))
        self.workspaces = workspaces
        self._lock = threading.Lock()
        self._running = False
        self._attempt = None                    # clock of the last refresh start
        self._logged = None                     # clock of the last journal line
        self._keys = {}                         # ws -> key (memory only)
        self._key_retry = {}                    # ws -> clock when a missing key may be looked for again
        self._good = {}                         # ws -> {"at", "issues", "teams"}
        self._ok = {}                           # ws -> did the last attempt succeed
        self._fail_since = {}                   # ws -> wall clock of the first failure since the last success
        self._load_saved()

    def _load_saved(self):
        """The last good results saved by an earlier run (diskcache.py), with their real age. Malformed: ignored."""
        if not self.cache_file:
            return
        import diskcache
        doc = diskcache.load(self.cache_file) or {}
        now_wall, now = self.wall(), self.clock()
        names = {name for name, _ in self.workspaces}
        for name, g in (doc.get("ws") or {}).items() if isinstance(doc.get("ws"), dict) else ():
            try:
                wall = float(g["wall"])
                issues = [i for i in g["issues"] if isinstance(i, dict) and isinstance(i.get("identifier"), str)
                          and _IDENT.match(i["identifier"]) and isinstance(i.get("title"), str)
                          and isinstance(i.get("state"), str) and isinstance(i.get("priority"), int)]
                teams = {t for t in g["teams"] if isinstance(t, str)}
            except (KeyError, TypeError, ValueError):
                continue
            if name not in names or not 0 <= now_wall - wall < GOOD_FOR_S:
                continue
            for i in issues:
                i["backlog"] = bool(i.get("backlog"))
                i["title"] = i["title"][:500]
                i["prs"] = valid_refs(i.get("prs"))          # an older saved copy has none: unknown (None)
            self._good[name] = {"at": now - (now_wall - wall), "wall": wall, "issues": issues, "teams": teams,
                                "done": valid_done(g.get("done"))}     # an older saved copy has none: unknown

    def _save(self):
        if not self.cache_file:
            return
        import diskcache
        with self._lock:
            doc = {"v": 1, "ws": {name: {"wall": g["wall"], "issues": g["issues"], "teams": sorted(g["teams"]),
                                         "done": g.get("done")}
                                  for name, g in self._good.items()}}
        diskcache.save(self.cache_file, doc)

    def _note(self, line):
        now = self.clock()
        if self._logged is None or now - self._logged >= LOG_EVERY_S:
            self._logged = now
            self.log(line)

    def _key(self, ws, item):
        key = self._keys.get(ws)
        if key:
            return key
        if self.clock() < self._key_retry.get(ws, float("-inf")):
            return None
        try:
            key = self.get_key(item)
        except Exception:
            key = None
        if key:
            self._keys[ws] = key
            return key
        self._key_retry[ws] = self.clock() + KEY_RETRY_S
        return None

    def _fetch(self, key):
        issues, teams, after, done = [], set(), None, None
        for i in range(MAX_PAGES):
            doc = self.post(key, {"query": QUERY, "variables": {"after": after}})
            page, tkeys, after = parse_page(doc)
            if i == 0:
                done = parse_done(doc)
            issues += page
            teams |= tkeys
            if not after:
                break
        return issues, teams, done

    def refresh(self):
        """Fetch both workspaces now (blocking). Never raises."""
        with self._lock:
            self._attempt = self.clock()
        for ws, item in self.workspaces:
            try:
                key = self._key(ws, item)
                if not key:
                    with self._lock:
                        self._ok[ws] = False
                        self._fail_since.setdefault(ws, int(self.wall()))
                    self._note("tasks: linear %s: no key yet, using the notes" % ws)
                    continue
                issues, teams, done = self._fetch(key)
                with self._lock:
                    self._good[ws] = {"at": self.clock(), "wall": self.wall(), "issues": issues, "teams": teams,
                                      "done": done}
                    self._ok[ws] = True
                    self._fail_since.pop(ws, None)
                self._save()
            except Exception as e:
                if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403):
                    self._keys.pop(ws, None)     # a revoked or wrong key: read it again next time
                with self._lock:
                    self._ok[ws] = False
                    self._fail_since.setdefault(ws, int(self.wall()))
                code = getattr(e, "code", "")
                self._note("tasks: linear %s fetch failed (%s%s)" % (ws, type(e).__name__,
                                                                     " %s" % code if isinstance(code, int) else ""))

    def maybe_refresh(self):
        with self._lock:
            if self._running or (self._attempt is not None and self.clock() - self._attempt < FETCH_EVERY_S):
                return
            self._running = True

        def run():
            try:
                self.refresh()
            finally:
                with self._lock:
                    self._running = False
        threading.Thread(target=run, name="linear-fetch", daemon=True).start()

    def snapshot(self):
        """{"ws": {ws: {"issues", "teams", "wall", "done"}} for every workspace with a good result under an hour old,
        "status": "ok" (every workspace fresh), "stale" (some old or missing) or "off" (none usable),
        "at": wall clock of the OLDEST good result in use (the age the screen shows when not "ok"), or None}."""
        now = self.clock()
        with self._lock:
            used = {name: g for name, g in self._good.items() if now - g["at"] < GOOD_FOR_S}
            ws = {name: {"issues": list(g["issues"]), "teams": set(g["teams"]),
                         "wall": int(g["wall"]) if "wall" in g else None,
                         "done": [dict(d) for d in g["done"]] if g.get("done") is not None else None}
                  for name, g in used.items()}
            fresh = all(self._ok.get(name) and name in ws for name, _ in self.workspaces)
            at = min((int(g["wall"]) for g in used.values() if "wall" in g), default=None)
            # a workspace with nothing usable: "loading" (no attempt has finished yet, e.g. just after a start with
            # nothing saved) or "down" (attempts failed; since when). The list is not shown without it.
            missing = {name: ("down", self._fail_since.get(name)) if name in self._ok else ("loading", None)
                       for name, _ in self.workspaces if name not in ws}
        out = {"ws": ws, "status": "ok" if fresh else "stale" if ws else "off", "at": at}
        if missing:
            out["missing"] = missing
        return out

    def team_keys(self):
        """Every team key of the workspaces in use (the work ticket prefixes), or an empty set."""
        keys = set()
        for data in self.snapshot()["ws"].values():
            keys |= data["teams"]
        return keys


class Source:
    """Caches the notes's lines by its (path, mtime, size); with a `Linear`, merges its snapshot on every call."""

    def __init__(self, notes=None, linear=None, flight=None, prs=None, wall=time.time):
        self.notes, self.linear = notes, linear
        # a callable -> the merged In flight block (inflight.Source.flight: hand block + the harness's agents), or None
        self.flight = flight
        self.prs = prs                    # derive.TicketPRs (the owner's recent PRs with the ticket ids they name), or None
        self.wall = wall
        self._key, self._data, self._lines = None, EMPTY, None
        self._lock = threading.Lock()

    def current(self):
        block = None
        if self.flight is not None:
            try:
                block = self.flight()
            except Exception:
                block = None
        data = self._notes_only() if self.linear is None else self._merged(block)
        if self.flight is None:
            return data
        try:
            return apply_flight(data, block)
        except Exception:
            return data

    def _facts(self, block):
        """What the state lines are derived from, besides Linear: the PR snapshot and the finished lines."""
        gh = {"prs": [], "status": "off", "at": None}
        if self.prs is not None:
            try:
                self.prs.maybe_refresh()
                gh = self.prs.snapshot()
            except Exception:
                pass
        finished = list((block or {}).get("f_all") or [])
        return {"prs": gh["prs"], "finished": finished, "now": int(self.wall())}, gh

    def _merged(self, block=None):
        with self._lock:
            try:
                path = notes_path(self.notes)
                st = os.stat(path)
                key = (path, st.st_mtime_ns, st.st_size)
                if key != self._key:
                    self._lines = read_tail_lines(path)
                    self._key = key
            except Exception:
                self._key, self._lines = None, None
            try:
                self.linear.maybe_refresh()
                snap = self.linear.snapshot()
            except Exception:
                snap = {"ws": {}, "status": "off", "at": None}
            facts, gh = self._facts(block)
            try:
                data = build(self._lines or [], snap, facts)
            except Exception:
                try:
                    data = build(self._lines or [], snap)
                except Exception:
                    data = dict(EMPTY)
            data = blocked_if_missing(data, snap)
            # each source's state and the age of what is shown, so a source that stopped answering shows its age
            return dict(data, linear=snap["status"], linear_at=snap.get("at"),
                        github=gh["status"] if self.prs is not None else "off", github_at=gh.get("at"))

    def _notes_only(self):
        with self._lock:
            try:
                path = notes_path(self.notes)
                st = os.stat(path)
                key = (path, st.st_mtime_ns, st.st_size)
                if key != self._key:
                    self._data = build(read_tail_lines(path))
                    self._key = key
                return self._data
            except Exception:
                self._key = None
                return EMPTY


def blocked_if_missing(data, snap):
    """ ("whoa my list just went from 12 to 6 though"): a list without a workspace's Linear issues is
    a wrong picture, so it is never shown. When a workspace has nothing usable (no fetch answered since the start and
    nothing saved, or failing for longer than the saved result lasts) the payload carries no items and says why:
    "blocked": {"why": "loading"|"down", "since": epoch|null, "workspaces": [...]}; "show" stays true so the panel
    says it plainly instead of disappearing."""
    missing = snap.get("missing") or {}
    if not missing:
        return data
    why = "down" if any(w == "down" for w, _ in missing.values()) else "loading"
    since = min((t for w, t in missing.values() if w == "down" and t), default=None)
    return dict(data, show=True, items=[], total=0, rows=0, waiting=0, active=0,
                blocked={"why": why, "since": since, "workspaces": sorted(missing)})


def payload(source, now=None, force=False):
    """The API payload. `force` (the remote's "Tasks" panel, decided on the server) shows it outside work hours;
    the list is exactly the same either way."""
    if not shown(now, force):
        return dict(EMPTY)
    return dict(source.current())
