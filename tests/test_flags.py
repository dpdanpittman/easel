"""Offline tests for the task list's waiting/running flags joined from the In flight block (tasks.apply_flight,
), the real
counts in /api/inflight and the shape of /api/tasks.

Run: python3 -m unittest discover -s tests
"""
import json
import os
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
NOW = datetime(2026, 11, 20, 9, 40, tzinfo=TZ)

TASKS = """- 2026-11-19 11:54 SAM'S WORK TASKS (current):
  1. [P1] OPS-577 CI token for CI: apply the stack. WAITING ON: Sam (staging stack apply, CI tokens).
  2. [P1] ACME-4810 rehearsal: branch ready. IN PROGRESS: Agent.
  3. [P2] OPS-57 old ticket: something else
  4. [P2] ACME-4892/4893 exports: review. WAITING ON: Priya (his #24).
  5. [P3] OPS-600 docs: write them. WAITING ON: Sam (names).
  6. [P3] Laptop cleanup: finish it
"""


def flight(at="2026-11-20 09:35", lines=()):
    return ["- %s IN FLIGHT (current):" % at] + ["  " + x for x in lines]


def payload(flight_lines, now=NOW):
    """The task payload (notes only) joined with an In flight block parsed at `now`."""
    data = tasks.build(TASKS.split("\n"))
    block = inflight.parse(flight_lines, now) if flight_lines is not None else None
    return tasks.apply_flight(data, block)


def by_id(data):
    return {it["ids"] or it["title"]: it for it in data["items"]}


class TicketIds(unittest.TestCase):
    def test_whole_token_and_case(self):
        self.assertEqual(tasks.ticket_ids("OPS-577: apply the stack"), {"OPS-577"})
        self.assertEqual(tasks.ticket_ids("acme-4810: rehearsal"), {"ACME-4810"})
        self.assertEqual(tasks.ticket_ids("see (Ops-577) and ACME-4892/4893"), {"OPS-577", "ACME-4892", "ACME-4893"})

    def test_near_misses(self):
        self.assertEqual(tasks.ticket_ids("OPS-5770 is another"), {"OPS-5770"})
        self.assertNotIn("OPS-57", tasks.ticket_ids("OPS-577: x"))
        self.assertNotIn("OPS-577", tasks.ticket_ids("OPS-57: x"))
        self.assertEqual(tasks.ticket_ids("XOPS-577 and OPS-577x and foo_OPS-577"), {"XOPS-577"})
        self.assertEqual(tasks.ticket_ids("no ticket here, 12:30 and #24"), set())

    def test_is_owner(self):
        for who in ("Sam", "Sam (staging stack apply)", "sam's call", "you", "me"):
            self.assertTrue(tasks.is_owner(who), who)
        for who in ("Priya", "Marco (credential upload)", "Priya's #24, then Sam", "Samantha"):
            self.assertFalse(tasks.is_owner(who), who)


