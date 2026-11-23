"""Offline tests for the grid scene: the state mapping (status.grid_nodes, from settings `scene.nodes`) and the page
wiring (web/grid.js).

Run: python3 -m unittest discover -s tests
"""
import os
import re
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _env  # noqa: E402,F401  (settings for the tests)
ROOT = os.path.dirname(HERE)
import remote   # noqa: E402
import status   # noqa: E402

WEB = os.path.join(ROOT, "web")
NOW = 1_795_000_000


def read(name):
    with open(os.path.join(WEB, name), encoding="utf-8") as f:
        return f.read()


def unit(active="inactive", start=None, end=None, result="success"):
    u = {"ActiveState": active, "Result": result}
    if start:
        u["ExecMainStartTimestamp"] = f"@{start}"
    if end:
        u["ExecMainExitTimestamp"] = f"@{end}"
    return u


class GridNodes(unittest.TestCase):
    def nodes(self, units):
        return {n["key"]: n for n in status.grid_nodes(NOW, units)}

    def test_nodes_come_from_the_settings(self):
        self.assertEqual([k for k, _, _ in status.GRID_NODES], ["backup", "lab", "vpn", "chat"])
        self.assertEqual(status.GRID_WORDS["backup"], ("the backup is running", "the backup"))

    def test_every_node_quiet_when_nothing_ran(self):
        out = status.grid_nodes(NOW, {})
        self.assertEqual([n["key"] for n in out], [k for k, _, _ in status.GRID_NODES])
        self.assertTrue(all(n["state"] == "quiet" and n["age"] is None for n in out))

    def test_which_units_light_which_node(self):
        expect = {"backup.service": "backup", "lab-check.service": "lab", "vpn-watch.service": "vpn",
                  "standup.service": "chat", "reminders.service": "chat"}
        self.assertEqual(sorted(status.GRID_UNITS), sorted(expect))
        for u, key in expect.items():
            n = self.nodes({u: unit("active", start=NOW - 70)})
            lit = [k for k, v in n.items() if v["state"] != "quiet"]
            self.assertEqual(lit, [key], u)
            self.assertEqual(n[key]["state"], "running")
            self.assertEqual(n[key]["age"], 70)

    def test_unlisted_units_never_light(self):
        for u in ("easel-power.service", "easel-server.service", "private.service"):
            self.assertNotIn(u, status.GRID_UNITS)
            self.assertTrue(all(n["state"] == "quiet" for n in status.grid_nodes(NOW, {u: unit("active", NOW)})))

    def test_after_glow(self):
        n = self.nodes({"backup.service": unit(end=NOW - 120)})
        self.assertEqual((n["backup"]["state"], n["backup"]["age"]), ("ran", 120))
        self.assertEqual(self.nodes({"backup.service": unit(end=NOW - 901)})["backup"]["state"], "quiet")
        # a poller glows only briefly, so the grid is quiet most of the time
        self.assertEqual(self.nodes({"vpn-watch.service": unit(end=NOW - 20)})["vpn"]["state"], "ran")
        self.assertEqual(self.nodes({"vpn-watch.service": unit(end=NOW - 31)})["vpn"]["state"], "quiet")
        # glow 0: lights only while it is running
        self.assertEqual(self.nodes({"reminders.service": unit(end=NOW - 1)})["chat"]["state"], "quiet")
        self.assertEqual(self.nodes({"reminders.service": unit("activating", start=NOW)})["chat"]["state"], "running")
        # a clock that went backwards is not a run
        self.assertEqual(self.nodes({"backup.service": unit(end=NOW + 60)})["backup"]["state"], "quiet")

    def test_failure_wins(self):
        n = self.nodes({"reminders.service": unit("active", start=NOW - 5),
                        "standup.service": unit("failed", end=NOW - 4000, result="exit-code")})
        self.assertEqual(n["chat"]["state"], "failed")
        self.assertIsNone(n["chat"]["age"])

    def test_running_beats_ran_and_newest_ran_wins(self):
        n = self.nodes({"standup.service": unit(end=NOW - 100), "reminders.service": unit("active", start=NOW - 9)})
        self.assertEqual((n["chat"]["state"], n["chat"]["age"]), ("running", 9))

    def test_only_short_words_and_numbers(self):
        units = {u: unit("active", start=NOW - 3) for u in status.GRID_UNITS}
        for n in status.grid_nodes(NOW, units):
            self.assertEqual(set(n), {"key", "label", "state", "age", "running", "after"})
            self.assertIn(n["state"], ("running", "ran", "failed", "quiet"))

    def test_collect_asks_systemd_once_for_every_unit(self):
        calls = []

        def fake_run(args, timeout=4):
            calls.append(args)
            if "--property=NFailedUnits" in args:
                return "0\n"
            return ("Id=backup.service\nActiveState=active\nExecMainStartTimestamp=@%d\n\n"
                    "Id=vpn-watch.service\nActiveState=failed\nResult=exit-code\n" % (NOW - 30))

        with mock.patch.object(status, "_run", fake_run), mock.patch.object(status, "in_session", lambda now: True):
            d = status.collect(NOW)
        shows = [c for c in calls if "show" in c and "--property=NFailedUnits" not in c]
        self.assertEqual(len(shows), 1)
        for u in status.GRID_UNITS + ["chat-bot.service", "inbox.service"]:
            self.assertEqual(shows[0].count(u), 1, u)
        g = {n["key"]: n["state"] for n in d["grid"]}
        self.assertEqual((g["backup"], g["vpn"], g["lab"]), ("running", "failed", "quiet"))
        self.assertTrue(d["session"])         # the line to the brain and the beam: the strip's own signal
        self.assertTrue(d["scene"])


class PageWiring(unittest.TestCase):
    def test_index_loads_the_grid(self):
        html = read("index.html")
        self.assertIn('<div id="grid"', html)
        self.assertIn('src="/static/grid.js"', html)
        self.assertIn('href="/static/grid.css"', html)
        self.assertLess(html.index("grid.js"), html.index("screen.js"))

    def test_screen_draws_the_grid(self):
        js = read("screen.js")
        self.assertIn("EaselGrid.render(s)", js)
        self.assertIn("EaselGrid.sky(s.sky)", js)

    def test_grid_has_a_spot_for_every_node_it_may_get(self):
        js = read("grid.js")
        block = re.search(r"var SLOTS = \[(.*?)\n  \];", js, re.S).group(1)
        self.assertEqual(len(re.findall(r"\{ x: \d+, y: \d+ \}", block)), 9)      # config.check: at most 9 nodes
        # the page reads only the status it is given
        self.assertIn("s.session", js)
        self.assertIn("s.grid", js)
        self.assertNotRegex(js, r"XMLHttpRequest|fetch\(|https?://(?!www\.w3\.org)")

    def test_remote_serves_no_scene_code(self):
        self.assertNotIn("/static/grid.js", remote.REMOTE_PATHS)


if __name__ == "__main__":
    unittest.main()
