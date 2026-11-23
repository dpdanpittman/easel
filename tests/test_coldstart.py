"""Offline tests: a restarted server never shows a partial task list. The last good Linear and GitHub answers are kept on
disk (diskcache.py) and served with their age until a fresh answer arrives; with nothing usable the list is held back
and says why ("blocked").

Run: python3 -m unittest discover -s tests
"""
import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _env  # noqa: E402,F401  (settings for the tests)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import derive     # noqa: E402
import diskcache  # noqa: E402
import inflight   # noqa: E402
import tasks      # noqa: E402
from test_linear import GLOBEX, ACME, GLOBEX_KEY, ACME_KEY, KEYS, Clock, FakeLinear  # noqa: E402

WALL = 1_795_200_000


class Wall:
    def __init__(self, t=WALL):
        self.t = t

    def __call__(self):
        return self.t


def linear(tmp, post, wall, keys=KEYS, clock=None):
    return tasks.Linear(get_key=lambda item: keys.get(item), post=post, clock=clock or Clock(), log=lambda line: None,
                        wall=wall, cache_file=os.path.join(tmp, tasks.LINEAR_CACHE))


NOTES = ["- 2026-11-20 08:00 SAM'S WORK TASKS (current):", "  1. OPS-572 Device check: staging build.",
           "  2. Laptop cleanup: finish it"]


def payload(tmp, lin):
    path = os.path.join(tmp, "h.md")
    with open(path, "w") as f:
        f.write("\n".join(NOTES) + "\n")
    cfg = os.path.join(tmp, "c.json")
    with open(cfg, "w") as f:
        json.dump({"notes": path}, f)

    class NoFetch:                       # the snapshot as it is now: no background fetch started by the test
        def maybe_refresh(self):
            pass

        def snapshot(self):
            return lin.snapshot()
    return tasks.Source(notes=_env.notes_of(cfg), linear=NoFetch()).current()


class DiskCache(unittest.TestCase):
    def test_atomic_mode_600_and_bad_files(self):
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "sub", "x.json")
            self.assertTrue(diskcache.save(f, {"a": 1}))
            self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)
            self.assertEqual(diskcache.load(f), {"a": 1})
            self.assertEqual([n for n in os.listdir(os.path.dirname(f)) if n.endswith(".tmp")], [])
            for bad in ("{broken", "[1]", ""):
                with open(f, "w") as fh:
                    fh.write(bad)
                self.assertIsNone(diskcache.load(f))
            self.assertIsNone(diskcache.load(os.path.join(d, "missing.json")))
            self.assertFalse(diskcache.save("/proc/nope/x.json", {"a": 1}))       # never raises


