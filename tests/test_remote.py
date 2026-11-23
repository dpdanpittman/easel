"""Offline tests for the remote: access gate, CSRF header, actions, the screen override with power.py.

Run: python3 -m unittest discover -s tests
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from http.server import ThreadingHTTPServer
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _env  # noqa: E402,F401  (settings for the tests)
import power    # noqa: E402
import remote   # noqa: E402
import tasks    # noqa: E402

TZ = power.TZ
NO_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def start(handler, kind="local", names=()):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.daemon_threads = True
    srv.kind, srv.names = kind, names
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class Gate(unittest.TestCase):
    NAMES = ("100.75.123.68", "easel-box.example.internal", "easel-box")

    def test_local(self):
        self.assertEqual(remote.gate("local", "127.0.0.1", "/", "127.0.0.1:8090", 8090), 0)
        self.assertEqual(remote.gate("local", "127.0.0.1", "/api/control", "localhost:8090", 8090), 0)
        self.assertEqual(remote.gate("local", "127.0.0.1", "/", "evil.example:8090", 8090), 403)   # DNS rebinding
        self.assertEqual(remote.gate("local", "172.16.1.20", "/", "127.0.0.1:8090", 8090), 403)

    def test_remote_listener(self):
        g = lambda peer, path="/remote", host="100.75.123.68:8090": remote.gate(   # noqa: E731
            "remote", peer, path, host, 8090, self.NAMES)
        self.assertEqual(g("100.66.102.44"), 0)
        self.assertEqual(g("100.66.102.44", host="easel-box.example.internal:8090"), 0)
        self.assertEqual(g("100.66.102.44", "/api/control"), 0)
        self.assertEqual(g("100.66.102.44", "/static/remote.js"), 0)
        for path in ("/", "/api/tasks", "/api/status", "/api/mode", "/api/inflight", "/api/done", "/static/screen.js"):
            self.assertEqual(g("100.66.102.44", path), 403, path)
        for peer in ("172.16.1.20", "172.16.5.9", "10.0.0.1", "127.0.0.1", "8.8.8.8", "100.128.0.1", "::1", "x"):
            self.assertEqual(g(peer), 403, peer)
        self.assertEqual(g("100.66.102.44", host="evil.example:8090"), 403)
        self.assertEqual(g("100.66.102.44", host=None), 403)

    def test_unknown_listener(self):
        self.assertEqual(remote.gate("lan", "172.16.1.20", "/remote", "172.16.1.10:8090", 8090), 403)


class Power(unittest.TestCase):
    def at(self, d, h, mi=0):
        return datetime(2026, 11, d, h, mi, tzinfo=TZ)

    def test_next_boundary(self):
        self.assertEqual(power.next_boundary(self.at(18, 14)), self.at(19, 1))
        self.assertEqual(power.next_boundary(self.at(18, 0, 30)), self.at(18, 1))
        self.assertEqual(power.next_boundary(self.at(18, 1)), self.at(18, 6))
        self.assertEqual(power.next_boundary(self.at(18, 3)), self.at(18, 6))
        self.assertEqual(power.next_boundary(self.at(18, 6)), self.at(19, 1))

    def test_override_until_next_boundary_then_schedule(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s", "override.json")
            until = power.set_override("off", self.at(18, 14), path)
            self.assertEqual(until, self.at(19, 1).timestamp())
            self.assertEqual(power.effective(self.at(18, 15), path), ("off", "override"))
            self.assertEqual(power.effective(self.at(19, 0, 59), path), ("off", "override"))
            self.assertEqual(power.effective(self.at(19, 1), path), ("off", "schedule"))
            self.assertEqual(power.effective(self.at(19, 6), path), ("on", "schedule"))
            # on in the night: holds until 06:00, which is on anyway
            power.set_override("on", self.at(19, 2), path)
            self.assertEqual(power.effective(self.at(19, 3), path), ("on", "override"))
            self.assertEqual(power.effective(self.at(19, 6), path), ("on", "schedule"))
            power.clear_override(path)
            self.assertEqual(power.effective(self.at(19, 3), path), ("off", "schedule"))
            power.clear_override(path)          # twice is fine

    def test_odd_files_mean_schedule(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "override.json")
            now = self.at(18, 3)
            for body in ("not json", "{}", '{"want": "maybe", "set_at": 0, "until": 9e12}',
                         json.dumps({"want": "on", "set_at": now.timestamp(), "until": now.timestamp() + 30 * 3600}),
                         json.dumps({"want": "on", "set_at": now.timestamp() + 7200, "until": now.timestamp() + 9000})):
                with open(path, "w") as f:
                    f.write(body)
                self.assertEqual(power.effective(now, path), ("off", "schedule"), body)

    def _main(self, path, now, enabled):
        calls = []

        def fake(args, **kw):
            calls.append(args)
            return mock.Mock(returncode=0, stdout=f'HDMI-A-1 "x"\n  Enabled: {enabled}\n', stderr="")

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "wayland-0"), "w").close()
            with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": d, "WAYLAND_DISPLAY": "wayland-0"}), \
                    mock.patch("subprocess.run", side_effect=fake), mock.patch.object(power, "datetime", Clock):
                self.assertEqual(power.main([], path), 0)
        return calls

    def test_main_obeys_override_then_resumes(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "override.json")
            power.set_override("on", self.at(18, 2), path)
            # 03:00, the schedule says off, the override says on: the output stays on
            self.assertEqual(self._main(path, self.at(18, 3), "yes"), [["wlr-randr"]])
            self.assertTrue(os.path.exists(path))
            # 06:00 boundary passed: override expired and removed, the schedule (on) decides
            self.assertEqual(self._main(path, self.at(18, 7), "yes"), [["wlr-randr"]])
            self.assertFalse(os.path.exists(path))
            # off now at 14:00: switched off, and stays off at 15:00
            power.set_override("off", self.at(18, 14), path)
            self.assertEqual(self._main(path, self.at(18, 15), "yes"),
                             [["wlr-randr"], ["wlr-randr", "--output", "HDMI-A-1", "--off"]])
            # at 01:00 the schedule takes over (dark until 06:00), then on at 06:00
            self.assertEqual(self._main(path, self.at(19, 2), "no"), [["wlr-randr"]])
            self.assertFalse(os.path.exists(path))
            self.assertEqual(self._main(path, self.at(19, 6), "no"),
                             [["wlr-randr"], ["wlr-randr", "--output", "HDMI-A-1", "--on"]])


class Server(unittest.TestCase):
    """The real server handler with a Remote on a test clock."""

    def setUp(self):
        import server
        self.server = server
        self.tmp = tempfile.TemporaryDirectory()
        self.logs = []
        self.now = [datetime(2026, 11, 18, 14, 0, tzinfo=TZ).timestamp()]
        self.applied = []
        self.old = server.REMOTE
        server.REMOTE = remote.Remote(clock=lambda: self.now[0], apply_power=lambda: self.applied.append(1),
                                      override_path=os.path.join(self.tmp.name, "override.json"),
                                      log=self.logs.append)
        server._limit = remote.RateLimit(rate=0, burst=1000)
        self.p_out = mock.patch.object(power, "outputs", return_value={"HDMI-A-1": True})
        self.p_out.start()
        self.stdout = io.StringIO()
        self.redirect = contextlib.redirect_stdout(self.stdout)
        self.redirect.__enter__()
        self.srv = start(server.Handler)
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.bodies = []

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.redirect.__exit__(None, None, None)
        self.p_out.stop()
        self.server.REMOTE = self.old
        self.server._limit = remote.RateLimit()
        self.tmp.cleanup()
        for line in self.logs:
            self.assertRegex(line, r"^remote: [a-z.-]+ \(n=\d+\)$")

    def req(self, path, method="GET", body=None, headers=None, raw=None):
        h = dict(headers or {})
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with NO_PROXY.open(r, timeout=10) as resp:
                out = resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as e:
            out = e.code, e.read(), dict(e.headers)
        self.bodies.append(out[1].decode("latin-1") + json.dumps(out[2]))
        return out

    def act(self, action, **args):
        code, body, _ = self.req("/api/control", "POST", {"action": action, "args": args}, {"X-Easel-Remote": "1"})
        return code, json.loads(body or b"{}")

    def mode(self):
        return json.loads(self.req("/api/mode")[1])

    def test_csrf(self):
        good = {"action": "panel.set", "args": {"panel": "tasks"}}
        self.assertEqual(self.req("/api/control", "POST", good)[0], 403)                       # no header
        self.assertEqual(self.req("/api/control", "POST", good, {"X-Easel-Remote": "0"})[0], 403)
        self.assertEqual(self.req("/api/control", "POST", raw=b"action=panel.set", headers={
            "X-Easel-Remote": "1", "Content-Type": "application/x-www-form-urlencoded"})[0], 403)
        self.assertEqual(self.req("/api/control", "POST", good, {"X-Easel-Remote": "1",
                                                                 "Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.req("/api/control", "POST", good, {"X-Easel-Remote": "1",
                                                                 "Sec-Fetch-Site": "cross-site"})[0], 403)
        code, _, hdrs = self.req("/api/control", "OPTIONS")
        self.assertEqual(code, 405)
        self.assertNotIn("Access-Control-Allow-Origin", hdrs)
        self.assertEqual(self.req("/api/control")[0], 404)                                     # GET does nothing
        self.assertEqual(self.mode()["panel"], "auto")
        self.assertEqual(self.req("/api/control", "POST", good, {
            "X-Easel-Remote": "1", "Origin": self.base, "Sec-Fetch-Site": "same-origin"})[0], 200)
        self.assertEqual(self.mode()["panel"], "tasks")
        self.assertEqual(self.req("/api/control", "POST", raw=b"x" * 5000, headers={
            "X-Easel-Remote": "1", "Content-Type": "application/json"})[0], 400)
        self.assertEqual(self.act("nope.nope")[0], 400)
        self.assertEqual(self.act("x")[0], 400)

    def test_remote_listener_refuses_a_peer_outside_its_networks(self):
        t = start(self.server.Handler, "remote", ("127.0.0.1",))
        try:
            base = f"http://127.0.0.1:{t.server_address[1]}"
            for path in ("/remote", "/api/remote/state", "/", "/api/tasks"):
                with self.assertRaises(urllib.error.HTTPError) as e:
                    NO_PROXY.open(base + path, timeout=5)
                self.assertEqual(e.exception.code, 403, path)
        finally:
            t.shutdown()
            t.server_close()

    def test_local_listener_refuses_foreign_host(self):
        self.assertEqual(self.req("/remote", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.req("/remote")[0], 200)
        self.assertEqual(self.req("/static/remote.js")[0], 200)

    def test_tasks_panel_lifts_only_the_hours(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "config.json")
            with open(cfg, "w") as f:
                json.dump({"notes": os.path.join(d, "h.md")}, f)
            with open(os.path.join(d, "h.md"), "w") as f:
                f.write("- 2026-11-17 SAM'S WORK TASKS:\n  1. OPS-1 ship it: in review.\n"
                        "  2. OPS-2 see the dentist: soon.\n- next entry\n")
            old = self.server._tasks_src
            self.server._tasks_src = tasks.Source(notes=_env.notes_of(cfg))
            try:
                with mock.patch.dict(os.environ, {tasks.FORCE_ENV: ""}), \
                        mock.patch.object(tasks, "in_hours", return_value=False):
                    self.server._tasks_cache["at"] = 0.0
                    self.assertEqual(json.loads(self.req("/api/tasks")[1])["items"], [])
                    self.act("panel.set", panel="tasks")
                    self.assertEqual(self.mode()["panel"], "tasks")
                    data = json.loads(self.req("/api/tasks")[1])
                    self.assertEqual([it["n"] for it in data["items"]], [1, 2])   # the same list, 1..N, nothing hidden
                    self.assertIn("dentist", json.dumps(data))
                    self.act("panel.normal")
                    self.assertEqual(json.loads(self.req("/api/tasks")[1])["items"], [])
            finally:
                self.server._tasks_src = old
                self.server._tasks_cache["at"] = 0.0

    def test_inflight_follows_work_hours_and_the_remote_lifts_them(self):
        import inflight
        import tasks

        class Src:
            def current(self):
                return dict(inflight.EMPTY, waiting=[{"text": "work item"}], notes="ok")
        old = self.server._inflight_src
        self.server._inflight_src = Src()
        try:
            with mock.patch.dict(os.environ, {tasks.FORCE_ENV: ""}):
                with mock.patch.object(tasks, "in_hours", return_value=True):
                    self.server._inflight_cache["at"] = 0.0
                    data = json.loads(self.req("/api/inflight")[1])
                    self.assertTrue(data["show"])                     # in hours
                    self.assertEqual(data["waiting"], [{"text": "work item"}])
                with mock.patch.object(tasks, "in_hours", return_value=False):
                    self.server._inflight_cache["at"] = 0.0
                    data = json.loads(self.req("/api/inflight")[1])
                    self.assertFalse(data["show"])                    # after hours: hidden
                    self.assertNotIn("work item", json.dumps(data))
                    self.act("panel.set", panel="inflight")           # the remote only lifts the hours
                    data = json.loads(self.req("/api/inflight")[1])
                    self.assertTrue(data["show"])
                    self.assertEqual(data["waiting"], [{"text": "work item"}])
                    self.act("panel.normal")
                    self.assertFalse(json.loads(self.req("/api/inflight")[1])["show"])
                    self.act("panel.set", panel="tasks")              # Tasks lifts only the task list's hours
                    self.assertFalse(json.loads(self.req("/api/inflight")[1])["show"])
                    self.act("panel.normal")
        finally:
            self.server._inflight_src = old
            self.server._inflight_cache["at"] = 0.0

    def test_showing_follows_work_hours(self):
        showing = lambda: json.loads(self.req("/api/remote/state")[1])["showing"]   # noqa: E731
        self.assertEqual(showing(), "Work tasks and In flight")                                   # Wed 14:00
        self.now[0] = datetime(2026, 11, 18, 16, 0, tzinfo=TZ).timestamp()
        self.assertEqual(showing(), "Off hours")
        self.now[0] = datetime(2026, 11, 21, 10, 0, tzinfo=TZ).timestamp()                        # Saturday
        self.assertEqual(showing(), "Off hours")

    def test_inflight_panel(self):
        self.assertEqual(self.act("panel.set", panel="inflight")[0], 200)
        self.assertEqual(self.mode()["panel"], "inflight")
        self.assertEqual(json.loads(self.req("/api/remote/state")[1])["showing"], "In flight (full size)")
        self.act("panel.normal")
        self.assertEqual(self.mode()["panel"], "auto")
        self.assertEqual(self.act("panel.set", panel="nope")[0], 400)

    def test_screen_override_and_night_reset(self):
        r = self.server.REMOTE
        self.act("screen.off")
        self.assertEqual(self.applied, [1])
        st = json.loads(self.req("/api/remote/state")[1])["screen"]
        self.assertEqual((st["want"], st["source"]), ("off", "override"))
        self.assertIn("1:00 AM", st["next"])
        self.act("screen.on")
        self.assertEqual(power.effective(datetime.fromtimestamp(self.now[0], TZ), r.override_path), ("on", "override"))
        self.act("screen.auto")
        self.assertFalse(os.path.exists(r.override_path))
        self.assertEqual(len(self.applied), 3)
        # a panel chosen at 14:00 holds through the evening and resets when the screen goes dark at 01:00
        self.act("panel.set", panel="tasks")
        self.now[0] = datetime(2026, 11, 19, 0, 59, tzinfo=TZ).timestamp()
        self.assertEqual(self.mode()["panel"], "tasks")
        self.now[0] = datetime(2026, 11, 19, 1, 1, tzinfo=TZ).timestamp()
        self.assertEqual(self.mode()["panel"], "auto")

    def test_rate_limit(self):
        self.server._limit = remote.RateLimit(rate=0, burst=3)
        codes = [self.act("panel.normal")[0] for _ in range(5)]
        self.assertEqual(codes, [200, 200, 200, 429, 429])


class RateLimit(unittest.TestCase):
    def test_bucket_refills(self):
        t = [0.0]
        rl = remote.RateLimit(rate=1.0, burst=2, clock=lambda: t[0])
        self.assertEqual([rl.allow("a"), rl.allow("a"), rl.allow("a")], [True, True, False])
        self.assertTrue(rl.allow("b"))
        t[0] = 1.0
        self.assertTrue(rl.allow("a"))
        self.assertFalse(rl.allow("a"))



if __name__ == "__main__":
    unittest.main()
