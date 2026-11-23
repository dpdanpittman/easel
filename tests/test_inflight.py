"""Offline tests for the In flight panel (inflight.py): the notes block parser, the GitHub fetch (a fake runner, no
gh, no network), the merge and de-duplication, and the /api/inflight route.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _env  # noqa: E402,F401  (settings for the tests)
sys.path.insert(0, os.path.dirname(HERE))
import inflight  # noqa: E402
import tasks     # noqa: E402

TZ = inflight.TZ
NOW = datetime(2026, 11, 19, 12, 50, tzinfo=TZ)

BLOCK = """- 2026-11-19 09:00 IN FLIGHT (current):
  W. an older list
- 2026-11-19 12:45 IN FLIGHT (current):
  W. OPS-577: apply the staging stack and create the CI tokens
  W. old token: delete the copy in the team vault
  R. 12:33 ACME-4810 scripts build
  R. 12:42 easel In flight panel
  F. 12:29 infra#24 merged
  F. 12:41 easel task list fix live
  a stray indented note
- 2026-11-19 12:46 next notes entry mentions "- 2026-11-19 13:00 IN FLIGHT" in quotes
""".split("\n")


def ts(h, m, day=19):
    return int(datetime(2026, 11, day, h, m, tzinfo=TZ).timestamp())


class Parser(unittest.TestCase):
    def test_newest_block_and_groups(self):
        b = inflight.parse(BLOCK, NOW)
        self.assertEqual(b["state"], "ok")
        self.assertEqual(b["at"], ts(12, 45))
        self.assertEqual([w["text"] for w in b["waiting"]],
                         ["OPS-577: apply the staging stack and create the CI tokens",
                          "old token: delete the copy in the team vault"])
        self.assertEqual([(r["text"], r["ts"]) for r in b["running"]],
                         [("ACME-4810 scripts build", ts(12, 33)), ("easel In flight panel", ts(12, 42))])
        # newest first, whatever the written order
        self.assertEqual([f["text"] for f in b["finished"]], ["easel task list fix live", "infra#24 merged"])
        self.assertNotIn("stray", json.dumps(b))
        self.assertNotIn("older", json.dumps(b))

    def test_block_ends_at_empty_or_unindented_line(self):
        lines = ["- 2026-11-19 12:45 IN FLIGHT:", "  W. one", "", "  W. after the gap"]
        self.assertEqual([w["text"] for w in inflight.parse(lines, NOW)["waiting"]], ["one"])
        lines = ["- 2026-11-19 12:45 IN FLIGHT", "  W. one", "not indented", "  W. two"]
        self.assertEqual(len(inflight.parse(lines, NOW)["waiting"]), 1)

    def test_header_must_be_at_column_zero_and_exact(self):
        for bad in ("  - 2026-11-19 12:45 IN FLIGHT:", "- 2026-11-19 12:45 mt IN FLIGHT:",
                    "- see 2026-11-19 12:45 IN FLIGHT", "- 2026-11-19 12:45 IN FLIGHTS",
                    "x- 2026-11-19 12:45 IN FLIGHT"):
            self.assertEqual(inflight.parse([bad, "  W. x"], NOW)["state"], "missing", bad)

    def test_missing_block(self):
        b = inflight.parse(["- 2026-11-19 SAM'S WORK TASKS:", "  1. OPS-1 x: y"], NOW)
        self.assertEqual((b["state"], b["waiting"], b["running"], b["finished"]), ("missing", [], [], []))
        self.assertEqual(inflight.parse([], NOW)["state"], "missing")

    def test_stale_block(self):
        lines = ["- 2026-11-18 18:00 IN FLIGHT (current):", "  W. yesterday's"]
        b = inflight.parse(lines, NOW)
        self.assertEqual(b["state"], "stale")
        self.assertEqual(b["waiting"], [])
        self.assertEqual(inflight.parse(lines, datetime(2026, 11, 19, 11, 59, tzinfo=TZ))["state"], "ok")

    def test_bad_times(self):
        lines = ["- 2026-11-19 12:45 IN FLIGHT:", "  R. 25:10 no hour like that", "  R. 12:7 short",
                 "  R. later today without a time", "  F. 12:61 bad minute", "  F. 12:40 good"]
        b = inflight.parse(lines, NOW)
        self.assertEqual([(r["text"], r["ts"]) for r in b["running"]],
                         [("25:10 no hour like that", None), ("12:7 short", None),
                          ("later today without a time", None)])
        self.assertEqual([(f["text"], f["ts"]) for f in b["finished"]], [("good", ts(12, 40)),
                                                                        ("12:61 bad minute", None)])
        # a bad header date is no block at all
        self.assertEqual(inflight.parse(["- 2026-13-40 12:45 IN FLIGHT:", "  W. x"], NOW)["state"], "missing")
        self.assertEqual(inflight.parse(["- 2026-11-19 29:45 IN FLIGHT:", "  W. x"], NOW)["state"], "missing")

    def test_time_after_header_is_the_day_before(self):
        lines = ["- 2026-11-19 00:10 IN FLIGHT:", "  R. 23:50 overnight build", "  F. 00:05 landed"]
        b = inflight.parse(lines, datetime(2026, 11, 19, 0, 20, tzinfo=TZ))
        self.assertEqual(b["running"][0]["ts"], ts(23, 50, day=18))
        self.assertEqual(b["finished"][0]["ts"], ts(0, 5))

    def test_finished_capped_at_six(self):
        lines = ["- 2026-11-19 12:45 IN FLIGHT:"] + ["  F. 12:%02d thing %d" % (i, i) for i in range(10)]
        f = inflight.parse(lines, NOW)["finished"]
        self.assertEqual([x["text"] for x in f], ["thing 9", "thing 8", "thing 7", "thing 6", "thing 5", "thing 4"])

    def test_untrusted_text(self):
        long = "word " * 60
        lines = ["- 2026-11-19 12:45 IN FLIGHT:",
                 "  W. <script>alert(1)</script> merge `iac`: see https://evil.example/x?token=abc now",
                 "  W. bell\x07 and ‮ reversed",
                 "  W. " + long,
                 "  R. 12:00 **bold** www.example.com/thing"]
        b = inflight.parse(lines, NOW)
        w = [x["text"] for x in b["waiting"]]
        self.assertNotIn("https://", w[0])
        self.assertNotIn("token=abc", w[0])
        self.assertNotIn("`", w[0])
        self.assertIn("<script>", w[0])       # kept as text: the page only ever sets textContent
        self.assertNotIn("\x07", w[1])
        self.assertNotIn("‮", w[1])
        self.assertLessEqual(len(w[2]), inflight.TEXT_MAX)
        self.assertTrue(w[2].endswith("…"))
        self.assertEqual(b["running"][0]["text"], "bold")

    def test_example_block_from_the_brief(self):
        text = """- 2026-11-19 12:47 IN FLIGHT (current):
  W. OPS-577: apply the staging stack, create the CI tokens and the GitHub environment
  W. OPS-576: say whether to put the central data repo forward
  W. old token: delete the copy left in the team vault
  R. 12:33 ACME-4810 scripts build
  R. 12:43 easel In flight panel
  F. 12:41 easel task list: no hidden rows, numbered 1 to N
  F. 12:34 infra#26 updated, plan shows 10 imports
  F. 12:29 infra#24 merged
  F. 12:23 OPS-580 closed, log ingest fixed
  F. 12:15 OPS-574 closed, on staging
