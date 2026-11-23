"""Offline tests for the easel's state from sources of truth: task state lines derived from Linear, PRs and finished lines (derive.py, tasks.derive_states); background
agents recorded by the harness hook (agents.py); the merge in /api/inflight (inflight.merge_agents); the work-only rule.

Run: python3 -m unittest discover -s tests
"""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _env  # noqa: E402,F401  (settings for the tests)
sys.path.insert(0, os.path.dirname(HERE))
import agents    # noqa: E402
import derive    # noqa: E402
import inflight  # noqa: E402
import tasks     # noqa: E402

TZ = tasks.TZ
NOW_DT = datetime(2026, 11, 20, 11, 45, tzinfo=TZ)
NOW = int(NOW_DT.timestamp())


def at(h, m, day=20):
    return int(datetime(2026, 11, day, h, m, tzinfo=TZ).timestamp())


def snap(issues, teams=("OPS", "WEB")):
    return {"ws": {"globex": {"issues": issues, "teams": set(teams)}}, "status": "ok", "at": NOW}


def issue(ident, state="In Progress", title="a ticket", backlog=False, priority=2):
    return {"identifier": ident, "title": title, "priority": priority, "state": state, "backlog": backlog}


def pr(number, ids, state="open", ci="passing", draft=False, merged_at=None):
    return {"repo": "infra", "number": number, "state": state, "draft": draft, "ci": ci,
            "merged_at": merged_at, "ids": sorted(ids)}


def block_lines(written="2026-11-20 11:32", items=("  3. [P2] OPS-577 plan on PR / apply on merge: merged and applied",)):
    return ["- %s SAM'S WORK TASKS (current):" % written] + list(items)


def states(lines, linear, prs=(), finished=(), now=NOW):
    data = tasks.build(lines, linear, {"prs": list(prs), "finished": list(finished), "now": now})
    return {it["ids"] or it["title"]: (it["state"], it.get("state_src")) for it in data["items"]}


