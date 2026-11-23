"""Offline tests for the status strip (status.py), the sky (sky.py) and the server's routes.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.request
from datetime import date, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _env  # noqa: E402,F401  (settings for the tests)
import sky      # noqa: E402
import status   # noqa: E402


class StatusChecks(unittest.TestCase):
    def test_parse_show_and_ts(self):
        text = ("Id=a.service\nActiveState=active\nResult=success\n\n"
                "Id=b.timer\nNextElapseUSecRealtime=Wed 2026-11-18 05:45:00 UTC\n")
        u = status.parse_show(text)
        self.assertEqual(u["a.service"]["ActiveState"], "active")
        ts = status._ts(u["b.timer"]["NextElapseUSecRealtime"])
        self.assertEqual(datetime.fromtimestamp(ts, sky.TZ).strftime("%H:%M"), "05:45")
        self.assertEqual(status._ts("@1794900000"), 1794900000)
        self.assertIsNone(status._ts("n/a"))

    def test_job(self):
        now = 1794950000
        ok = {"ActiveState": "inactive", "Result": "success", "ExecMainExitTimestamp": f"@{now - 3600}"}
        self.assertEqual(status.check_job(now, ok)["state"], "ok")
        self.assertEqual(status.check_job(now, dict(ok, Result="exit-code"))["state"], "bad")
        self.assertEqual(status.check_job(now + 40 * 3600, ok)["state"], "bad")
        self.assertEqual(status.check_job(now + 40 * 3600, ok, max_age_h=48)["state"], "ok")
        self.assertEqual(status.check_job(now, dict(ok, ActiveState="active"))["state"], "info")
        self.assertEqual(status.check_job(now, {})["state"], "warn")
        c = status.check_job(now, ok, "sync", "Sync")
        self.assertEqual((c["key"], c["label"]), ("sync", "Sync"))

    def test_failed_and_services(self):
        self.assertEqual(status.check_failed(0)["state"], "ok")
        self.assertEqual(status.check_failed(2)["value"], "2 failed")
        self.assertEqual(status.check_failed(None)["state"], "warn")
        self.assertEqual(status.check_service("s", "S", {"ActiveState": "active"})["state"], "ok")
        self.assertEqual(status.check_service("s", "S", {"ActiveState": "failed"})["state"], "bad")

    def test_in_session_reads_mtime_only(self):
        with tempfile.TemporaryDirectory() as root:
            proj = os.path.join(root, "p")
            os.makedirs(proj)
            t = os.path.join(proj, "x.jsonl")
            open(t, "w").close()
            os.chmod(t, 0)            # unreadable: only the mtime may be used
            now = os.stat(t).st_mtime
            self.assertTrue(status.in_session(now + 30, root))
            self.assertFalse(status.in_session(now + 600, root))
            self.assertFalse(status.in_session(now, os.path.join(root, "missing")))

    def test_in_session_ignores_headless_runs(self):
        with tempfile.TemporaryDirectory() as root:
            for d in ("-home-user-jobs-nightly-run", "-tmp-claude-1000--home-user-x-scratchpad", "-tmp"):
                os.makedirs(os.path.join(root, d))
                open(os.path.join(root, d, "s.jsonl"), "w").close()
            sub = os.path.join(root, "-home-user", "sess", "subagents")
            os.makedirs(sub)
            t = os.path.join(sub, "a.jsonl")
            open(t, "w").close()
            now = os.stat(t).st_mtime
            self.assertFalse(status.in_session(now + 5, root))
            open(os.path.join(root, "-home-user", "sess.jsonl"), "w").close()
            self.assertTrue(status.in_session(now + 5, root))

    def test_tiles_follow_the_settings(self):
        tiles = [{"type": "disk", "path": "/", "label": "Root"}, {"type": "service", "unit": "x.service"},
                 {"type": "nonsense"}, {"type": "linear"}]
        with unittest.mock.patch.object(status, "_run", return_value=""):
            off = status.collect(tiles=tiles)
            on = status.collect(tiles=tiles, feeds={"linear": {"at": None, "missing": ["acme"]}, "github": []})
        self.assertEqual([c["key"] for c in off["checks"]], ["disk", "x.service"])
        self.assertEqual(off["checks"][0]["label"], "Root")
        self.assertEqual([c["key"] for c in on["checks"]], ["disk", "x.service", "linear"])

    def test_collect_has_no_private_fields(self):
        s = status.collect()
        self.assertIn(s["overall"], ("ok", "warn", "bad"))
        blob = json.dumps(s).lower()
        for word in ("token", "password", "$", "key_cmd"):
            self.assertNotIn(word, blob)
        self.assertEqual(s["branding"]["assistant"], "Agent")
        self.assertIn("sky", s)

    def test_dark_follows_the_power_hours(self):
        self.assertTrue(status._dark(datetime(2026, 11, 17, 2, 0, tzinfo=sky.TZ)))
        self.assertFalse(status._dark(datetime(2026, 11, 17, 7, 0, tzinfo=sky.TZ)))


class Sky(unittest.TestCase):
    def test_sun_times_at_the_configured_place(self):
        # tests/kiosk.test.toml: 45 N, 110 W, America/Denver
        rise, sset = sky.sun_times(date(2026, 11, 17))
        self.assertEqual((rise.strftime("%H:%M"), sset.strftime("%H:%M")), ("07:20", "16:50"))
        snap = sky.snapshot(datetime(2026, 11, 17, 13, 0, tzinfo=sky.TZ))
        self.assertEqual((snap["sunrise"], snap["sunset"], snap["daylight"]), ("7:20 AM", "4:50 PM", "9h 30m"))
        rise, sset = sky.sun_times(date(2026, 6, 21))
        self.assertTrue(15 * 3600 < (sset - rise).total_seconds() < 16 * 3600)

    def test_moon(self):
        full = sky.moon(datetime(2026, 11, 24, 8, 0, tzinfo=sky.TZ))       # full moon 2026-11-24
        self.assertGreater(full["illum"], 0.97)
        new = sky.moon(datetime(2026, 12, 9, 0, 0, tzinfo=sky.TZ))
        self.assertLess(new["illum"], 0.05)

    def test_solstice(self):
        self.assertEqual(sky._next_solstice(date(2026, 11, 17))["days"], 34)
        self.assertEqual(sky._next_solstice(date(2026, 12, 22))["date"], "2027-06-21")


class ServerRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server
        from http.server import ThreadingHTTPServer
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, ""

    def test_routes(self):
        self.assertEqual(self.get("/static/../server.py")[0], 404)
        self.assertEqual(self.get("/static/screen.js")[0], 200)
        self.assertEqual(self.get("/static/kiosk.toml")[0], 404)
        self.assertEqual(self.get("/paper/")[0], 404)
        self.assertEqual(self.get("/")[0], 200)
        code, body = self.get("/api/status")
        self.assertEqual(code, 200)
        data = json.loads(body)
        self.assertIn("overall", data)
        self.assertIn("build", data)

    def test_tasks_route(self):
        import server
        import tasks
        with tempfile.TemporaryDirectory() as d:
            notes = os.path.join(d, "notes.md")
            with open(notes, "w") as f:
                f.write("- 2026-11-17 WORK TASKS:\n  1. OPS-1 ship it: in review.\n"
                        "  2. OPS-2 see the dentist: soon.\n- next entry\n")
            old = server._tasks_src
            server._tasks_src = tasks.Source(notes=notes)
            try:
                for force, want in (("1", 2), ("", None)):
                    server._tasks_cache["at"] = 0.0
                    with unittest.mock.patch.dict(os.environ, {tasks.FORCE_ENV: force}), \
                            unittest.mock.patch.object(tasks, "in_hours", return_value=False):
                        code, body = self.get("/api/tasks")
                    self.assertEqual(code, 200)
                    data = json.loads(body)
                    if want:   # no word filter on the task list
                        self.assertEqual((len(data["items"]), data["total"]), (want, want))
                        self.assertIn("dentist", body)
                        self.assertNotIn("hidden", data)
                    else:
                        self.assertEqual(data["items"], [])
                        self.assertNotIn("dentist", body)
            finally:
                server._tasks_src = old
                server._tasks_cache["at"] = 0.0

    def test_csp_header(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as r:
            csp = r.headers.get("Content-Security-Policy", "")
        self.assertIn("default-src 'self'", csp)
        self.assertIn("connect-src 'self'", csp)
