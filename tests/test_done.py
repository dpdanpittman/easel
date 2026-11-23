"""Offline tests for the Done this week box (done.py), Linear's "done" field (tasks.parse_done), the health tiles
(status.check_linear / check_github / check_standup) and the /api/done route. No gh, no Linear, no network.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _env  # noqa: E402,F401  (settings for the tests)
sys.path.insert(0, os.path.dirname(HERE))
import done     # noqa: E402
import status   # noqa: E402
import tasks    # noqa: E402

TZ = tasks.TZ
NOW = datetime(2026, 11, 23, 13, 30, tzinfo=TZ)
NOW_TS = int(NOW.timestamp())
H = 3600


def ts(day, hh, mm=0):
    return int(datetime(2026, 11, day, hh, mm, tzinfo=TZ).timestamp())


def gql_pr(num, title, merged, body="", branch="x", owner="acme-inc", repo="acme"):
    return {"number": num, "title": title, "mergedAt": datetime.fromtimestamp(merged).astimezone(
        __import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "headRefName": branch, "body": body,
        "repository": {"name": repo, "nameWithOwner": owner + "/" + repo}}


class Runner:
    def __init__(self, out, code=0):
        self.out, self.code, self.calls = out, code, []

    def __call__(self, args, timeout):
        self.calls.append(args)
        return self.code, self.out


class Merged(unittest.TestCase):
    def test_query_is_a_read_only_search_of_the_last_7_days(self):
        q = done.query(datetime(2026, 11, 23))
        for want in ("is:pr is:merged", "author:octocat", "org:globex-labs", "org:acme-inc", "merged:>=2026-11-16"):
            self.assertIn(want, q)
        self.assertNotIn("mutation", q)

    def test_parse_keeps_well_formed_nodes_and_ids(self):
        doc = {"data": {"m0": {"nodes": [gql_pr(5, "fix", ts(23, 10), owner="globex-labs", repo="monitors",
                                                body="Refs OPS-580")]},
                        "m1": {"nodes": [gql_pr(4321, "feat", ts(23, 13), branch="sam/acme-5213-x"),
                                         {"number": "x"}, gql_pr(1, "foreign", ts(23, 9), owner="evil")]}}}
        prs = done.parse_merged(doc)
        self.assertEqual([(p["number"], p["ids"]) for p in prs], [(5, ["OPS-580"]), (4321, ["ACME-5213"])])
        with self.assertRaises(ValueError):
            done.parse_merged({"errors": [{}]})

    def test_fetch_failure_is_quiet_and_marks_off(self):
        logs = []
        m = done.MergedPRs(run=Runner("", code=1), log=logs.append)
        m.refresh()
        self.assertEqual(m.snapshot()["status"], "off")
        self.assertEqual(len(logs), 1)
        self.assertNotIn("http", logs[0])

    def test_saved_copy_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "m.json")
            doc = json.dumps({"data": {"m0": {"nodes": []}, "m1": {"nodes": [gql_pr(9, "t", ts(23, 10))]}}})
            done.MergedPRs(run=Runner(doc), cache_file=f).refresh()
            again = done.MergedPRs(run=Runner("", 1), cache_file=f)
            snap = again.snapshot()
            self.assertEqual((snap["status"], [p["number"] for p in snap["prs"]]), ("stale", [9]))
            self.assertEqual(oct(os.stat(f).st_mode & 0o777), "0o600")


NOTES = """- 2026-11-14 10:00 IN FLIGHT (current):
  F. 09:00 Done: ACME-1: too old, outside the 7 days
- 2026-11-20 12:00 IN FLIGHT (current):
  W. ACME-9: a waiting line
  F. 11:30 Done: OPS-567: say Done (fix merged, check green)
  F. 11:31 OPS-567: marked Done
  F. 11:40 Done: Essence site: pick a design direction (A, B or C)
not indented: the block ends
  F. 11:50 Done: never read, outside a block
- 2026-11-20 12:10 IN FLIGHT (current):
  F. 11:30 Done: OPS-567: say Done (fix merged, check green)
  F. 12:05 Done: Essence site: pick the design direction A or B or C
  F. 12:06 Done: see https://evil.example/x `rm -rf` \x1b[31m
- 2026-11-23 13:00 IN FLIGHT (current):
  F. 12:48 Done: ACME-5171: draft the three questions to Noor as a chat message?