class DeriveState(unittest.TestCase):
    def test_linear_only(self):
        got = states([], snap([issue("OPS-577", "In Review")]))
        self.assertEqual(got["OPS-577"], ("In Review", "derived"))

    def test_open_pr_checks_passing(self):
        got = states([], snap([issue("OPS-577", "In Review")]), [pr(30, {"OPS-577"})])
        self.assertEqual(got["OPS-577"][0], "In Review · #30 checks passing")

    def test_failing_and_running_checks_and_draft(self):
        self.assertEqual(states([], snap([issue("OPS-577")]), [pr(30, {"OPS-577"}, ci="failing")])["OPS-577"][0],
                         "In Progress · #30 checks failing")
        self.assertEqual(states([], snap([issue("OPS-577")]), [pr(30, {"OPS-577"}, ci="running")])["OPS-577"][0],
                         "In Progress · #30 checks running")
        self.assertEqual(states([], snap([issue("OPS-577")]), [pr(31, {"OPS-577"}, ci="none", draft=True)])["OPS-577"][0],
                         "In Progress · draft #31 no checks")

    def test_open_non_draft_wins_and_more_are_counted(self):
        prs = [pr(31, {"OPS-577"}, ci="running", draft=True), pr(30, {"OPS-577"}, ci="running"),
               pr(28, {"OPS-577"}, state="merged", ci="failing", merged_at=at(11, 30))]
        self.assertEqual(states([], snap([issue("OPS-577")]), prs)["OPS-577"][0],
                         "In Progress · #30 checks running (+2 PRs)")

    def test_merged_pr_time_no_checks_and_window(self):
        prs = [pr(28, {"OPS-577"}, state="merged", ci="failing", merged_at=at(11, 30))]
        self.assertEqual(states([], snap([issue("OPS-577")]), prs)["OPS-577"][0], "In Progress · #28 merged 11:30")
        old = [pr(24, {"OPS-577"}, state="merged", merged_at=at(9, 0, day=17))]          # older than 48 h: gone
        self.assertEqual(states([], snap([issue("OPS-577")]), old)["OPS-577"][0], "In Progress")
        yday = [pr(26, {"OPS-577"}, state="merged", merged_at=at(15, 0, day=19))]
        self.assertEqual(states([], snap([issue("OPS-577")]), yday)["OPS-577"][0], "In Progress · #26 merged Nov 19")

    def test_pr_for_another_ticket_is_not_used(self):
        self.assertEqual(states([], snap([issue("OPS-577")]), [pr(5, {"OPS-57"}), pr(6, {"OPS-5770"})])["OPS-577"][0],
                         "In Progress")

    def test_hand_text_fresh_goes_after_the_facts(self):
        got = states(block_lines("2026-11-20 11:32"), snap([issue("OPS-577")]), [pr(30, {"OPS-577"}, ci="running")])
        self.assertEqual(got["OPS-577"], ("In Progress · #30 checks running · merged and applied", "derived"))

    def test_hand_text_expired_is_dropped(self):
        got = states(block_lines("2026-11-19 11:54"), snap([issue("OPS-577")]), [pr(30, {"OPS-577"}, ci="running")])
        self.assertEqual(got["OPS-577"], ("In Progress · #30 checks running", "derived"))

    def test_hand_text_window_edge(self):
        four_h = NOW - derive.HAND_FRESH_S
        written = datetime.fromtimestamp(four_h, TZ).strftime("%Y-%m-%d %H:%M")
        self.assertIn("merged and applied", states(block_lines(written), snap([issue("OPS-577")]))["OPS-577"][0])
        written = datetime.fromtimestamp(four_h - 60, TZ).strftime("%Y-%m-%d %H:%M")
        self.assertEqual(states(block_lines(written), snap([issue("OPS-577")]))["OPS-577"][0], "In Progress")

    def test_header_without_time_counts_as_old(self):
        lines = ["- SAM'S WORK TASKS (current):", "  3. OPS-577 plan: merged and applied"]
        self.assertEqual(states(lines, snap([issue("OPS-577")]))["OPS-577"][0], "In Progress")

    def test_no_ticket_keeps_hand_text_as_before(self):
        lines = block_lines("2026-11-18 09:00", ["  1. [P1] Acme CI speed ACME-4892/4893: parked for now",
                                                 "  2. Laptop cleanup: finish it"])
        got = states(lines, snap([issue("OPS-577")]))
        self.assertEqual(got["Acme CI speed ACME-4892/4893"], ("parked for now", "hand"))
        self.assertEqual(got["Laptop cleanup"], ("finish it", "hand"))

    def test_nothing_derivable_shows_the_old_note_with_its_age(self):
        # Linear off for that workspace and no PR: the hand text stays, marked with when it was written
        off = {"ws": {}, "status": "off", "at": None}
        got = states(block_lines("2026-11-19 11:54"), off)
        self.assertEqual(got["OPS-577"], ("as of Nov 19 11:54: merged and applied", "hand-old"))
        fresh = states(block_lines("2026-11-20 11:32"), off)
        self.assertEqual(fresh["OPS-577"], ("merged and applied", "hand"))

    def test_finished_line_naming_the_ticket(self):
        fin = [{"text": "OPS-577: PR #28 merged (plan trigger covers shared files)", "ts": at(11, 30)},
               {"text": "OPS-577: first apply from main", "ts": at(11, 18)},
               {"text": "OPS-567: something else", "ts": at(11, 40)}]
        self.assertEqual(states([], snap([issue("OPS-577")]), (), fin)["OPS-577"][0],
                         "In Progress · 11:30 PR #28 merged (plan trigger covers shared files)")

    def test_shorthand_ids_on_a_notes_line(self):
        # both open: a grouped row (2026-11-23); each step's state comes from its own ticket's facts only
        lines = block_lines("2026-11-20 11:00", ["  1. ACME-4892/4893 exports: review"])
        data = tasks.build(lines, {"ws": {"acme": {"issues": [issue("ACME-4892", "Todo"), issue("ACME-4893", "In Review")],
                                                     "teams": {"ACME"}}}, "status": "ok"},
                           {"prs": [pr(7, {"ACME-4893"}, ci="failing")], "finished": [], "now": NOW})
        it = data["items"][0]
        self.assertEqual((it["ids"], it["state"], it["state_src"]), ("ACME-4892/4893", "review", "group"))
        self.assertEqual([(t["id"], t["state"]) for t in it["group"]["tickets"]],
                         [("ACME-4892", "Todo"), ("ACME-4893", "In Review · #7 checks failing")])
        # one open (ACME-4893 closed): a normal row for ACME-4892; the closed ticket's PR no longer counts
        got = states(lines, {"ws": {"acme": {"issues": [issue("ACME-4892", "Todo")], "teams": {"ACME"}}},
                             "status": "ok"}, [pr(7, {"ACME-4893"}, ci="failing")])
        self.assertEqual(got["ACME-4892"][0], "Todo · review")

    def test_backlog_and_closed_rules_unchanged(self):
        # backlog issues still only show when a line names them; closed keys still drop (merge() untouched)
        lines = block_lines("2026-11-20 11:00", ["  1. OPS-1 closed one: x", "  2. OPS-9 named backlog: y"])
        got = states(lines, snap([issue("OPS-2", backlog=True), issue("OPS-9", "Backlog", backlog=True)]))
        self.assertEqual(set(got), {"OPS-9"})
        self.assertEqual(got["OPS-9"][0], "Backlog · y")

    def test_build_without_facts_is_the_old_behaviour(self):
        data = tasks.build(block_lines("2026-11-19 11:54"), snap([issue("OPS-577")]))
        self.assertEqual(data["items"][0]["state"], "merged and applied")


