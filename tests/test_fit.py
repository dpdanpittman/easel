"""Offline tests for the kiosk's sizing rules (web/fit.js, run with node; skipped when node is missing).

THE THRESHOLD:
a list moves only when at least one whole row cannot be shown; less than a row is absorbed, boxes end on rows.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import shutil
import subprocess
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
FIT = os.path.join(os.path.dirname(HERE), "web", "fit.js")
NODE = shutil.which("node")

ROW = 43                                   # a waiting row: 43 px with a 6 px gap
BOTTOMS = [43, 92, 141, 190, 239, 288]


def js(expr):
    out = subprocess.run([NODE, "-e", "const F=require(%s); process.stdout.write(JSON.stringify(%s))"
                          % (json.dumps(FIT), expr)], capture_output=True, text=True, timeout=20)
    if out.returncode:
        raise AssertionError(out.stderr)
    return json.loads(out.stdout)


@unittest.skipUnless(NODE, "node not installed")
class Decide(unittest.TestCase):
    def test_threshold(self):
        self.assertEqual(js("F.decide(0, 43)"), "fits")
        self.assertEqual(js("F.decide(1, 43)"), "fits")           # rounding
        self.assertEqual(js("F.decide(3, 43)"), "absorb")         # a few pixels never move
        self.assertEqual(js("F.decide(21, 43)"), "absorb")        # half a row never moves
        self.assertEqual(js("F.decide(42.5, 43)"), "scroll")      # one row (within rounding)
        self.assertEqual(js("F.decide(49, 43)"), "scroll")        # one row with its gap
        self.assertEqual(js("F.decide(400, 43)"), "scroll")       # many rows

    def test_fit_box_ends_on_a_row(self):
        # whole list fits
        self.assertEqual(js("F.fitBox(%s, 300)" % BOTTOMS), {"mode": "fits", "height": 288, "rows": 6})
        # 3 px short: the box gets the pixels, no motion
        self.assertEqual(js("F.fitBox(%s, 285)" % BOTTOMS), {"mode": "absorb", "height": 288, "rows": 6})
        # half a row short: absorbed too
        self.assertEqual(js("F.fitBox(%s, 265)" % BOTTOMS)["mode"], "absorb")
        # a whole row short: shows 5 whole rows and moves
        self.assertEqual(js("F.fitBox(%s, 239)" % BOTTOMS), {"mode": "scroll", "height": 239, "rows": 5})
        # many rows short, an odd box: snapped down to whole rows (never a half-cut row)
        self.assertEqual(js("F.fitBox(%s, 120)" % BOTTOMS), {"mode": "scroll", "height": 92, "rows": 2})

    def test_rows_that_fit_and_stops(self):
        self.assertEqual(js("F.rowsThatFit(%s, 141)" % BOTTOMS), 3)
        self.assertEqual(js("F.rowsThatFit(%s, 140.5)" % BOTTOMS), 3)
        self.assertEqual(js("F.rowsThatFit(%s, 139)" % BOTTOMS), 2)
        # one row at a time, then the bottom
        self.assertEqual(js("F.rowStops([0,49,98,147,196,245], 147)"), [0, 49, 98, 147])
        self.assertEqual(js("F.rowStops([0,49], 0)"), [0])


TODAY = dict(t=[59, 128, 197, 266, 335, 404], w=[43, 92, 141], r=[27, 57], d=[27, 57, 87, 117, 147, 177],
             labW=24, labR=24, labD=24, colGap=6, tGapSlack=5)


@unittest.skipUnless(NODE, "node not installed")
class Allocate(unittest.TestCase):
    """the task list takes the room it needs, In flight gets smaller and scrolls."""
    def alloc(self, **kw):
        return js("F.allocate(%s)" % json.dumps(dict(TODAY, **kw)))

    def test_everything_whole_when_there_is_room(self):
        k = self.alloc(budget=700)
        self.assertEqual((k["t"], k["w"], k["r"], k["d"]), (6, 3, 2, 6))
        self.assertTrue(k["fits"])
        self.assertEqual(k["gapCut"], 0)
        self.assertEqual(k["flightMin"], 165)      # 3 waiting rows under their label

    def test_tasks_whole_in_flight_smaller_and_moving(self):
        # 404 for the tasks + 165 for In flight: the task list keeps all 6 rows, just finished shrinks to 1 and moves
        k = self.alloc(budget=569)
        self.assertEqual((k["t"], k["w"], k["r"], k["d"]), (6, 3, 2, 1))
        self.assertFalse(k["fits"])
        self.assertEqual(k["tHeight"], 404)
        # many waiting lines: waiting shows what fits and moves; the tasks are untouched
        k = self.alloc(budget=569, w=BOTTOMS)
        self.assertEqual((k["t"], k["w"]), (6, 3))

    def test_a_few_pixels_short_tightens_task_gaps_not_rows(self):
        k = self.alloc(budget=565)                 # 4 px short of the minimum
        self.assertEqual((k["t"], k["w"], k["gapCut"]), (6, 3, 1))
        self.assertEqual(k["tHeight"], 399)

    def test_last_resort_the_task_list_moves(self):
        # In flight keeps its minimum (3 waiting rows); the task list shows the whole rows that fit and moves
        k = self.alloc(budget=500)
        self.assertEqual((k["t"], k["w"]), (5, 3))
        self.assertEqual(k["tHeight"], 335)
        self.assertGreaterEqual(500 - k["tHeight"], k["flightMin"])

    def test_running_leaves_a_row_for_just_finished(self):
        k = self.alloc(budget=404 + 110, r=[27, 57, 87, 117, 147])
        self.assertGreaterEqual(k["d"], 1)
        self.assertGreaterEqual(k["r"], 1)

    def test_few_waiting_lines_lower_the_minimum(self):
        # one waiting line: the minimum is the right column's (one running and one just finished row), 108 px
        k = self.alloc(budget=404 + 108, w=[43])
        self.assertEqual((k["t"], k["flightMin"]), (6, 108))


@unittest.skipUnless(NODE, "node not installed")
class Wiring(unittest.TestCase):
    def test_page_loads_fit_before_screen(self):
        with open(os.path.join(os.path.dirname(HERE), "web", "index.html")) as f:
            html = f.read()
        self.assertLess(html.index('src="/static/fit.js"'), html.index('src="/static/screen.js"'))
        with open(os.path.join(os.path.dirname(HERE), "web", "screen.js")) as f:
            src = f.read()
        self.assertIn("var Fit = window.KioskFit;", src)
        self.assertIn('Fit.decide(max, rowPx) !== "scroll"', src)


if __name__ == "__main__":
    unittest.main()
