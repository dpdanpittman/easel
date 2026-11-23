#!/usr/bin/python3 -I
"""Run the kiosk on invented data, with no network and no keys: for screenshots, layout checks and trying it out.

    python3 scripts/demo.py [--port 8099] [--off-hours]

Everything the server would read is faked: two Linear workspaces (Acme and Globex), the gh CLI's answers, the
systemd unit states, a notes file with a WORK TASKS and an IN FLIGHT block, and the background agents record. The
work panels show at any hour. Open http://127.0.0.1:8099/ in a 1920x1080 window, or run scripts/screenshot.py.
Every name, ticket and pull request here is made up.
"""
import argparse
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="kiosk-demo-")
TZ_NAME = "America/Chicago"

SETTINGS = """
[branding]
title = "Work board"
assistant = "Agent"
owner = "Sam"

[time]
timezone = "%(tz)s"

[notes]
path = "%(notes)s"

[[linear.workspaces]]
name = "acme"
label = "Acme"
key_cmd = ["demo-key", "acme"]

[[linear.workspaces]]
name = "globex"
label = "Globex"
key_cmd = ["demo-key", "globex"]

[github]
author = "sam-example"
orgs = ["acme-inc", "globex-labs"]

[status]
tiles = [
  { type = "job", unit = "backup.service", key = "backup", label = "Backup" },
  { type = "failed", label = "Jobs" },
  { type = "service", unit = "chat-bot.service", key = "bot", label = "Chat bot" },
  { type = "disk", path = "/" },
  { type = "linear" },
  { type = "github" },
  { type = "standup", dir = "%(tmp)s/standup", due = "09:30" },
  { type = "uptime" },
]

[scene]
nodes = [
  { key = "backup", label = "Backup", units = [{ unit = "backup.service", glow = 900 }], running = "the backup is running", after = "the backup" },
  { key = "bot", label = "Chat", units = [{ unit = "chat-bot-digest.service", glow = 900 }], running = "a digest is going out", after = "the digest" },
  { key = "tidy", label = "Files", units = [{ unit = "tidy.service", glow = 900 }], running = "tidying the home folder", after = "the home folder" },
  { key = "lab", label = "Lab", units = [{ unit = "lab-check.service", glow = 900 }], running = "the lab check is out", after = "the lab check" },
]

[sky]
enabled = true
latitude = 41.9
longitude = -87.6

[server]
state_dir = "%(tmp)s/state"
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    ap.add_argument("--off-hours", action="store_true", help="show the off-hours view instead of the work panels")
    args = ap.parse_args()

    from zoneinfo import ZoneInfo
    tz = ZoneInfo(TZ_NAME)
    now = datetime.now(tz).replace(second=0, microsecond=0)
    notes = os.path.join(TMP, "sketchbook.md")
    cfg = os.path.join(TMP, "kiosk.toml")
    with open(cfg, "w") as f:
        f.write(SETTINGS % {"tz": TZ_NAME, "notes": notes, "tmp": TMP})
    os.environ["KIOSK_CONFIG"] = cfg
    if not args.off_hours:
        os.environ["KIOSK_TASKS_ANYTIME"] = "1"
    os.environ["KIOSK_PORT"] = str(args.port)
    os.makedirs(os.path.join(TMP, "standup"))
    with open(os.path.join(TMP, "standup", "posted-%s.json" % now.date().isoformat()), "w") as f:
        json.dump({"state": "posted", "ts": int((now.replace(hour=9, minute=31)).timestamp())}, f)

    def hm(minutes_ago):
        return (now - timedelta(minutes=minutes_ago)).strftime("%H:%M")

    head = (now - timedelta(minutes=12)).strftime("%Y-%m-%d %H:%M")
    with open(notes, "w") as f:
        f.write("# Sketchbook\n\n")
        f.write("- %s WORK TASKS (current):\n" % (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M"))
        f.write("  1. [P1] ACME-212 Checkout retries: rollout plan agreed\n")
        f.write("  2. [P1] Search speed ACME-230/231/232: measuring the new index\n")
        f.write("  3. [P2] OPS-41 Rotate the staging certificates: WAITING ON: Priya (approval)\n")
        f.write("  4. [P3] Write the on-call handbook: first draft\n\n")
        f.write("- %s IN FLIGHT (current):\n" % head)
        f.write("  W. OPS-57: apply the staging stack and create the CI tokens\n")
        f.write("  W. decide on the cache size for the search index\n")
        f.write("  R. %s ACME-231 benchmark the new index\n" % hm(38))
        f.write("  F. %s Done: release notes for 2.4 sent\n" % hm(95))
        f.write("  F. %s ACME-212 retry tests green on main\n" % hm(51))
        f.write("\n")

    os.makedirs(os.path.join(TMP, "state"))
    t = int(now.timestamp())
    agents_state = {"v": 1, "agents": {
        "a1": {"id": "a1", "desc": "ACME-212 review the retry PR", "type": "general-purpose", "depth": 1,
               "shape": "background", "state": "running", "started": t - 9 * 60, "finished": None, "runs": 1,
               "seen": t - 60, "tpath": None},
        "a2": {"id": "a2", "desc": "[work] dependency audit", "type": "general-purpose", "depth": 1,
               "shape": "background", "state": "finished", "started": t - 70 * 60, "finished": t - 22 * 60,
               "runs": 1, "seen": t - 22 * 60, "tpath": None}}}
    with open(os.path.join(TMP, "state", "agents.json"), "w") as f:
        json.dump(agents_state, f)

    sys.path.insert(0, ROOT)
    import derive   # noqa: E402
    import done     # noqa: E402
    import inflight  # noqa: E402
    import status   # noqa: E402
    import tasks    # noqa: E402

    def iso(dt):
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def issue(ident, title, prio, state, typ="started", prs=()):
        return {"identifier": ident, "title": title, "priority": prio, "state": {"name": state, "type": typ},
                "attachments": {"nodes": [{"url": "https://github.com/" + p} for p in prs]}}

    linear = {
        ("demo-key", "acme"): {"data": {"viewer": {
            "assignedIssues": {"nodes": [
                issue("ACME-212", "Retry failed checkouts with backoff", 1, "In Progress"),
                issue("ACME-230", "Search index: pick the shard size", 2, "In Progress"),
                issue("ACME-231", "Search index: benchmark against production queries", 2, "In Progress"),
                issue("ACME-232", "Search index: switch reads over", 3, "Todo", "unstarted"),
                issue("ACME-198", "Rate limits on the public API", 2, "In Review", prs=("acme-inc/api/pull/88",)),
                issue("ACME-240", "Flaky test in the payments suite", 3, "Todo", "unstarted"),
                issue("ACME-251", "Upgrade the build image", 3, "Todo", "unstarted"),
            ], "pageInfo": {"hasNextPage": False, "endCursor": None}},
            "done": {"nodes": [
                {"identifier": "ACME-190", "title": "Dark mode for the admin pages",
                 "completedAt": iso(now - timedelta(days=1, hours=3)), "state": {"type": "completed"}},
                {"identifier": "ACME-205", "title": "Export orders as CSV",
                 "completedAt": iso(now - timedelta(days=3)), "state": {"type": "completed"}}]}},
            "teams": {"nodes": [{"key": "ACME"}]}}},
        ("demo-key", "globex"): {"data": {"viewer": {
            "assignedIssues": {"nodes": [
                issue("OPS-41", "Rotate the staging certificates", 2, "Todo", "unstarted"),
                issue("OPS-57", "Staging stack for the new region", 1, "In Progress"),
                issue("OPS-63", "Alert on disk pressure", 3, "Todo", "unstarted"),
            ], "pageInfo": {"hasNextPage": False, "endCursor": None}},
            "done": {"nodes": [
                {"identifier": "OPS-38", "title": "Move the nightly backup to the new bucket",
                 "completedAt": iso(now - timedelta(hours=5)), "state": {"type": "completed"}}]}},
            "teams": {"nodes": [{"key": "OPS"}]}}},
    }

    def rollup(state):
        return {"nodes": [{"commit": {"statusCheckRollup": {"state": state}}}]}

    def pr(num, title, repo, org, state="OPEN", draft=False, ci="SUCCESS", review="APPROVED", body="", branch="",
           merged=None):
        return {"number": num, "title": title, "isDraft": draft, "reviewDecision": review, "state": state,
                "mergedAt": iso(merged) if merged else None, "headRefName": branch, "body": body,
                "repository": {"name": repo, "nameWithOwner": "%s/%s" % (org, repo)}, "commits": rollup(ci)}

    open_prs = [pr(91, "Retry failed checkouts with backoff", "shop", "acme-inc", body="Refs ACME-212",
                   branch="work/acme-212-retries"),
                pr(14, "Staging stack for the new region", "infra", "globex-labs", ci="PENDING",
                   review="REVIEW_REQUIRED", body="Refs OPS-57")]
    recent = open_prs + [pr(88, "Rate limits on the public API", "api", "acme-inc", state="MERGED",
                            merged=now - timedelta(hours=26), body="Refs ACME-198")]
    merged = [pr(88, "Rate limits on the public API", "api", "acme-inc", state="MERGED",
                 merged=now - timedelta(hours=26), body="Refs ACME-198"),
              pr(85, "Export orders as CSV", "shop", "acme-inc", state="MERGED",
                 merged=now - timedelta(days=3, hours=1), body="Closes ACME-205"),
              pr(12, "Nightly backup: new bucket", "infra", "globex-labs", state="MERGED",
                 merged=now - timedelta(hours=5, minutes=20), body="Refs OPS-38")]

    def gh(argv, timeout):
        q = argv[-1]
        if "o0:" in q:
            doc = {"data": {"o0": {"nodes": [p for p in open_prs if p["repository"]["nameWithOwner"].startswith("acme")]},
                            "o1": {"nodes": [p for p in open_prs if p["repository"]["nameWithOwner"].startswith("globex")]}}}
        elif "t0:" in q:
            doc = {"data": {"t0": {"nodes": [p for p in recent if p["repository"]["nameWithOwner"].startswith("acme")]},
                            "t1": {"nodes": [p for p in recent if p["repository"]["nameWithOwner"].startswith("globex")]}}}
        elif "m0:" in q:
            doc = {"data": {"m0": {"nodes": [p for p in merged if p["repository"]["nameWithOwner"].startswith("acme")]},
                            "m1": {"nodes": [p for p in merged if p["repository"]["nameWithOwner"].startswith("globex")]}}}
        else:
            doc = {"data": {}}
        return 0, json.dumps(doc)

    units = {
        "backup.service": "Id=backup.service\nActiveState=inactive\nResult=success\nExecMainExitTimestamp=@%d\n"
                          % (t - 6 * 3600),
        "chat-bot.service": "Id=chat-bot.service\nActiveState=active\nResult=success\n",
        "chat-bot-digest.service": "Id=chat-bot-digest.service\nActiveState=active\nResult=success\n"
                                   "ExecMainStartTimestamp=@%d\n" % (t - 40),
        "tidy.service": "Id=tidy.service\nActiveState=inactive\nResult=success\nExecMainExitTimestamp=@%d\n" % (t - 300),
    }

    def systemctl(argv, timeout=4):
        if "--property=NFailedUnits" in argv:
            return "0\n"
        return "\n\n".join(units[u] for u in argv if u in units) + "\n"

    status._run = systemctl
    status.check_disk = lambda path="/": {"key": "disk", "label": "Disk free", "value": "412 GB · 61%", "state": "ok"}
    status.check_uptime = lambda path=None: {"key": "uptime", "label": "Up", "value": "12d 4h", "state": "info"}
    status.in_session = lambda now, *a, **k: True

    if args.off_hours:
        tasks.in_hours = lambda now: False
    import server  # noqa: E402  (after the settings and the fakes above)

    def get_key(cmd):                                   # the fake key names its workspace
        return "demo-" + cmd[-1]

    def post(key, body):
        return linear[("demo-key", key.split("-", 1)[1])]
    lin = tasks.Linear(get_key=get_key, post=post, cache_file=None)
    ticket_prs = derive.TicketPRs(run=gh, wanted=lambda: inflight.wanted_refs(lin.snapshot()))
    gh_open = inflight.GitHub(run=gh)
    merged_prs = done.MergedPRs(run=gh)
    for src in (lin, ticket_prs, gh_open, merged_prs):
        src.refresh()                                   # answer at once, before the first page load
    server._linear = lin
    server._ticket_prs = ticket_prs
    server._merged_prs = merged_prs
    server._inflight_src = inflight.Source(github=gh_open, agents=server.agents.Reader(), prefixes=lin.team_keys,
                                           linear=lin, ticket_prs=ticket_prs)
    server._tasks_src = tasks.Source(linear=lin, flight=lambda: server._inflight_src.flight(), prs=ticket_prs)
    server._done_src = done.Source(linear=lin, merged=merged_prs, ticket_prs=ticket_prs)
    print("kiosk demo: http://127.0.0.1:%d/ (invented data, files in %s)" % (args.port, TMP), flush=True)
    time.sleep(0.1)
    server.main()


if __name__ == "__main__":
    main()