class PRMatching(unittest.TestCase):
    def node(self, number, body="", branch="", state="OPEN", roll="SUCCESS", owner="globex-labs", merged=None):
        return {"number": number, "state": state, "isDraft": False, "mergedAt": merged, "headRefName": branch,
                "body": body, "repository": {"name": "infra", "nameWithOwner": owner + "/infra"},
                "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": roll}}}]}}

    def doc(self, *nodes):
        return {"data": {"t0": {"nodes": list(nodes)}, "t1": {"nodes": []}}}

    def test_body_refs_wins_over_other_mentions(self):
        self.assertEqual(derive.body_ids("Plan on PR.\n\nRefs OPS-577\n\nUnblocks OPS-600 later."), {"OPS-577"})
        self.assertEqual(derive.body_ids("Closes ACME-4892, ACME-4893 and ACME-4894"),
                         {"ACME-4892", "ACME-4893", "ACME-4894"})
        self.assertEqual(derive.body_ids("Follow-up to OPS-500"), {"OPS-500"})        # no keyword: any id

    def test_ticket_only_in_branch_name(self):
        prs = derive.parse(self.doc(self.node(30, body="no ids here", branch="work/sam/ops-577-apply-on-merge")))
        self.assertEqual(prs[0]["ids"], ["OPS-577"])
        self.assertEqual(derive.prs_for({"OPS-577"}, prs, NOW)[0]["number"], 30)

    def test_ticket_in_body_and_parse_shape(self):
        prs = derive.parse(self.doc(
            self.node(30, body="Refs OPS-577", roll="PENDING"),
            self.node(28, body="Refs OPS-577", state="MERGED", roll="FAILURE", merged="2026-11-20T18:30:33Z"),
            self.node(27, body="Refs OPS-577", state="CLOSED"),
            self.node(5, body="Refs OPS-577", owner="evil")))
        self.assertEqual([(p["number"], p["state"], p["ci"]) for p in prs],
                         [(30, "open", "running"), (28, "merged", "failing")])
        self.assertEqual(prs[1]["merged_at"], at(11, 30) + 33)
        self.assertNotIn("body", prs[0])

    def test_fetch_failure_last_good_and_no_secrets(self):
        calls = []

        def run(args, timeout):
            calls.append(args)
            if len(calls) == 1:
                return 0, json.dumps(self.doc(self.node(30, body="Refs OPS-577")))
            raise subprocess.TimeoutExpired(args, timeout)
        clock = [1000.0]
        logs = []
        t = derive.TicketPRs(run=run, clock=lambda: clock[0], wall=lambda: NOW, log=logs.append)
        self.assertEqual(t.snapshot()["status"], "pending")
        t.refresh()
        self.assertEqual((t.snapshot()["status"], t.snapshot()["at"]), ("ok", NOW))
        clock[0] += 600
        t.refresh()
        self.assertEqual(t.snapshot()["status"], "stale")            # last good, with its age
        clock[0] += derive.GOOD_FOR_S
        self.assertEqual(t.snapshot(), {"prs": [], "status": "off", "at": None, "closed": None, "looked": {}})
        q = calls[0][-1]
        for want in ("author:octocat", "org:globex-labs", "org:acme-inc", "updated:>=2026-11-17", "headRefName"):
            self.assertIn(want, q)
        self.assertFalse(any("token" in a.lower() for a in calls[0]))
        self.assertEqual(logs, ["tasks: github ticket PRs fetch failed (TimeoutExpired)"])

    def test_garbage(self):
        for out in ("nope", json.dumps({"errors": [1]}), json.dumps([1])):
            t = derive.TicketPRs(run=lambda a, t, o=out: (0, o), log=lambda line: None)
            t.refresh()
            self.assertEqual(t.snapshot()["status"], "off")