class Join(unittest.TestCase):
    def test_w_line_names_the_ticket(self):
        d = payload(flight(lines=["W. OPS-600: pick the vault item names",
                                  "W. Essence site: pick a design direction"]))
        it = by_id(d)
        self.assertEqual(d["flags"], "inflight")
        self.assertEqual((it["OPS-600"]["waiting"], it["OPS-600"]["waiting_text"]),
                         ("Sam", "pick the vault item names"))
        # a hand "WAITING ON: Sam" with no current W line is stale: not shown
        self.assertEqual((it["OPS-577"]["waiting"], it["OPS-577"]["waiting_text"]), ("", ""))
        # the W line about no listed ticket stays in the panel only
        self.assertFalse(any("Essence" in json.dumps(x) for x in d["items"]))
        self.assertEqual(d["waiting"], 2)            # OPS-600 (W line) + ACME-4892/4893 (waiting on Priya)

    def test_near_miss_does_not_flag(self):
        it = by_id(payload(flight(lines=["W. OPS-577: token"])))
        self.assertEqual(it["OPS-577"]["waiting"], "Sam")
        self.assertEqual(it["OPS-57"]["waiting"], "")
        it = by_id(payload(flight(lines=["W. OPS-57: old one"])))
        self.assertEqual(it["OPS-57"]["waiting"], "Sam")
        self.assertEqual(it["OPS-577"]["waiting"], "")

    def test_case_insensitive_and_anywhere_in_the_line(self):
        it = by_id(payload(flight(lines=["W. merge the PR for ops-577 today"])))
        self.assertEqual((it["OPS-577"]["waiting"], it["OPS-577"]["waiting_text"]),
                         ("Sam", "merge the PR for ops-577 today"))

    def test_several_w_lines_for_one_ticket(self):
        it = by_id(payload(flight(lines=["W. OPS-577: create the token", "W. OPS-577: add the repo secret"])))
        self.assertEqual(it["OPS-577"]["waiting_text"], "create the token · +1 more")

    def test_shorthand_ids_on_the_task_match(self):
        it = by_id(payload(flight(lines=["W. ACME-4893: approve the export script"])))
        self.assertEqual((it["ACME-4892/4893"]["waiting"], it["ACME-4892/4893"]["waiting_text"]),
                         ("Sam", "approve the export script"))   # the W line beats the hand "Priya"

    def test_waiting_on_someone_else_stays(self):
        it = by_id(payload(flight(lines=["W. Essence site: pick a direction"])))
        self.assertEqual((it["ACME-4892/4893"]["waiting"], it["ACME-4892/4893"]["waiting_text"]),
                         ("Priya (his #24)", ""))

    def test_r_line_marks_running(self):
        d = payload(flight(lines=["R. 09:30 ACME-4810: rehearsal scripts build", "R. 09:31 easel scroll"]))
        it = by_id(d)
        self.assertEqual((it["ACME-4810"]["active"], it["ACME-4810"]["running"]), ("rehearsal scripts build", True))
        self.assertEqual(d["active"], 1)
        # hand IN PROGRESS with no R line is not shown while the block is current
        d = payload(flight(lines=["R. 09:31 easel scroll"]))
        self.assertEqual((by_id(d)["ACME-4810"]["active"], by_id(d)["ACME-4810"]["running"]), ("", False))
        self.assertEqual(d["active"], 0)

    def test_finished_lines_flag_nothing(self):
        it = by_id(payload(flight(lines=["F. 09:20 OPS-577: token made"])))
        self.assertEqual((it["OPS-577"]["waiting"], it["OPS-577"]["active"]), ("", ""))

    def test_stale_block_falls_back_to_hand_text(self):
        hand = tasks.build(TASKS.split("\n"))
        for lines in (flight(at="2026-11-19 09:00", lines=["W. OPS-600: names"]), [], None):
            d = payload(lines)
            self.assertEqual(d["flags"], "hand")
            for a, b in zip(d["items"], hand["items"]):
                self.assertEqual((a["waiting"], a["active"]), (b["waiting"], b["active"]))
                self.assertEqual((a["waiting_text"], a["running"]), ("", False))
            self.assertEqual((d["waiting"], d["active"]), (hand["waiting"], hand["active"]))
        it = by_id(payload(None))
        self.assertTrue(it["OPS-577"]["waiting"].startswith("Sam"))
        self.assertEqual(it["ACME-4810"]["active"], "Agent")

    def test_w_lines_past_the_cap_still_flag(self):
        lines = ["W. thing %d" % i for i in range(inflight.ITEMS_MAX)] + ["W. OPS-600: late line"]
        it = by_id(payload(flight(lines=lines)))
        self.assertEqual(it["OPS-600"]["waiting"], "Sam")

    def test_linear_items_get_flags_too(self):
        snap = {"ws": {"globex": {"issues": [{"identifier": "OPS-700", "title": "New issue", "priority": 3,
                                             "state": "Todo", "backlog": False}], "teams": {"OPS"}}},
                "status": "ok"}
        data = tasks.build(TASKS.split("\n"), snap)
        d = tasks.apply_flight(data, inflight.parse(flight(lines=["W. OPS-700 review the plan"]), NOW))
        self.assertEqual((by_id(d)["OPS-700"]["waiting"], by_id(d)["OPS-700"]["waiting_text"]),
                         ("Sam", "review the plan"))

    def test_odd_input_never_raises(self):
        self.assertEqual(tasks.apply_flight({}, {"state": "ok"})["items"], [])
        d = tasks.apply_flight(dict(tasks.EMPTY), None)
        self.assertEqual((d["waiting"], d["active"], d["flags"]), (0, 0, "hand"))