"""


class NotesDone(unittest.TestCase):
    def test_done_lines_of_the_last_7_days_once_each(self):
        got = done.notes_done(NOTES.split("\n"), NOW)
        texts = [g["text"] for g in got]
        self.assertEqual(len([t for t in texts if t.startswith("OPS-567: say Done")]), 1)
        self.assertNotIn("ACME-1: too old, outside the 7 days", texts)
        self.assertFalse(any("marked Done" in t for t in texts))       # only "Done:" lines
        self.assertFalse(any("never read" in t for t in texts))
        self.assertFalse(any("waiting line" in t for t in texts))
        bad = [t for t in texts if t.startswith("see")][0]
        self.assertNotIn("http", bad)
        self.assertNotIn("`", bad)
        self.assertNotIn("\x1b", bad)
        self.assertEqual(got[-1]["ids"], ["ACME-5171"])

    def test_odd_input_never_raises(self):
        self.assertEqual(done.notes_done(None, NOW), [])
        self.assertEqual(done.notes_done(["- 2026-13-40 99:99 IN FLIGHT:", "  F. xx Done: y"], NOW), [])


class Combine(unittest.TestCase):
    def test_same_ticket_or_pr_counts_once(self):
        tickets = [{"identifier": "OPS-567", "title": "replace the read token", "completed_at": ts(20, 11, 32)}]
        prs = [{"owner_repo": "globex-labs/infra", "repo": "infra", "number": 29, "title": "ci: token",
                "merged_at": ts(20, 11, 30), "ids": ["OPS-567"]}]
        notes = [{"ts": ts(20, 11, 30), "text": "OPS-567: say Done", "ids": ["OPS-567"]},
                 {"ts": ts(21, 9, 0), "text": "OPS-567: another day", "ids": ["OPS-567"]}]
        items = done.combine(tickets, prs, notes)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual((it["ref"], it["pr"], it["title"], it["ts"]), ("OPS-567", "#29", "replace the read token",
                                                                          ts(21, 9, 0)))
        self.assertEqual(it["kinds"], ["linear", "note", "pr"])

    def test_pr_without_ticket_shows_repo_and_number(self):
        prs = [{"owner_repo": "globex-labs/tribunal", "repo": "tribunal", "number": 141, "title": "fix docs",
                "merged_at": ts(23, 9), "ids": []}]
        self.assertEqual(done.combine([], prs, [])[0]["ref"], "tribunal#141")

    def test_same_day_word_overlap_merges(self):
        notes = [{"ts": ts(20, 11, 40), "text": "Essence site: pick a design direction (A, B or C)", "ids": []},
                 {"ts": ts(20, 12, 5), "text": "Essence site: pick the design direction A or B or C", "ids": []},
                 {"ts": ts(21, 12, 5), "text": "Essence site: pick the design direction A or B or C", "ids": []}]
        items = done.combine([], [], notes)
        self.assertEqual(len(items), 2)                     # the other day stays its own item

    def test_overlap_never_merges_two_tickets_or_two_prs(self):
        tickets = [{"identifier": "ACME-1", "title": "cache the pnpm store in ci", "completed_at": ts(23, 10)},
                   {"identifier": "ACME-2", "title": "cache the pnpm store in ci again", "completed_at": ts(23, 11)}]
        prs = [{"owner_repo": "acme-inc/acme", "repo": "acme", "number": n, "title": "bump the cache key now",
                "merged_at": ts(23, 9), "ids": []} for n in (1, 2)]
        self.assertEqual(len(done.combine(tickets, prs, [])), 4)

    def test_low_overlap_stays_apart(self):
        notes = [{"ts": ts(23, 10), "text": "approve the gitleaks rerun for the PR", "ids": []},
                 {"ts": ts(23, 11), "text": "pick a design for the essence site", "ids": []}]
        self.assertEqual(len(done.combine([], [], notes)), 2)

    def test_foreign_prefixes_are_not_tickets(self):
        prs = [{"owner_repo": "acme-inc/acme", "repo": "acme", "number": 15, "title": "t", "merged_at": ts(23, 9),
                "ids": ["MAIN-2026", "OPS-570"]}]
        self.assertEqual(done.combine([], prs, [], {"OPS", "ACME"})[0]["ref"], "OPS-570")

    def test_newest_first(self):
        notes = [{"ts": ts(21, 9), "text": "older thing done here", "ids": []},
                 {"ts": ts(23, 9), "text": "newer different finished", "ids": []}]
        self.assertEqual([i["title"] for i in done.combine([], [], notes)][0], "newer different finished")


class FakeLinear:
    def __init__(self, snap):
        self.snap = snap

    def maybe_refresh(self):
        pass

    def snapshot(self):
        return self.snap


class FakeSnap(FakeLinear):
    pass


def linear_snap(done_b=None, done_e=None, status_="ok", issues=()):
    ws = {}
    if done_b is not None:
        ws["globex"] = {"issues": [], "teams": {"OPS"}, "wall": NOW_TS - 60, "done": done_b}
    if done_e is not None:
        ws["acme"] = {"issues": list(issues), "teams": {"ACME"}, "wall": NOW_TS - 60, "done": done_e}
    return {"ws": ws, "status": status_, "at": NOW_TS - 60}


class SourceAndPayload(unittest.TestCase):
    def cfg(self, d, text=NOTES):
        h = os.path.join(d, "notes.md")
        with open(h, "w") as f:
            f.write(text)
        c = os.path.join(d, "config.json")
        with open(c, "w") as f:
            json.dump({"notes": h}, f)
        return c

    def test_all_sources_answer(self):
        with tempfile.TemporaryDirectory() as d:
            lin = FakeLinear(linear_snap([{"identifier": "OPS-567", "title": "token", "completed_at": ts(20, 11)}],
                                         [{"identifier": "ACME-9", "title": "old", "completed_at": ts(10, 11)}]))
            gh = FakeSnap({"status": "ok", "at": NOW_TS, "prs": [
                {"owner_repo": "acme-inc/acme", "repo": "acme", "number": 4321, "title": "feat",
                 "merged_at": ts(23, 13, 21), "ids": ["ACME-5213"]}]})
            src = done.Source(linear=lin, merged=gh, notes=_env.notes_of(self.cfg(d)), now=lambda: NOW)
            with unittest.mock.patch.object(tasks, "in_hours", return_value=True):
                data = done.payload(src, NOW)
        self.assertTrue(data["show"])
        self.assertEqual(data["notes"], [])
        refs = [i["ref"] for i in data["items"]]
        self.assertEqual(refs[0], "ACME-5213")
        self.assertNotIn("ACME-9", refs)                    # completed 13 days ago
        self.assertEqual(data["count"], len(data["items"]) + data["more"])

    def test_each_source_fails_closed_and_says_so(self):
        with tempfile.TemporaryDirectory() as d:
            lin = FakeLinear(linear_snap([], None, status_="stale"))
            gh = FakeSnap({"status": "off", "at": None, "prs": []})
            src = done.Source(linear=lin, merged=gh, notes=_env.notes_of(os.path.join(d, "missing.json")), now=lambda: NOW)
            data = src.current()
        self.assertEqual(data["items"], [])
        self.assertIn("Linear Acme has not answered", data["notes"])
        self.assertIn("GitHub has not answered", data["notes"])
        self.assertIn("notes file not readable", data["notes"])
        self.assertTrue(any(n.startswith("Linear as of") for n in data["notes"]))

    def test_stale_github_is_used_with_its_age(self):
        with tempfile.TemporaryDirectory() as d:
            gh = FakeSnap({"status": "stale", "at": ts(23, 12, 5), "prs": [
                {"owner_repo": "acme-inc/acme", "repo": "acme", "number": 1, "title": "x", "merged_at": ts(23, 9),
                 "ids": []}]})
            data = done.Source(linear=FakeLinear(linear_snap([], [])), merged=gh, notes=_env.notes_of(self.cfg(d)),
                               now=lambda: NOW).current()
        self.assertIn("GitHub as of 12:05", data["notes"])
        self.assertIn("acme#1", [i["ref"] for i in data["items"]])

    def test_can_close_uses_the_auto_waiting_rule(self):
        issue = {"identifier": "ACME-4896", "title": "deploy lead", "state": "In Review", "priority": 2,
                 "backlog": False, "prs": ["acme-inc/acme#7100"]}
        lin = FakeLinear(linear_snap([], [], issues=[issue]))
        tp = FakeSnap({"status": "ok", "prs": [{"owner_repo": "acme-inc/acme", "repo": "acme", "number": 7100,
                                                 "state": "merged", "ids": []}], "closed": [], "looked": {}})
        with tempfile.TemporaryDirectory() as d:
            data = done.Source(linear=lin, merged=FakeSnap({"status": "ok", "at": NOW_TS, "prs": []}),
                               ticket_prs=tp, notes=_env.notes_of(self.cfg(d)), now=lambda: NOW).current()
        self.assertEqual(data["can_close"], ["ACME-4896"])

    def test_hidden_outside_work_hours_and_nothing_read(self):
        class Boom:
            def current(self):
                raise AssertionError("read outside hours")
        with unittest.mock.patch.dict(os.environ, {tasks.FORCE_ENV: ""}), \
                unittest.mock.patch.object(tasks, "in_hours", return_value=False):
            data = done.payload(Boom())
        self.assertFalse(data["show"])
        self.assertEqual(data["items"], [])

    def test_route_and_cache(self):
        import server
        old = server._done_src

        class Fixed:
            calls = 0

            def current(self):
                Fixed.calls += 1
                return {"show": True, "count": 1, "items": [{"ts": NOW_TS, "ref": "OPS-1", "pr": "", "title": "t",
                                                              "kinds": ["note"]}], "more": 0, "can_close": [],
                        "notes": [], "now": NOW_TS}
        server._done_src = Fixed()
        server._done_cache["at"] = 0.0
        try:
            with unittest.mock.patch.object(tasks, "in_hours", return_value=True):
                a = json.loads(server.done_body())
                server.done_body()
        finally:
            server._done_src = old
            server._done_cache["at"] = 0.0
        self.assertEqual(a["items"][0]["ref"], "OPS-1")
        self.assertEqual(Fixed.calls, 1)


class LinearDone(unittest.TestCase):
    def test_query_asks_for_the_completed_issues(self):
        self.assertIn('done: assignedIssues(filter: {completedAt: {gte: "-P7D"}}', tasks.QUERY)
        self.assertNotIn("mutation", tasks.QUERY)

    def test_parse_done(self):
        doc = {"data": {"viewer": {"done": {"nodes": [
            {"identifier": "ACME-1", "title": "t", "completedAt": "2026-11-23T19:14:02.123Z", "state": {"type": "completed"}},
            {"identifier": "ACME-2", "title": "c", "completedAt": "2026-11-23T19:14:02.123Z", "state": {"type": "canceled"}},
            {"identifier": "bad id", "title": "t", "completedAt": "2026-11-23T19:14:02Z", "state": {"type": "completed"}},
            {"identifier": "ACME-3", "title": "t", "completedAt": None, "state": {"type": "completed"}}]}}}}
        self.assertEqual(tasks.parse_done(doc), [{"identifier": "ACME-1", "title": "t", "completed_at": 1795461242}])
        self.assertIsNone(tasks.parse_done({"data": {"viewer": {}}}))
        self.assertIsNone(tasks.parse_done(None))

    def test_fetch_keeps_done_and_saves_it(self):
        page = {"data": {"viewer": {"assignedIssues": {"nodes": [], "pageInfo": {"hasNextPage": False}},
                                    "done": {"nodes": [{"identifier": "OPS-1", "title": "t",
                                                        "completedAt": "2026-11-23T19:14:02Z",
                                                        "state": {"type": "completed"}}]}},
                         "teams": {"nodes": [{"key": "OPS"}]}}}
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "lin.json")
            lin = tasks.Linear(get_key=lambda item: "k", post=lambda key, body: page, log=lambda line: None,
                               cache_file=f)
            lin.refresh()
            self.assertEqual([x["identifier"] for x in lin.snapshot()["ws"]["globex"]["done"]], ["OPS-1"])
            again = tasks.Linear(get_key=lambda item: None, post=None, log=lambda line: None, cache_file=f)
            self.assertEqual([x["identifier"] for x in again.snapshot()["ws"]["globex"]["done"]], ["OPS-1"])

    def test_an_answer_without_done_is_unknown_not_empty(self):
        page = {"data": {"viewer": {"assignedIssues": {"nodes": [], "pageInfo": {"hasNextPage": False}}},
                         "teams": {"nodes": []}}}
        lin = tasks.Linear(get_key=lambda item: "k", post=lambda key, body: page, log=lambda line: None)
        lin.refresh()
        self.assertIsNone(lin.snapshot()["ws"]["globex"]["done"])


class HealthTiles(unittest.TestCase):
    def test_linear_ages(self):
        n = NOW_TS
        self.assertEqual(status.check_linear(n, {"at": n - 60, "missing": []})["state"], "ok")
        self.assertEqual(status.check_linear(n, {"at": n - 16 * 60, "missing": []})["state"], "warn")
        self.assertEqual(status.check_linear(n, {"at": n - 3 * H, "missing": []})["state"], "bad")
        self.assertEqual(status.check_linear(n, {"at": n - 60, "missing": ["acme"]})["state"], "bad")
        self.assertEqual(status.check_linear(n, {"at": None, "missing": []})["state"], "bad")

    def test_github_ages_and_failing(self):
        n = NOW_TS
        ok = {"at": n - 60, "status": "ok"}
        self.assertEqual(status.check_github(n, [ok, ok, ok])["state"], "ok")
        self.assertEqual(status.check_github(n, [ok, {"at": n - 11 * 60, "status": "ok"}])["state"], "warn")
        self.assertEqual(status.check_github(n, [ok, {"at": n - 2 * H, "status": "ok"}])["state"], "bad")
        self.assertEqual(status.check_github(n, [ok, {"at": n - 60, "status": "stale", "failed": True}])["value"],
                         "failing")
        # a copy loaded from disk before the first fetch is not a failure
        self.assertEqual(status.check_github(n, [ok, {"at": n - 60, "status": "stale", "failed": False}])["state"], "ok")
        self.assertEqual(status.check_github(n, [ok, {"at": None, "status": "off"}])["state"], "bad")
        self.assertEqual(status.check_github(n, [{"at": None, "status": "pending"}])["state"], "info")

    def standup(self, when, posted=None, held=None, unit=None):
        with tempfile.TemporaryDirectory() as d:
            day = when.date().isoformat()
            if posted is not None:
                with open(os.path.join(d, "posted-%s.json" % day), "w") as f:
                    json.dump(posted, f)
            if held is not None:
                with open(os.path.join(d, "held-%s.json" % day), "w") as f:
                    json.dump(held, f)
            return status.check_standup(when, unit, d, (7, 45))

    def test_standup_states(self):
        day = datetime(2026, 11, 23, 9, 0, tzinfo=TZ)
        posted = {"state": "posted", "ts": str(ts(23, 7, 45)) + ".940889", "permalink": "https://x"}
        self.assertEqual(self.standup(day, posted), {"key": "standup", "label": "Standup", "value": "posted 7:45",
                                                     "state": "ok"})
        self.assertEqual(self.standup(day, held={"state": "held"})["value"], "held back")
        self.assertEqual(self.standup(day, held={"state": "held"})["state"], "warn")
        self.assertEqual(self.standup(day, posted, held={"state": "held"})["state"], "ok")   # a later hand post wins
        for st in ("failed", "unclear", "reply-unclear"):
            self.assertEqual(self.standup(day, {"state": st})["state"], "bad", st)
        self.assertEqual(self.standup(day, {"state": "posting"}, unit={"ActiveState": "active"})["state"], "info")
        self.assertEqual(self.standup(day, {"state": "posting"})["state"], "bad")
        self.assertEqual(self.standup(day, {"state": "skip"})["state"], "info")
        self.assertEqual(self.standup(day)["value"], "no record")
        self.assertEqual(self.standup(day, unit={"ActiveState": "activating"})["value"], "drafting now")
        early = datetime(2026, 11, 23, 7, 30, tzinfo=TZ)
        self.assertEqual(self.standup(early), {"key": "standup", "label": "Standup", "value": "at 7:45",
                                               "state": "info"})
        saturday = datetime(2026, 11, 28, 10, 0, tzinfo=TZ)
        self.assertEqual(self.standup(saturday, {"state": "failed"})["state"], "info")

    def test_collect_adds_tiles_only_in_work_hours(self):
        off = status.collect()
        self.assertNotIn("linear", [c["key"] for c in off["checks"]])
        self.assertIn("uptime", [c["key"] for c in off["checks"]])
        self.assertFalse(off["work"])
        n = int(__import__("time").time())
        on = status.collect(feeds={"linear": {"at": n - 4 * H, "missing": []},
                                   "github": [{"at": n - 60, "status": "ok"}]})
        keys = [c["key"] for c in on["checks"]]
        self.assertEqual(keys[-3:], ["linear", "github", "standup"])
        self.assertNotIn("uptime", keys)
        self.assertEqual(len(on["checks"]), 8)             # the test settings' tiles; + assistant + verdict on the page
        self.assertEqual(on["overall"], "bad")             # a red Linear tile makes the verdict red
        self.assertIn("Linear: as of", " ".join(on["problems"]))


if __name__ == "__main__":
    unittest.main()