class Hook(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "agents.json")
        self.projects = os.path.join(self.tmp.name, "projects")
        self.session = os.path.join(self.projects, "-home-agent", "sess1")
        os.makedirs(os.path.join(self.session, "subagents"))
        self.main = self.session + ".jsonl"
        open(self.main, "w").close()
        self._old = agents.PROJECTS
        agents.PROJECTS = self.projects

    def tearDown(self):
        agents.PROJECTS = self._old
        self.tmp.cleanup()

    def meta(self, aid, desc, depth=1, shape="background"):
        with open(os.path.join(self.session, "subagents", "agent-%s.meta.json" % aid), "w") as f:
            json.dump({"agentType": "general-purpose", "description": desc, "spawnDepth": depth,
                       "requestShape": shape, "toolUseId": "toolu_x"}, f)

    def ev(self, name, aid, now, **extra):
        e = {"hook_event_name": name, "agent_id": aid, "agent_type": "general-purpose", "session_id": "sess1",
             "transcript_path": self.main}
        e.update(extra)
        agents.record(e, self.path, now)

    def state(self):
        return agents.load(self.path)["agents"]

    def test_launch_and_finish(self):
        self.meta("a1", "OPS-577 apply on merge")
        self.ev("SubagentStart", "a1", 100)
        e = self.state()["a1"]
        self.assertEqual((e["state"], e["desc"], e["depth"], e["shape"], e["started"]),
                         ("running", "OPS-577 apply on merge", 1, "background", 100))
        self.assertTrue(e["tpath"].endswith("subagents/agent-a1.jsonl"))
        self.ev("SubagentStop", "a1", 160)
        e = self.state()["a1"]
        self.assertEqual((e["state"], e["finished"], e["runs"]), ("finished", 160, 1))

    def test_post_tool_use_backup_for_background_and_foreground(self):
        agents.record({"hook_event_name": "PostToolUse", "tool_name": "Agent", "transcript_path": self.main,
                       "tool_input": {"description": "OPS-577 destroy proof", "run_in_background": True},
                       "tool_response": {"status": "async_launched", "agentId": "b2"}}, self.path, 200)
        self.assertEqual((self.state()["b2"]["state"], self.state()["b2"]["desc"]), ("running", "OPS-577 destroy proof"))
        agents.record({"hook_event_name": "PostToolUse", "tool_name": "Agent", "transcript_path": self.main,
                       "tool_input": {"description": "ACME-1 quick look"},
                       "tool_response": {"status": "completed", "agentId": "c3", "totalDurationMs": 50000}},
                      self.path, 300)
        e = self.state()["c3"]
        self.assertEqual((e["state"], e["started"], e["finished"], e["shape"]), ("finished", 250, 300, "foreground"))

    def test_resumed_and_finishes_twice(self):
        self.meta("a1", "OPS-577 build")
        self.ev("SubagentStart", "a1", 100)
        self.ev("SubagentStop", "a1", 200)
        self.ev("SubagentStart", "a1", 300)              # resumed with SendMessage
        e = self.state()["a1"]
        self.assertEqual((e["state"], e["started"], e["finished"], e["runs"]), ("running", 300, None, 2))
        self.ev("SubagentStop", "a1", 400)
        e = self.state()["a1"]
        self.assertEqual((e["state"], e["finished"], e["runs"]), ("finished", 400, 2))
        self.assertEqual(len(self.state()), 1)

    def test_double_finish_keeps_the_first_time(self):
        self.meta("a1", "OPS-577 build")
        self.ev("SubagentStart", "a1", 100)
        self.ev("SubagentStop", "a1", 200)
        self.ev("SubagentStop", "a1", 250)
        agents.record({"hook_event_name": "PostToolUse", "tool_name": "Agent", "transcript_path": self.main,
                       "tool_response": {"status": "completed", "agentId": "a1"}}, self.path, 260)
        self.assertEqual(self.state()["a1"]["finished"], 200)

    def test_internal_agents_are_never_recorded(self):
        self.ev("SubagentStop", "zz9", 100)               # no meta file, never started: a prompt-suggestion agent
        self.assertEqual(self.state(), {})
        self.meta("late", "OPS-1 started before the hook existed")
        self.ev("SubagentStop", "late", 100)
        self.assertEqual((self.state()["late"]["state"], self.state()["late"]["started"]), ("finished", None))

    def test_garbage_input_never_fails(self):
        for raw in ("", "not json", "[1,2]", json.dumps({"hook_event_name": "SubagentStart"}),
                    json.dumps({"hook_event_name": "SubagentStart", "agent_id": "../../etc/passwd"}),
                    json.dumps({"hook_event_name": "PostToolUse", "tool_name": "Agent", "tool_response": "x"}),
                    json.dumps({"hook_event_name": "SubagentStart", "agent_id": "ok1",
                                "transcript_path": "/etc/passwd"}),
                    "x" * (agents.INPUT_MAX + 10)):
            err = io.StringIO()
            old, sys.stderr = sys.stderr, err
            try:
                self.assertEqual(agents.hook_main(io.StringIO(raw), self.path), 0)
            finally:
                sys.stderr = old
            self.assertLessEqual(err.getvalue().count("\n"), 1)
        st = self.state()
        self.assertEqual(set(st), {"ok1"})
        self.assertIsNone(st["ok1"]["tpath"])             # a path outside ~/.claude/projects is never kept

    def test_other_events_write_nothing(self):
        self.assertEqual(agents.hook_main(io.StringIO(json.dumps({"hook_event_name": "Stop"})), self.path), 0)
        self.assertFalse(os.path.exists(self.path))
        self.assertFalse(os.path.exists(self.path + ".lock"))

    def test_bad_state_file_is_replaced_not_fatal(self):
        with open(self.path, "w") as f:
            f.write("{broken")
        self.meta("a1", "OPS-1 x")
        self.ev("SubagentStart", "a1", 100)
        self.assertEqual(list(self.state()), ["a1"])

    def test_concurrent_writes_lose_nothing(self):
        for i in range(40):
            self.meta("c%d" % i, "OPS-%d job" % i)

        def go(i):
            self.ev("SubagentStart", "c%d" % i, 100 + i)
        threads = [threading.Thread(target=go, args=(i,)) for i in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.state()), 40)
        self.assertEqual([n for n in os.listdir(self.tmp.name) if n.endswith(".tmp")], [])

    def test_prune_old_and_cap(self):
        self.meta("old", "OPS-1 old")
        self.ev("SubagentStart", "old", 100)
        self.meta("new", "OPS-2 new")
        self.ev("SubagentStart", "new", 100 + agents.KEEP_S + 10)
        self.assertEqual(list(self.state()), ["new"])

    def test_lock_timeout_is_one_stderr_line_and_exit_0(self):
        import fcntl
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path + ".lock", "a") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            old_wait, agents.LOCK_WAIT_S = agents.LOCK_WAIT_S, 0.05
            err = io.StringIO()
            old, sys.stderr = sys.stderr, err
            try:
                raw = json.dumps({"hook_event_name": "SubagentStart", "agent_id": "a1", "transcript_path": self.main})
                self.assertEqual(agents.hook_main(io.StringIO(raw), self.path), 0)
            finally:
                sys.stderr, agents.LOCK_WAIT_S = old, old_wait
        self.assertEqual(err.getvalue(), "easel-agents-hook: TimeoutError\n")