"""
        b = inflight.parse(text.split("\n"), NOW)
        self.assertEqual((len(b["waiting"]), len(b["running"]), len(b["finished"])), (3, 2, 5))


# --- GitHub -----------------------------------------------------------------------------------------------------
FAKE_TOKEN = "gho_" + "FAKETOKEN" * 4


def pr(repo, num, title, state="SUCCESS", review=None, draft=False, owner="globex-labs"):
    commits = {"nodes": [{"commit": {"statusCheckRollup": {"state": state} if state else None}}]}
    return {"number": num, "title": title, "isDraft": draft, "reviewDecision": review,
            "repository": {"name": repo, "nameWithOwner": owner + "/" + repo}, "commits": commits}


def gh_doc(globex=(), acme=()):
    return json.dumps({"data": {"o0": {"nodes": list(globex)}, "o1": {"nodes": list(acme)}}})


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Runner:
    def __init__(self, out=None, code=0, exc=None):
        self.out, self.code, self.exc, self.calls = out, code, exc, []

    def __call__(self, args, timeout):
        self.calls.append((args, timeout))
        if self.exc:
            raise self.exc
        return self.code, self.out


class GitHubFetch(unittest.TestCase):
    def make(self, runner):
        self.clock, self.logs = Clock(), []
        return inflight.GitHub(run=runner, clock=self.clock, wall=lambda: 1_700_000_000, log=self.logs.append)

    def test_success(self):
        run = Runner(gh_doc([pr("infra", 26, "Plan the stack", "FAILURE"),
                             pr("infra", 27, "Green one", "SUCCESS", "APPROVED"),
                             pr("draft-repo", 1, "a draft", draft=True)],
                            [pr("api", 9, "Running", "PENDING", "REVIEW_REQUIRED", owner="acme-inc"),
                             pr("api", 10, "No checks", None, owner="acme-inc")]))
        g = self.make(run)
        g.refresh()
        snap = g.snapshot()
        self.assertEqual(snap["status"], "ok")
        self.assertEqual(snap["at"], 1_700_000_000)
        got = {(p["repo"], p["number"]): (p["ci"], p["review"]) for p in snap["prs"]}
        self.assertEqual(got, {("infra", 26): ("failing", ""), ("infra", 27): ("passing", "approved"),
                               ("api", 9): ("running", "review required"), ("api", 10): ("none", "")})
        args, timeout = run.calls[0]
        self.assertEqual(args[:3], ["gh", "api", "graphql"])
        self.assertEqual(timeout, inflight.GH_TIMEOUT_S)
        q = args[-1]
        for want in ("author:octocat", "org:globex-labs", "org:acme-inc", "draft:false", "statusCheckRollup",
                     "reviewDecision"):
            self.assertIn(want, q)
        self.assertFalse(any("token" in a.lower() for a in args))

    def test_rejects_foreign_orgs_and_bad_fields(self):
        g = self.make(Runner(gh_doc([pr("x", 1, "other org", owner="evil"), pr("bad repo!", 2, "t"),
                                     {"number": "3", "title": "t", "isDraft": False}, None, pr("ok", 4, "fine")])))
        g.refresh()
        self.assertEqual([p["number"] for p in g.snapshot()["prs"]], [4])

    def test_failure_exit_code(self):
        g = self.make(Runner("Bad credentials " + FAKE_TOKEN, code=1))
        g.refresh()
        snap = g.snapshot()
        self.assertEqual((snap["status"], snap["prs"]), ("off", []))
        self.assertEqual(self.logs, ["inflight: github fetch failed (RuntimeError exit 1)"])

    def test_timeout_and_missing_gh(self):
        for exc in (subprocess.TimeoutExpired(["gh"], 30, output=FAKE_TOKEN), FileNotFoundError("gh " + FAKE_TOKEN)):
            g = self.make(Runner(exc=exc))
            g.refresh()                                    # never raises
            self.assertEqual(g.snapshot()["status"], "off")
            self.assertEqual(len(self.logs), 1)
            self.assertNotIn(FAKE_TOKEN, self.logs[0])
            self.assertNotIn("gh ", self.logs[0])

    def test_garbage_output(self):
        for out in ("not json", json.dumps({"errors": [{"message": FAKE_TOKEN}]}), json.dumps([1]), None,
                    "x" * (inflight.MAX_OUT + 1)):
            g = self.make(Runner(out))
            g.refresh()
            self.assertEqual(g.snapshot()["status"], "off")
            self.assertNotIn(FAKE_TOKEN, json.dumps(self.logs) + json.dumps(g.snapshot()))

    def test_ttls_last_good_and_log_rate(self):
        run = Runner(gh_doc([pr("r", 1, "t")]))
        g = self.make(run)
        self.assertEqual(g.snapshot()["status"], "pending")
        g.refresh()
        run.code, run.out = 1, ""
        self.clock.t += 600
        g.refresh()
        snap = g.snapshot()
        self.assertEqual((snap["status"], len(snap["prs"])), ("stale", 1))     # last good, within the hour
        self.clock.t += 600
        g.refresh()
        self.assertEqual(len(self.logs), 1)                                      # one journal line an hour
        self.clock.t += inflight.GOOD_FOR_S
        snap = g.snapshot()
        self.assertEqual((snap["status"], snap["prs"]), ("off", []))
        g.refresh()
        self.assertEqual(len(self.logs), 2)

    def test_maybe_refresh_at_most_every_five_minutes(self):
        started = threading.Event()
        run = Runner(gh_doc())

        def counting(args, timeout):
            started.set()
            return run(args, timeout)
        g = self.make(counting)
        g.maybe_refresh()
        self.assertTrue(started.wait(5))
        for _ in range(100):
            if not g._running:
                break
            threading.Event().wait(0.01)
        g.maybe_refresh()
        g.maybe_refresh()
        self.assertEqual(len(run.calls), 1)
        self.clock.t += inflight.FETCH_EVERY_S
        started.clear()
        g.maybe_refresh()
        self.assertTrue(started.wait(5))
        for _ in range(100):
            if len(run.calls) == 2:
                break
            threading.Event().wait(0.01)
        self.assertEqual(len(run.calls), 2)


class Merge(unittest.TestCase):
    def block(self, waiting):
        lines = ["- 2026-11-19 12:45 IN FLIGHT:"] + ["  W. " + w for w in waiting]
        return inflight.parse(lines, NOW)

    def snap(self, *prs):
        return {"prs": [dict(repo=r, owner_repo="globex-labs/" + r, number=n, title=t, ci=ci, review=rv)
                        for r, n, t, ci, rv in prs], "status": "ok", "at": 1}

    def test_green_pr_waits_on_dan_others_go_to_open_prs(self):
        out = inflight.merge(self.block(["decide OPS-576"]),
                             self.snap(("infra", 27, "Green", "passing", ""),
                                       ("infra", 26, "Red", "failing", ""),
                                       ("api", 3, "Busy", "running", "approved")))
        self.assertEqual([w["text"] for w in out["waiting"]], ["decide OPS-576", "merge infra#27: Green"])
        self.assertEqual(out["waiting"][1]["ci"], "passing")
        self.assertEqual([(p["repo"], p["number"], p["ci"]) for p in out["prs"]],
                         [("infra", 26, "failing"), ("api", 3, "running")])

    def test_dedup_notes_line_naming_the_pr(self):
        out = inflight.merge(self.block(["merge infra#27 once Priya is happy",
                                         "look at globex-labs/infra#26 plan"]),
                             self.snap(("infra", 27, "Green", "passing", "approved"),
                                       ("infra", 26, "Red", "failing", "")))
        self.assertEqual(len(out["waiting"]), 2)
        self.assertEqual(out["prs"], [])
        self.assertEqual((out["waiting"][0]["ci"], out["waiting"][0]["review"]), ("passing", "approved"))
        self.assertEqual(out["waiting"][1]["ci"], "failing")
        self.assertNotIn("ref", out["waiting"][0])

    def test_no_github(self):
        out = inflight.merge(self.block(["x"]), {"prs": [], "status": "off"})
        self.assertEqual((len(out["waiting"]), out["prs"]), (1, []))


class SourceAndRoute(unittest.TestCase):
    def write(self, d, text):
        cfg = os.path.join(d, "config.json")
        with open(cfg, "w") as f:
            json.dump({"notes": os.path.join(d, "h.md")}, f)
        with open(os.path.join(d, "h.md"), "w") as f:
            f.write(text)
        return cfg

    def test_source_never_raises(self):
        class Boom:
            def maybe_refresh(self):
                raise RuntimeError("x")

            def snapshot(self):
                raise RuntimeError("x")
        with tempfile.TemporaryDirectory() as d:
            src = inflight.Source(notes=_env.notes_of(os.path.join(d, "missing.json")), github=Boom(), now=lambda: NOW)
            data = inflight.payload(src, NOW)
            self.assertEqual((data["notes"], data["github"], data["waiting"]), ("missing", "off", []))
            cfg = self.write(d, "\n".join(BLOCK))
            data = inflight.payload(inflight.Source(notes=_env.notes_of(cfg), github=Boom(), now=lambda: NOW), NOW)
            self.assertEqual((data["notes"], len(data["waiting"]), len(data["running"])), ("ok", 2, 2))

    def test_route(self):
        import server
        from http.server import ThreadingHTTPServer
        with tempfile.TemporaryDirectory() as d:
            cfg = self.write(d, "\n".join(BLOCK))
            run = Runner(gh_doc([pr("infra", 27, "Green", "SUCCESS"), pr("infra", 26, "Red", "FAILURE")]))
            gh = inflight.GitHub(run=run, log=lambda line: None)
            gh.refresh()
            old = server._inflight_src
            server._inflight_src = inflight.Source(notes=_env.notes_of(cfg), github=gh, now=lambda: NOW)
            server._inflight_cache["at"] = 0.0
            hours = unittest.mock.patch.object(tasks, "in_hours", return_value=True)
            hours.start()
            srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            srv.kind, srv.names = "local", ()
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/api/inflight" % srv.server_address[1],
                                            timeout=5) as r:
                    self.assertEqual(r.headers["Content-Type"], "application/json")
                    data = json.loads(r.read())
            finally:
                srv.shutdown()
                srv.server_close()
                hours.stop()
                server._inflight_src = old
                server._inflight_cache["at"] = 0.0
            self.assertEqual(set(data), {"waiting", "running", "finished", "prs", "notes", "block_at", "github",
                                         "github_at", "now", "show", "counts", "agents"})
            # the real counts are always in the answer (the page puts them in the headings)
            self.assertEqual(data["counts"], {"waiting": 3, "running": 2, "finished": 2, "prs": 1})
            self.assertTrue(data["show"])
            self.assertEqual([w["text"] for w in data["waiting"]][-1], "merge infra#27: Green")
            self.assertEqual([p["number"] for p in data["prs"]], [26])
            self.assertEqual(data["github"], "ok")
            self.assertEqual(len(run.calls), 1)          # the fresh result was used; no second fetch inside 5 min

    def test_route_hidden_after_hours(self):
        import server
        old, src = server._inflight_src, Counting()
        server._inflight_src = src
        server._inflight_cache["at"] = 0.0
        try:
            with unittest.mock.patch.dict(os.environ, {tasks.FORCE_ENV: ""}), \
                    unittest.mock.patch.object(tasks, "in_hours", return_value=False):
                data = json.loads(server.inflight_body())
        finally:
            server._inflight_src = old
            server._inflight_cache["at"] = 0.0
        self.assertFalse(data["show"])
        self.assertEqual((data["waiting"], data["running"], data["finished"], data["prs"]), ([], [], [], []))
        self.assertEqual(src.calls, 0)

    def test_tailnet_listener_does_not_serve_it(self):
        import remote
        self.assertEqual(remote.gate("tailnet", "100.100.1.1", "/api/inflight", "100.75.123.68:8090", 8090,
                                     ("100.75.123.68",)), 403)


class Counting:
    """A source that counts reads and always has something in flight (so a leak would show)."""

    def __init__(self):
        self.calls = 0

    def current(self):
        self.calls += 1
        return dict(inflight.EMPTY, waiting=[{"text": "work item"}], notes="ok")


class WorkHours(unittest.TestCase):
    """The panel follows the task list's hours (tasks.shown): , "fix the past 4pm screen"."""

    def at(self, *a):
        return datetime(*a, tzinfo=TZ)

    def setUp(self):
        self.env = unittest.mock.patch.dict(os.environ, {tasks.FORCE_ENV: ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_in_hours_shows_the_panel(self):
        src = Counting()
        for t in (self.at(2026, 11, 19, 8, 0), self.at(2026, 11, 19, 12, 50), self.at(2026, 11, 19, 15, 59)):
            data = inflight.payload(src, t)
            self.assertTrue(data["show"], t)
            self.assertEqual(data["waiting"], [{"text": "work item"}])
        self.assertEqual(src.calls, 3)

    def test_out_of_hours_hides_it_without_reading_anything(self):
        src = Counting()
        for t in (self.at(2026, 11, 19, 16, 0), self.at(2026, 11, 19, 20, 30), self.at(2026, 11, 19, 7, 59)):
            data = inflight.payload(src, t)
            self.assertFalse(data["show"], t)
            self.assertEqual((data["waiting"], data["running"], data["finished"], data["prs"]), ([], [], [], []))
            self.assertEqual(data["notes"], "hidden")
            self.assertNotIn("work item", json.dumps(data))
        self.assertEqual(src.calls, 0)                   # neither the notes nor GitHub is touched after hours

    def test_weekend_hidden(self):
        src = Counting()
        for t in (self.at(2026, 11, 21, 10, 0), self.at(2026, 11, 22, 12, 0)):    # Saturday, Sunday mid-day
            self.assertFalse(inflight.payload(src, t)["show"], t)
        self.assertEqual(src.calls, 0)

    def test_forced_by_the_remote_or_test_env(self):
        src = Counting()
        for t in (self.at(2026, 11, 19, 20, 30), self.at(2026, 11, 21, 10, 0)):
            data = inflight.payload(src, t, force=True)
            self.assertTrue(data["show"])
            self.assertEqual(data["waiting"], [{"text": "work item"}])
        with unittest.mock.patch.dict(os.environ, {tasks.FORCE_ENV: "1"}):
            self.assertTrue(inflight.payload(src, self.at(2026, 11, 21, 10, 0))["show"])

    def test_one_rule_shared_with_the_task_list(self):
        for t in (self.at(2026, 11, 19, 7, 59), self.at(2026, 11, 19, 8, 0), self.at(2026, 11, 19, 16, 0),
                  self.at(2026, 11, 21, 10, 0)):
            self.assertEqual(inflight.payload(Counting(), t)["show"], tasks.in_hours(t), t)
        with unittest.mock.patch.object(tasks, "shown", return_value=False) as rule:
            self.assertFalse(inflight.payload(Counting(), self.at(2026, 11, 19, 12, 0))["show"])
            rule.assert_called_once()

    def test_page_defaults_to_hidden_work_panels(self):
        # before the first answer from the server (and whenever it says hidden) no work panel shows; the server's
        # answer decides, never the page's clock
        with open(os.path.join(os.path.dirname(HERE), "web", "index.html")) as f:
            html = f.read()
        self.assertRegex(html, r'<section id="flightpanel" class="panel" hidden>')
        self.assertRegex(html, r'<section id="taskpanel" class="panel" hidden>')
        self.assertRegex(html, r'<section id="offpanel" class="panel" hidden>')
        with open(os.path.join(os.path.dirname(HERE), "web", "screen.js")) as f:
            js = f.read()
        self.assertIn('var showFlight = flightOn && (panel === "auto" || panel === "inflight");', js)
        self.assertIn('$("offpanel").hidden = !(panel === "auto" && !showFlight && !work);', js)

    def test_timezone_label_is_optional(self):
        for header in ("- 2026-11-19 12:45 IN FLIGHT:", "- 2026-11-19 12:45 MT IN FLIGHT:",
                       "- 2026-11-19 12:45 UTC IN FLIGHT (current):"):
            self.assertEqual(inflight.parse([header, "  W. x"], NOW)["state"], "ok", header)


if __name__ == "__main__":
    unittest.main()