class ColdStart(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.tmp = self._t.name

    def tearDown(self):
        self._t.cleanup()

    def fill_cache(self, wall):
        lin = linear(self.tmp, FakeLinear(GLOBEX, ACME), wall)
        lin.refresh()
        self.assertEqual(lin.snapshot()["status"], "ok")
        return lin

    def test_saved_result_has_no_secret(self):
        self.fill_cache(Wall())
        with open(os.path.join(self.tmp, tasks.LINEAR_CACHE)) as f:
            raw = f.read()
        self.assertNotIn(GLOBEX_KEY, raw)
        self.assertNotIn(ACME_KEY, raw)
        self.assertIn("OPS-572", raw)

    def test_cold_start_with_a_saved_result_serves_it_with_its_age(self):
        self.fill_cache(Wall(WALL))
        wall = Wall(WALL + 120)                          # restarted two minutes later; nothing has answered yet
        lin = linear(self.tmp, FakeLinear(urllib.error.URLError("x"), urllib.error.URLError("x")), wall)
        snap = lin.snapshot()
        self.assertEqual((snap["status"], snap["at"], snap.get("missing")), ("stale", WALL, None))
        out = payload(self.tmp, lin)
        ids = [it["ids"] for it in out["items"]]
        self.assertEqual(len(ids), 7)                    # 2 notes + 5 Linear-only: the whole list, not the 2
        self.assertNotIn("blocked", out)
        self.assertEqual((out["linear"], out["linear_at"]), ("stale", WALL))

    def test_cold_start_with_nothing_saved_says_loading(self):
        lin = linear(self.tmp, FakeLinear(GLOBEX, ACME), Wall())
        out = payload(self.tmp, lin)
        self.assertEqual((out["show"], out["items"], out["total"]), (True, [], 0))
        self.assertEqual(out["blocked"], {"why": "loading", "since": None, "workspaces": ["acme", "globex"]})
        lin.refresh()                                    # the first fetch answers: the full list, nothing blocked
        out = payload(self.tmp, lin)
        self.assertNotIn("blocked", out)
        self.assertEqual(out["total"], 7)

    def test_linear_failing_with_nothing_saved_says_since_when(self):
        wall = Wall()
        lin = linear(self.tmp, FakeLinear(urllib.error.URLError("x"), ACME), wall)
        lin.refresh()
        wall.t += 600
        lin.refresh()
        out = payload(self.tmp, lin)
        self.assertEqual(out["items"], [])                # never the Acme-only (shorter) list
        self.assertEqual(out["blocked"], {"why": "down", "since": WALL, "workspaces": ["globex"]})

    def test_linear_failing_after_a_good_fetch_keeps_it_with_its_age(self):
        wall, clock = Wall(), Clock()
        fake = FakeLinear(GLOBEX, ACME)
        lin = linear(self.tmp, fake, wall, clock=clock)
        lin.refresh()
        fake.data[GLOBEX_KEY] = urllib.error.URLError("down")
        wall.t += 3 * 3600
        clock.t += 3 * 3600
        lin.refresh()
        out = payload(self.tmp, lin)
        self.assertEqual((out["total"], out["linear"], out["linear_at"]), (7, "stale", WALL))

    def test_stale_saved_result_shows_its_age_and_expires(self):
        self.fill_cache(Wall(WALL))
        lin = linear(self.tmp, FakeLinear(GLOBEX, ACME), Wall(WALL + 5 * 3600))
        self.assertEqual((lin.snapshot()["status"], lin.snapshot()["at"]), ("stale", WALL))
        old = linear(self.tmp, FakeLinear(GLOBEX, ACME), Wall(WALL + tasks.GOOD_FOR_S + 1))
        self.assertEqual(old.snapshot()["missing"], {"globex": ("loading", None), "acme": ("loading", None)})

    def test_malformed_saved_entries_are_dropped(self):
        diskcache.save(os.path.join(self.tmp, tasks.LINEAR_CACHE),
                       {"v": 1, "ws": {"globex": {"wall": WALL, "teams": ["OPS"],
                                                 "issues": [{"identifier": "bad id", "title": "x", "state": "s",
                                                             "priority": 1},
                                                            {"identifier": "OPS-1", "title": "ok", "state": "Todo",
                                                             "priority": 2}]},
                                       "evil": {"wall": WALL, "teams": [], "issues": []}}})
        lin = linear(self.tmp, FakeLinear(GLOBEX, ACME), Wall(WALL + 60))
        snap = lin.snapshot()
        self.assertEqual(set(snap["ws"]), {"globex"})
        self.assertEqual([i["identifier"] for i in snap["ws"]["globex"]["issues"]], ["OPS-1"])
        self.assertEqual(snap["missing"], {"acme": ("loading", None)})


class GitHubColdStart(unittest.TestCase):
    def test_both_github_caches_survive_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            doc = {"data": {"o0": {"nodes": [{"number": 7, "title": "t", "isDraft": False, "reviewDecision": None,
                                              "repository": {"name": "r", "nameWithOwner": "globex-labs/r"},
                                              "commits": {"nodes": []}}]}, "o1": {"nodes": []}}}
            f = os.path.join(d, "gh.json")
            g = inflight.GitHub(run=lambda a, t: (0, json.dumps(doc)), wall=lambda: WALL, log=lambda line: None,
                                cache_file=f)
            g.refresh()
            g2 = inflight.GitHub(run=lambda a, t: (1, ""), wall=lambda: WALL + 60, log=lambda line: None,
                                 cache_file=f)
            snap = g2.snapshot()
            self.assertEqual((snap["status"], snap["at"], [p["number"] for p in snap["prs"]]), ("stale", WALL, [7]))
            self.assertEqual(stat.S_IMODE(os.stat(f).st_mode), 0o600)

            tdoc = {"data": {"t0": {"nodes": [{"number": 30, "state": "OPEN", "isDraft": False, "mergedAt": None,
                                               "headRefName": "work/sam/ops-577-x", "body": "Refs OPS-577",
                                               "repository": {"name": "r", "nameWithOwner": "globex-labs/r"},
                                               "commits": {"nodes": []}}]}, "t1": {"nodes": []}}}
            f2 = os.path.join(d, "t.json")
            t = derive.TicketPRs(run=lambda a, tm: (0, json.dumps(tdoc)), wall=lambda: WALL, log=lambda line: None,
                                 cache_file=f2)
            t.refresh()
            with open(f2) as fh:
                self.assertNotIn("Refs", fh.read())            # PR bodies are never kept, only the ids found
            t2 = derive.TicketPRs(run=lambda a, tm: (1, ""), wall=lambda: WALL + 60, log=lambda line: None,
                                  cache_file=f2)
            self.assertEqual((t2.snapshot()["status"], t2.snapshot()["prs"][0]["ids"]), ("stale", ["OPS-577"]))


class PageBlocked(unittest.TestCase):
    def test_page_renders_blocked_plainly(self):
        with open(os.path.join(os.path.dirname(HERE), "web", "screen.js")) as f:
            js = f.read()
        self.assertIn('"Loading the list from Linear…"', js)
        self.assertIn('"Linear has not answered"', js)


if __name__ == "__main__":
    unittest.main()