class ViewAndWorkRule(unittest.TestCase):
    def entry(self, aid, desc, state="running", started=at(11, 0), finished=None, depth=1, seen=None, tpath=None):
        return {"id": aid, "desc": desc, "state": state, "started": started, "finished": finished, "depth": depth,
                "seen": seen or finished or started, "tpath": tpath, "runs": 1}

    def view(self, *entries, prefixes=None, stat=None):
        st = {"v": 1, "agents": {e["id"]: e for e in entries}}
        return agents.view(st, NOW, prefixes, stat=stat or (lambda p: (_ for _ in ()).throw(OSError())))

    def test_work_only(self):
        v = self.view(self.entry("a", "OPS-577 apply on merge"),
                      self.entry("b", "Easel: state from Linear [work]"),
                      self.entry("c", "Christmas list ideas"),                       # personal: never shown
                      self.entry("d", "chess blunder check"),
                      self.entry("e", "UTF-8 cleanup of notes"))                     # UTF is no team key
        self.assertEqual(sorted(x["text"] for x in v["running"]), sorted(["OPS-577 apply on merge", "Easel: state from Linear"]))
        self.assertEqual(v["hidden"], 3)

    def test_prefixes_from_linear(self):
        v = self.view(self.entry("a", "PRO-12 thing"), prefixes={"PRO"})
        self.assertEqual(len(v["running"]), 1)
        self.assertEqual(len(self.view(self.entry("a", "PRO-12 thing"))["running"]), 0)   # fallback DO/ENG/ACME

    def test_nested_agents_never_show(self):
        self.assertEqual(self.view(self.entry("a", "OPS-577 helper", depth=2))["running"], [])

    def test_quiet_and_expired(self):
        quiet = self.entry("q", "OPS-1 long", started=at(6, 0))                     # no transcript, 5 h 45 m ago
        gone = self.entry("g", "OPS-2 crashed", started=at(23, 0, day=19))         # 12 h 45 m of silence
        v = self.view(quiet, gone)
        self.assertEqual([(x["id"], x.get("quiet_since")) for x in v["running"]], [("q", at(6, 0))])

    def test_transcript_mtime_is_the_last_word(self):
        class St:
            st_mtime = at(11, 40)
        busy = self.entry("b", "OPS-1 busy", started=at(8, 0), tpath=os.path.join(agents.PROJECTS, "x", "a.jsonl"))
        v = self.view(busy, stat=lambda p: St)
        self.assertNotIn("quiet_since", v["running"][0])
        St.st_mtime = at(10, 30)                                                  # 75 min without a write
        v = self.view(busy, stat=lambda p: St)
        self.assertEqual(v["running"][0]["quiet_since"], at(10, 30))

    def test_finished_window_and_order(self):
        v = self.view(self.entry("a", "OPS-1 a", "finished", finished=at(11, 30)),
                      self.entry("b", "OPS-2 b", "finished", finished=at(11, 40)),
                      self.entry("c", "OPS-3 c", "finished", finished=at(15, 0, day=19)))   # 20 h ago
        self.assertEqual([x["id"] for x in v["finished"]], ["b", "a"])