class SourceWiring(unittest.TestCase):
    def write(self, d, text):
        cfg = os.path.join(d, "config.json")
        with open(cfg, "w") as f:
            json.dump({"notes": os.path.join(d, "h.md")}, f)
        with open(os.path.join(d, "h.md"), "w") as f:
            f.write(text)
        return cfg

    def test_source_joins_and_survives_a_broken_flight(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self.write(d, TASKS + "\n".join(flight(lines=["W. OPS-600: names"])) + "\n")
            fl = inflight.Source(notes=_env.notes_of(cfg), now=lambda: NOW)
            d1 = tasks.Source(notes=_env.notes_of(cfg), flight=fl.block).current()
            self.assertEqual(by_id(d1)["OPS-600"]["waiting_text"], "names")

            def boom():
                raise RuntimeError("x")
            d2 = tasks.Source(notes=_env.notes_of(cfg), flight=boom).current()
            self.assertEqual(d2["flags"], "hand")
            self.assertTrue(by_id(d2)["OPS-577"]["waiting"].startswith("Sam"))
            # without a flight source the payload is exactly as before
            self.assertNotIn("flags", tasks.Source(notes=_env.notes_of(cfg)).current())

    def test_api_tasks_shape(self):
        import server
        from http.server import ThreadingHTTPServer
        with tempfile.TemporaryDirectory() as d:
            cfg = self.write(d, TASKS + "\n".join(flight(lines=["W. OPS-600: names", "R. 09:30 ACME-4810 build"]))
                             + "\n")
            fl = inflight.Source(notes=_env.notes_of(cfg), now=lambda: NOW)
            old = server._tasks_src
            server._tasks_src = tasks.Source(notes=_env.notes_of(cfg), flight=fl.block)
            server._tasks_cache["at"] = 0.0
            hours = unittest.mock.patch.object(tasks, "in_hours", return_value=True)
            hours.start()
            srv = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
            srv.kind, srv.names = "local", ()
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/api/tasks" % srv.server_address[1],
                                            timeout=5) as r:
                    data = json.loads(r.read())
            finally:
                srv.shutdown()
                srv.server_close()
                hours.stop()
                server._tasks_src = old
                server._tasks_cache["at"] = 0.0
        self.assertTrue({"show", "items", "total", "done", "waiting", "active", "flags"} <= set(data))
        self.assertEqual((data["total"], data["waiting"], data["active"], data["flags"]), (6, 2, 1, "inflight"))
        for it in data["items"]:
            self.assertTrue({"n", "ids", "title", "state", "waiting", "waiting_text", "active", "running"} <= set(it))
        self.assertEqual(by_id(data)["ACME-4810"]["running"], True)


class InflightCounts(unittest.TestCase):
    def test_counts_are_the_real_numbers(self):
        lines = flight(lines=["W. w%d" % i for i in range(15)] + ["R. 09:%02d r%d" % (i, i) for i in range(3)]
                       + ["F. 09:%02d f%d" % (i, i) for i in range(9)])
        block = inflight.parse(lines, NOW)
        self.assertEqual(len(block["waiting"]), inflight.ITEMS_MAX)
        self.assertEqual(len(block["finished"]), inflight.FINISHED_MAX)
        g = inflight.merge(block, {"prs": []})
        self.assertEqual(g["counts"], {"waiting": 15, "running": 3, "finished": 9, "prs": 0})

    def test_counts_include_green_prs(self):
        block = inflight.parse(flight(lines=["W. one", "W. two", "W. three"]), NOW)
        prs = [{"repo": "x", "owner_repo": "globex-labs/x", "number": 1, "title": "t", "ci": "passing", "review": ""},
               {"repo": "x", "owner_repo": "globex-labs/x", "number": 2, "title": "t", "ci": "failing", "review": ""}]
        g = inflight.merge(block, {"prs": prs})
        self.assertEqual(g["counts"], {"waiting": 4, "running": 0, "finished": 0, "prs": 1})

    def test_empty_and_hidden_carry_counts(self):
        self.assertEqual(inflight.EMPTY["counts"], {"waiting": 0, "running": 0, "finished": 0, "prs": 0})
        self.assertIn("counts", inflight.HIDDEN)


class PageRules(unittest.TestCase):
    """The page side (the real behaviour is checked in a browser; these pin the rules in the code)."""

    def setUp(self):
        web = os.path.join(os.path.dirname(HERE), "web")
        with open(os.path.join(web, "screen.js")) as f:
            self.js = f.read()
        with open(os.path.join(web, "index.html")) as f:
            self.html = f.read()

    def test_no_row_is_hidden_any_more(self):
        self.assertNotIn("of \" + lis.length", self.js)
        self.assertNotIn('style.display = "none"', self.js.split("/* ---------- Chess")[0].split("In flight:")[1])

    def test_scrollers_on_every_group_and_the_task_list(self):
        for gid in ("g-wait", "g-run", "g-done"):
            self.assertRegex(self.html, r'id="%s"[^>]*>\s*<div class="fglab">[^<]*<span class="fgn"></span></div>\s*'
                             r'<div class="fview"><ul class="flist"></ul></div>' % gid)
        self.assertIn('<div id="taskview" class="tview"><ol id="tasklist" class="tasklist"></ol></div>', self.html)
        self.assertIn("prefers-reduced-motion: reduce", self.js)
        self.assertIn('sub = "updated " + clockOf(d.block_at);', self.js)
        self.assertIn('requestAnimationFrame(measureScrollers);', self.js)


if __name__ == "__main__":
    unittest.main()
