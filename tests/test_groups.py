"""Offline tests for grouped rows on the work task list (tasks.group_item; ). A notes line whose title names several tickets
became one row whose hand note replaced every ticket's live state (ACME-4896 sat as "parked" while Linear had it In
Review). Now each open ticket is a step of the row with its own live title and state; the hand note is the label.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import unittest
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _env  # noqa: E402,F401  (settings for the tests)
sys.path.insert(0, os.path.dirname(HERE))
import inflight  # noqa: E402
import tasks     # noqa: E402

TZ = tasks.TZ
NOW = int(datetime(2026, 11, 23, 9, 40, tzinfo=TZ).timestamp())
CI_LINE = "  1. [P1] Acme CI speed ACME-4892/4893/4894/4896: parked for now (Sam, 11-18)."


def block(*items, at="2026-11-20 11:52"):
    return ["- %s SAM'S WORK TASKS (current):" % at] + list(items)


def issue(ident, state="Todo", title="a ticket"):
    return {"identifier": ident, "title": title, "priority": 2, "state": state, "backlog": False}


def snap(acme=(), globex=None):
    ws = {"acme": {"issues": list(acme), "teams": {"ACME"}}}
    if globex is not None:
        ws["globex"] = {"issues": list(globex), "teams": {"OPS", "WEB"}}
    return {"ws": ws, "status": "ok", "at": NOW}


CI_OPEN = [issue("ACME-4892", "In Progress", "Cache the pnpm store in CI"),
           issue("ACME-4893", "Todo", "Split the test job by package"),
           issue("ACME-4894", "In Review", "Remote build cache")]          # ACME-4896 is Done: not in the open list


def build(lines, linear, facts=None):
    return tasks.build(lines, linear, facts)


class Detect(unittest.TestCase):
    def test_shorthand_drops_the_prefix(self):
        self.assertEqual(tasks.ordered_ids("Acme CI speed ACME-4892/4893/4894/4896"),
                         ["ACME-4892", "ACME-4893", "ACME-4894", "ACME-4896"])

    def test_comma_and_ampersand_lists(self):
        self.assertEqual(tasks.ordered_ids("ACME-1, ACME-2 exports"), ["ACME-1", "ACME-2"])
        self.assertEqual(tasks.ordered_ids("ACME-1 & 2, 3"), ["ACME-1", "ACME-2", "ACME-3"])
        self.assertEqual(tasks.ordered_ids("ACME-1/OPS-2 and ACME-1 again"), ["ACME-1", "OPS-2"])
        self.assertEqual(tasks.ordered_ids("no ticket, 12:30"), [])

    def test_render_keeps_title_ids_and_label(self):
        it = tasks.render(CI_LINE)
        self.assertEqual(it["tids"], ["ACME-4892", "ACME-4893", "ACME-4894", "ACME-4896"])
        self.assertEqual((it["label"], it["state"]), ("Acme CI speed", "parked for now (Sam, 11-18)"))
        it = tasks.render("  2. ACME-1, ACME-2 exports: review")
        self.assertEqual((it["tids"], it["label"], it["ids"]), (["ACME-1", "ACME-2"], "exports", "ACME-1, ACME-2"))

    def test_ids_in_the_note_do_not_group(self):
        self.assertEqual(tasks.render("  3. OPS-577 apply: waits on OPS-580 and OPS-581")["tids"], ["OPS-577"])

    def test_strip_and_shorthand(self):
        self.assertEqual(tasks.strip_ids("ACME-1, ACME-2 & ACME-3 exports"), "exports")
        self.assertEqual(tasks.shorthand(["ACME-4892", "ACME-4893", "OPS-1", "OPS-2"]), "ACME-4892/4893/OPS-1/2")


class Group(unittest.TestCase):
    def test_the_ci_line_cycles_its_open_tickets(self):
        out = build(block(CI_LINE), snap(CI_OPEN))
        self.assertEqual(len(out["items"]), 1)
        it = out["items"][0]
        self.assertEqual((it["ids"], it["title"], it["state"], it["prio"], it["n"]),
                         ("ACME-4892/4893/4894", "Acme CI speed", "parked for now (Sam, 11-18)", 1, 1))
        g = it["group"]
        self.assertEqual((g["title"], g["note"]), ("Acme CI speed", "parked for now (Sam, 11-18)"))
        self.assertEqual([(t["n"], t["id"], t["title"], t["state"]) for t in g["tickets"]],
                         [(1, "ACME-4892", "Cache the pnpm store in CI", "In Progress"),
                          (2, "ACME-4893", "Split the test job by package", "Todo"),
                          (3, "ACME-4894", "Remote build cache", "In Review")])
        # the count: each open ticket counts; the row is one row
        self.assertEqual((out["total"], out["rows"]), (3, 1))

    def test_states_are_derived_per_ticket_and_the_note_never_replaces_them(self):
        prs = [{"repo": "acme", "number": 41, "state": "merged", "draft": False, "ci": "passing",
                "merged_at": NOW - 3600, "ids": ["ACME-4894"]}]
        out = build(block(CI_LINE, at="2026-11-18 09:00"), snap(CI_OPEN), {"prs": prs, "finished": [], "now": NOW})
        it = out["items"][0]
        self.assertEqual(it["state_src"], "group")
        states = {t["id"]: (t["state"], t["state_src"]) for t in it["group"]["tickets"]}
        self.assertEqual(states["ACME-4894"], ("In Review · #41 merged 08:40", "derived"))
        self.assertEqual(states["ACME-4892"], ("In Progress", "derived"))
        self.assertNotIn("parked", json.dumps(it["group"]["tickets"]))

    def test_comma_form(self):
        out = build(block("  2. [P2] ACME-1, ACME-2 exports: review"), snap([issue("ACME-1"), issue("ACME-2")]))
        it = out["items"][0]
        self.assertEqual((it["ids"], it["title"]), ("ACME-1/2", "exports"))
        self.assertEqual([t["id"] for t in it["group"]["tickets"]], ["ACME-1", "ACME-2"])

    def test_closed_tickets_drop_out_of_the_cycle_and_never_show_alone(self):
        out = build(block(CI_LINE), snap(CI_OPEN[:2] + [issue("ACME-9", title="other")]))
        ids = [it["ids"] for it in out["items"]]
        self.assertEqual(ids, ["ACME-4892/4893", "ACME-9"])
        self.assertEqual((out["total"], out["rows"]), (3, 2))
        self.assertEqual([it["n"] for it in out["items"]], [1, 3])

    def test_one_open_ticket_left_is_a_normal_row(self):
        out = build(block(CI_LINE), snap([CI_OPEN[1]]))
        it = out["items"][0]
        self.assertEqual((it["ids"], it["title"], it["source"], it["n"]), ("ACME-4893", "Acme CI speed", "notes", 1))
        self.assertNotIn("group", it)
        self.assertEqual((out["total"], out["rows"]), (1, 1))
        # with facts it gets its live state first, like any ticket row
        out = build(block(CI_LINE, at="2026-11-18 09:00"), snap([CI_OPEN[1]]), {"prs": [], "finished": [], "now": NOW})
        self.assertEqual(out["items"][0]["state"], "Todo")

    def test_one_left_without_words_takes_the_linear_title(self):
        out = build(block("  1. ACME-1/2: review"), snap([issue("ACME-2", title="Live title")]))
        self.assertEqual((out["items"][0]["ids"], out["items"][0]["title"]), ("ACME-2", "Live title"))

    def test_none_open_the_row_goes(self):
        out = build(block(CI_LINE, "  2. OPS-1 other: x"), snap([], globex=[issue("OPS-1")]))
        self.assertEqual([it["ids"] for it in out["items"]], ["OPS-1"])
        self.assertEqual(out["total"], 1)

    def test_workspace_not_answering_keeps_the_line_as_written(self):
        # Acme has not answered: nothing is known about the CI tickets, the line shows exactly as before
        out = build(block(CI_LINE), {"ws": {"globex": {"issues": [issue("OPS-1")], "teams": {"OPS"}}}, "status": "stale"})
        it = [x for x in out["items"] if "Acme" in x["title"]][0]
        self.assertEqual((it["ids"], it["title"]), ("", "Acme CI speed ACME-4892/4893/4894/4896"))
        self.assertNotIn("group", it)

    def test_a_prefix_no_team_has_is_not_a_ticket(self):
        out = build(block("  1. WIDGET-31 bump for OPS-5: x"), snap([], globex=[issue("OPS-5")]))
        self.assertEqual([it["ids"] for it in out["items"]], ["WIDGET-31"])   # not a group: the usual rules
        self.assertNotIn("group", out["items"][0])

    def test_notes_only_is_unchanged(self):
        out = tasks.build(block(CI_LINE))
        self.assertEqual([(it["ids"], it["title"]) for it in out["items"]],
                         [("", "Acme CI speed ACME-4892/4893/4894/4896")])
        self.assertNotIn("group", out["items"][0])


class Shape(unittest.TestCase):
    def test_json_is_backward_compatible(self):
        out = build(block(CI_LINE, "  2. OPS-1 other: x"), snap(CI_OPEN, globex=[issue("OPS-1")]),
                    {"prs": [], "finished": [], "now": NOW})
        json.dumps(out)
        for key in ("show", "items", "total", "rows", "done", "waiting", "active"):
            self.assertIn(key, out)
        old_keys = {"n", "ids", "title", "state", "done", "waiting", "active", "prio", "source", "state_src"}
        for it in out["items"]:
            self.assertTrue(old_keys <= set(it), it)
            for internal in ("full", "_order", "_lin", "_group", "tids", "label"):
                self.assertNotIn(internal, it)
        g = out["items"][0]["group"]
        self.assertEqual(set(g), {"title", "note", "tickets"})
        self.assertEqual(set(g["tickets"][0]), {"n", "id", "title", "state", "state_src"})
        self.assertNotIn("group", out["items"][1])

    def test_blocked_payload_has_rows(self):
        out = tasks.blocked_if_missing({"show": True, "items": [1], "total": 3, "rows": 1},
                                       {"missing": {"acme": ("loading", None)}})
        self.assertEqual((out["total"], out["rows"], out["items"]), (0, 0, []))

    def test_flags_per_step(self):
        data = build(block(CI_LINE), snap(CI_OPEN))
        now = datetime(2026, 11, 23, 9, 40, tzinfo=TZ)
        fl = inflight.parse(["- 2026-11-23 09:35 IN FLIGHT (current):", "  W. ACME-4893: approve the split",
                             "  R. 09:30 ACME-4894: build cache trial"], now)
        out = tasks.apply_flight(data, fl)
        it = out["items"][0]
        steps = {t["id"]: (t["waiting_text"], t["running_text"]) for t in it["group"]["tickets"]}
        self.assertEqual(steps, {"ACME-4892": ("", ""), "ACME-4893": ("approve the split", ""),
                                 "ACME-4894": ("", "build cache trial")})
        self.assertEqual(it["waiting"], "Sam")          # the row as a whole still says it waits on Sam
        self.assertEqual(out["waiting"], 1)


class PageRules(unittest.TestCase):
    """The page side, pinned in the code (checked by hand in headless Chromium with fake APIs: the
    grouped row kept one height through every step, with and without reduced motion)."""

    def setUp(self):
        web = os.path.join(os.path.dirname(HERE), "web")
        with open(os.path.join(web, "screen.js")) as f:
            self.js = f.read()
        with open(os.path.join(web, "screen.css")) as f:
            self.css = f.read()

    def test_rotation_and_reduced_motion(self):
        self.assertIn("var GROUP_STEP_MS = 5000;", self.js)
        self.assertIn("setInterval(stepGroups, GROUP_STEP_MS);", self.js)
        self.assertIn("it.group.tickets.length > 1", self.js)
        css = self.css.replace("\n", " ")
        self.assertRegex(css, r"@media \(prefers-reduced-motion: reduce\) \{\s*\.task \.tgstep,\s*\.task \.tgstep\.on \{\s*transition: none;")

    def test_steps_share_one_cell_so_the_height_never_changes(self):
        self.assertRegex(self.css, r"\.task \.tgstep \{[^}]*grid-area: 1 / 1;[^}]*visibility: hidden;")


if __name__ == "__main__":
    unittest.main()