class InflightMerge(unittest.TestCase):
    def hand(self, lines, header="2026-11-20 11:33"):
        return inflight.parse(["- %s IN FLIGHT (current):" % header] + ["  " + x for x in lines], NOW_DT)

    def harness(self, running=(), finished=()):
        return {"running": [dict(x, source="harness") for x in running],
                "finished": [dict(x, source="harness") for x in finished], "hidden": 0}

    def test_harness_authoritative_hand_kept_unless_same_agent(self):
        block = self.hand(["W. OPS-577: say post",
                           "R. 11:31 OPS-577: building apply on merge + a stop on destroy",
                           "R. 11:20 ACME-4810: rehearsal prep",
                           "F. 11:30 OPS-567: PR #29 merged"])
        h = self.harness(running=[{"text": "OPS-577 apply on merge + destroy stop", "ts": at(11, 32), "id": "a1"}],
                         finished=[{"text": "OPS-567 merge PR 29", "ts": at(11, 50), "id": "a2"}])
        m = inflight.merge_agents(block, h)
        self.assertEqual([(x["text"], x["source"]) for x in m["running"]],
                         [("OPS-577 apply on merge + destroy stop", "harness"), ("ACME-4810: rehearsal prep", "notes")])
        self.assertEqual([x["source"] for x in m["finished"]], ["harness", "notes"])
        self.assertEqual(m["waiting"], block["waiting"])                        # W lines untouched
        self.assertEqual(m["counts"]["running"], 2)

    def test_hand_running_line_for_an_agent_the_harness_saw_finish(self):
        block = self.hand(["R. 11:31 Easel: state from Linear and GitHub [work]"])
        h = self.harness(finished=[{"text": "Easel: state from Linear and GitHub", "ts": at(11, 44), "id": "k"}])
        m = inflight.merge_agents(block, h)
        self.assertEqual(m["running"], [])
        self.assertEqual(m["finished"][0]["text"], "Easel: state from Linear and GitHub")

    def test_work_tag_not_shown_on_hand_lines(self):
        m = inflight.merge_agents(self.hand(["R. 11:33 Easel: state from hooks [work]"]), self.harness())
        self.assertEqual(m["running"][0]["text"], "Easel: state from hooks")

    def test_stale_hand_block_harness_still_shows(self):
        block = self.hand(["R. 08:00 OPS-1 x"], header="2026-11-19 08:00")
        m = inflight.merge_agents(block, self.harness(running=[{"text": "OPS-2 y", "ts": at(11, 0), "id": "z"}]))
        self.assertEqual([x["text"] for x in m["running"]], ["OPS-2 y"])

    def test_several_at_once_and_cap(self):
        many = [{"text": "OPS-%d job" % i, "ts": at(11, 0) + i, "id": "a%d" % i} for i in range(15)]
        m = inflight.merge_agents(self.hand([]), self.harness(finished=many))
        self.assertEqual((len(m["finished"]), m["counts"]["finished"]), (inflight.FINISHED_MAX, 15))
        self.assertEqual(m["finished"][0]["text"], "OPS-14 job")

    def test_source_payload_and_task_flags(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        notes = os.path.join(tmp.name, "h.md")
        with open(notes, "w") as f:
            f.write("- 2026-11-19 11:54 SAM'S WORK TASKS (current):\n  1. OPS-577 plan: merged and applied\n"
                    "- 2026-11-20 11:33 IN FLIGHT (current):\n  W. OPS-577: say post\n")
        cfg = os.path.join(tmp.name, "c.json")
        with open(cfg, "w") as f:
            json.dump({"notes": notes}, f)

        class Reader:
            def current(self):
                return {"v": 1, "agents": {
                    "a1": {"id": "a1", "desc": "OPS-577 apply on merge", "state": "running", "started": at(11, 31),
                           "seen": at(11, 44), "depth": 1, "finished": None},
                    "p1": {"id": "p1", "desc": "Christmas list", "state": "running", "started": at(11, 31),
                           "seen": at(11, 44), "depth": 1, "finished": None}}}
        src = inflight.Source(notes=_env.notes_of(cfg), agents=Reader(), now=lambda: NOW_DT)
        out = inflight.payload(src, now=NOW_DT, force=True)
        self.assertEqual(out["agents"], "ok")
        self.assertEqual([x["text"] for x in out["running"]], ["OPS-577 apply on merge"])
        self.assertNotIn("Christmas", json.dumps(out))

        class Lin:
            def maybe_refresh(self):
                pass

            def snapshot(self):
                return snap([issue("OPS-577", "In Review")])

        class PRs:
            def maybe_refresh(self):
                pass

            def snapshot(self):
                return {"prs": [pr(30, {"OPS-577"}, ci="running")], "status": "stale", "at": at(11, 0)}
        t = tasks.Source(notes=_env.notes_of(cfg), linear=Lin(), flight=src.flight, prs=PRs(), wall=lambda: NOW).current()
        it = t["items"][0]
        self.assertEqual(it["state"], "In Review · #30 checks running")
        self.assertEqual((it["waiting_text"], it["running"], it["active"]), ("say post", True, "apply on merge"))
        self.assertEqual((t["github"], t["github_at"], t["linear"]), ("stale", at(11, 0), "ok"))


class PageStaleness(unittest.TestCase):
    """The page keeps the last data on a failed poll and says since when (no empty list that reads "nothing")."""

    def test_page_source(self):
        with open(os.path.join(os.path.dirname(HERE), "web", "screen.js")) as f:
            js = f.read()
        for want in ("if (lastTasks) renderTasks(lastTasks);", "if (lastFlight) renderFlight(lastFlight);",
                     '"Linear as of "', '"PRs as of "', '"no word since "', '"not answering since "',
                     'st.className = "tstate"'):
            self.assertIn(want, js)
        with open(os.path.join(os.path.dirname(HERE), "web", "index.html")) as f:
            self.assertIn('id="tasksrc"', f.read())


if __name__ == "__main__":
    unittest.main()
