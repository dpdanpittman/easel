"""Offline tests for the work task list (tasks.py) and the night power schedule (power.py).

Run: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _env  # noqa: E402,F401  (settings for the tests)
sys.path.insert(0, os.path.dirname(HERE))
import power    # noqa: E402
import tasks    # noqa: E402

TZ = tasks.TZ
FAKE_TOKEN = "gh" + "p_" + "abcdefghij" * 3        # built at run time: a secret-shaped string, not a secret

NOTES = """# notes
- 2026-11-16 09:00 SAM'S WORK TASKS (old list):
  1. OPS-1 old task: should never show.
- 2026-11-17 10:00 MT something else entirely
  1. OPS-9 not a task block: nested under another entry.
- 2026-11-17 16:40Z SAM'S WORK TASKS (current; replaces the old list):
  1. OPS-570 Tribunal: DONE (closed).
  2. OPS-572 Device check on a phone: Marco sets up the decode credential 11-18 (staging build); then the phone. https://linear.app/x/issue/OPS-572
  3. OPS-573 `DevOps` user in the registry: DONE on staging (#146). Prod = publish the draft.
  4. ACME-2410 follow-ons ACME-2420, ACME-4726 (reminder 11-19 08:30).
  Off Sam's plate: OPS-569 (Priya's).
- 2026-11-17 21:45 MT SCREEN notes: parsed from the newest "SAM'S WORK TASKS" block; keep the format.
  5. OPS-999 a numbered line under a later entry that only mentions the block.
"""


class Block(unittest.TestCase):
    def test_newest_block_wins_and_stops_at_next_entry(self):
        raw = tasks.find_block(NOTES.splitlines())
        self.assertEqual(len(raw), 4)
        self.assertTrue(raw[0].strip().startswith("1. OPS-570"))
        self.assertTrue(all("OPS-1 " not in x and "OPS-999" not in x and "OPS-9 " not in x for x in raw))

    def test_skips_off_dans_plate(self):
        raw = tasks.find_block(NOTES.splitlines())
        self.assertFalse(any("Off Sam" in x for x in raw))

    def test_missing_block(self):
        self.assertIsNone(tasks.find_block(["- 2026-11-17 nothing here", "  1. OPS-1 x: y"]))
        out = tasks.build(["- 2026-11-17 nothing here"])
        self.assertEqual(out, {"show": False, "items": [], "total": 0, "done": 0})

    def test_quoted_mention_is_not_a_header(self):
        self.assertFalse(tasks._HEADER.search(NOTES.splitlines()[-2]))

    def test_tail_read_finds_block_in_large_file(self):
        head, tail = NOTES.split("- 2026-11-17 16:40Z")
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as f:
            f.write(head)
            f.write(("- 2026-11-17 filler " + "x" * 500 + "\n") * 2000)
            f.write("- 2026-11-17 16:40Z" + tail)
            f.write(("- 2026-11-17 later " + "y" * 300 + "\n") * 100)
            path = f.name
        try:
            raw = tasks.find_block(tasks.read_tail_lines(path, start=4096))
            self.assertEqual(len(raw), 4)
            self.assertIn("OPS-570", raw[0])
        finally:
            os.unlink(path)


class Render(unittest.TestCase):
    def items(self):
        return [tasks.render(x) for x in tasks.find_block(NOTES.splitlines())]

    def test_title_ids_state(self):
        it = self.items()
        self.assertEqual((it[0]["ids"], it[0]["title"], it[0]["state"], it[0]["done"]),
                         ("OPS-570", "Tribunal", "DONE (closed)", True))
        self.assertEqual(it[1]["ids"], "OPS-572")
        self.assertEqual(it[1]["title"], "Device check on a phone")
        self.assertEqual(it[1]["state"], "Marco sets up the decode credential 11-18 (staging build)")
        self.assertEqual(it[1]["n"], 2)

    def test_done_only_when_state_is_done(self):
        it = self.items()
        self.assertTrue(it[0]["done"])
        self.assertFalse(it[2]["done"])            # "DONE on staging" is not finished
        self.assertEqual(it[2]["state"], "DONE on staging (#146)")

    def test_urls_and_backticks_stripped(self):
        for it in self.items():
            for v in (it["title"], it["state"]):
                self.assertNotIn("http", v)
                self.assertNotIn("`", v)
        self.assertEqual(self.items()[2]["title"], "DevOps user in the registry")

    def test_time_colon_is_not_the_title_split(self):
        it = self.items()[3]
        self.assertEqual(it["ids"], "ACME-2410")
        self.assertIn("08:30", it["title"])
        self.assertEqual(it["state"], "")

    def test_truncation(self):
        it = tasks.render("  7. OPS-7 short title: " + "word " * 40 + "; second clause")
        self.assertLessEqual(len(it["state"]), tasks.STATE_MAX)
        self.assertTrue(it["state"].endswith("…"))
        it = tasks.render("  8. OPS-8 " + "longtitle " * 20)
        self.assertLessEqual(len(it["title"]), tasks.TITLE_MAX)
        self.assertTrue(it["title"].endswith("…"))
        it = tasks.render("  9. OPS-9 t: abcdefgh abcdefgh abcdefgh abcdefgh abcdefgh abcd (inside a bracket that goes on)")
        self.assertNotIn("(", it["state"])
        self.assertTrue(it["state"].endswith("…"))

    def test_counts(self):
        out = tasks.build(NOTES.splitlines())
        self.assertEqual((out["show"], out["total"], out["done"]), (True, 3, 1))   # done items are not shown or counted
        self.assertEqual(out["total"], len(out["items"]))
        self.assertNotIn("full", out["items"][0])
        self.assertNotIn("hidden", out)


class NoPrivacyScreen(unittest.TestCase):
    """The task list has no privacy screen."""

    def test_any_words_show(self):
        lines = ["- 2026-11-17 SAM'S WORK TASKS:",
                 "  1. OPS-1 ship the registry: in review.",
                 "  2. OPS-2 dentist appointment: tomorrow.",
                 "  3. OPS-3 quarterly invoice for the vendor: due.",
                 "  4. OPS-4 rotate the secret key: key " + FAKE_TOKEN,
                 "  5. OPS-5 deploy: fine; then the offsite afterwards.",
                 "  6. OPS-6 write the runbook"]
        out = tasks.build(lines)
        self.assertEqual([it["ids"] for it in out["items"]], ["OPS-1", "OPS-2", "OPS-3", "OPS-4", "OPS-5", "OPS-6"])
        self.assertEqual([it["n"] for it in out["items"]], [1, 2, 3, 4, 5, 6])
        self.assertEqual((out["total"], out["show"]), (6, True))
        self.assertNotIn("hidden", out)
        text = json.dumps(out)
        for word in ("dentist", "invoice", "secret"):
            self.assertIn(word, text)

    def test_no_screen_module_is_loaded(self):
        self.assertFalse(hasattr(tasks, "load_blocked"))
        self.assertFalse(hasattr(tasks, "screen_items"))
        self.assertFalse(hasattr(tasks, "SCREEN_PY"))

    def test_unreadable_source_shows_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = os.path.join(d, "config.json")
            with open(cfg, "w") as f:
                json.dump({"notes": os.path.join(d, "missing.md")}, f)
            src = tasks.Source(notes=_env.notes_of(cfg))
            self.assertEqual(src.current(), tasks.EMPTY)
            with open(os.path.join(d, "missing.md"), "w") as f:
                f.write(NOTES)
            self.assertEqual(src.current()["total"], 3)   # open tasks only


class Hours(unittest.TestCase):
    def at(self, y, mo, d, h, mi):
        return datetime(y, mo, d, h, mi, tzinfo=TZ)

    def test_weekday_edges(self):
        # 2026-11-18 is a Wednesday
        self.assertFalse(tasks.in_hours(self.at(2026, 11, 18, 7, 59)))
        self.assertTrue(tasks.in_hours(self.at(2026, 11, 18, 8, 0)))
        self.assertTrue(tasks.in_hours(self.at(2026, 11, 18, 15, 59)))
        self.assertFalse(tasks.in_hours(self.at(2026, 11, 18, 16, 0)))

    def test_weekend_never(self):
        self.assertFalse(tasks.in_hours(self.at(2026, 11, 21, 10, 0)))   # Saturday
        self.assertFalse(tasks.in_hours(self.at(2026, 11, 22, 10, 0)))   # Sunday
        self.assertTrue(tasks.in_hours(self.at(2026, 11, 20, 10, 0)))    # Friday

    def test_utc_input_uses_mountain_clock(self):
        self.assertTrue(tasks.in_hours(datetime(2026, 11, 18, 15, 30, tzinfo=timezone.utc)))    # 08:30 MST Wed
        self.assertFalse(tasks.in_hours(datetime(2026, 11, 21, 1, 0, tzinfo=timezone.utc)))     # 18:00 MST Fri

    def test_payload_empty_outside_hours(self):
        class Src:
            def current(self):
                return {"show": True, "items": [{"n": 1}], "total": 1, "done": 0}
        with mock.patch.dict(os.environ, {tasks.FORCE_ENV: ""}):
            self.assertEqual(tasks.payload(Src(), self.at(2026, 11, 21, 10, 0)), tasks.EMPTY)
            self.assertEqual(tasks.payload(Src(), self.at(2026, 11, 18, 16, 0)), tasks.EMPTY)
            self.assertEqual(tasks.payload(Src(), self.at(2026, 11, 18, 10, 0))["total"], 1)
        with mock.patch.dict(os.environ, {tasks.FORCE_ENV: "1"}):
            self.assertEqual(tasks.payload(Src(), self.at(2026, 11, 21, 10, 0))["total"], 1)

    def test_unit_never_sets_the_override(self):
        unit = os.path.join(os.path.dirname(HERE), "systemd", "kiosk-server.service")
        with open(unit) as f:
            self.assertNotIn(tasks.FORCE_ENV, f.read())


class Power(unittest.TestCase):
    def at(self, d, h, mi, s=0):
        return datetime(2026, 11, d, h, mi, s, tzinfo=TZ)

    def test_across_midnight(self):
        self.assertEqual(power.wanted(self.at(17, 23, 59)), "on")
        self.assertEqual(power.wanted(self.at(18, 0, 0)), "on")
        self.assertEqual(power.wanted(self.at(18, 0, 59)), "on")
        self.assertEqual(power.wanted(self.at(18, 1, 0)), "off")
        self.assertEqual(power.wanted(self.at(18, 3, 30)), "off")
        self.assertEqual(power.wanted(self.at(18, 5, 59, 59)), "off")
        self.assertEqual(power.wanted(self.at(18, 6, 0)), "on")
        self.assertEqual(power.wanted(self.at(18, 12, 0)), "on")

    def test_utc_input(self):
        self.assertEqual(power.wanted(datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)), "off")   # 02:00 MST
        self.assertEqual(power.wanted(datetime(2026, 11, 18, 13, 0, tzinfo=timezone.utc)), "on")   # 06:00 MST

    def test_parse_outputs(self):
        text = ('HDMI-A-1 "DZX KYY (HDMI-A-1)"\n  Make: DZX\n  Enabled: yes\n  Modes:\n    1920x1080 px\n'
                'DP-2 "Other"\n  Enabled: no\n')
        self.assertEqual(power.parse_outputs(text), {"HDMI-A-1": True, "DP-2": False})

    def test_no_socket_exits_quietly(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": d, "WAYLAND_DISPLAY": "wayland-x"}), \
                mock.patch("subprocess.run") as run:
            self.assertEqual(power.main([]), 0)
            run.assert_not_called()

    def test_sets_only_when_different(self):
        calls = []

        def fake(args, **kw):
            calls.append(args)
            return mock.Mock(returncode=0, stdout='HDMI-A-1 "x"\n  Enabled: yes\n', stderr="")
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "wayland-0"), "w").close()
            env = {"XDG_RUNTIME_DIR": d, "WAYLAND_DISPLAY": "wayland-0"}
            with mock.patch.dict(os.environ, env), mock.patch("subprocess.run", side_effect=fake), \
                    mock.patch.object(power, "wanted", return_value="on"):
                self.assertEqual(power.main([]), 0)
            self.assertEqual(calls, [["wlr-randr"]])
            calls.clear()
            with mock.patch.dict(os.environ, env), mock.patch("subprocess.run", side_effect=fake), \
                    mock.patch.object(power, "wanted", return_value="off"):
                self.assertEqual(power.main([]), 0)
            self.assertEqual(calls, [["wlr-randr"], ["wlr-randr", "--output", "HDMI-A-1", "--off"]])


if __name__ == "__main__":
    unittest.main()


class WaitingOn(unittest.TestCase):
    def test_marker_is_parsed_and_removed_from_the_state(self):
        it = tasks.render("  2. OPS-572 Device check on the office phone: credential made 11-18. WAITING ON: Marco (credential upload), the office iPhone.")
        self.assertEqual(it["waiting"], "Marco (credential upload), the office iPhone")
        self.assertNotIn("WAITING", it["state"])
        self.assertEqual(it["ids"], "OPS-572")

    def test_semicolon_ends_it_and_done_items_never_wait(self):
        it = tasks.render("  3. OPS-1 thing: half done; WAITING ON: Priya; more text")
        self.assertEqual(it["waiting"], "Priya")
        self.assertEqual(tasks.render("  1. OPS-2 x: DONE WAITING ON: nobody")["waiting"], "")

    def test_no_marker(self):
        self.assertEqual(tasks.render("  4. OPS-3 thing: in progress")["waiting"], "")

    def test_count_and_the_waiting_text_shows(self):
        lines = ["- 2026-11-18 SAM'S WORK TASKS (current):",
                 "  1. OPS-1 a: going. WAITING ON: Marco.",
                 "  2. OPS-2 b: going. WAITING ON: the lab results.",
                 "  3. OPS-3 c: going."]
        out = tasks.build(lines)
        self.assertEqual((out["waiting"], out["total"]), (2, 3))
        self.assertEqual(out["items"][1]["waiting"], "the lab results")


class Priority(unittest.TestCase):
    def test_tag_parsed_and_removed(self):
        it = tasks.render("  1. [P1] OPS-568 fleet monitor: built")
        self.assertEqual((it["prio"], it["ids"], it["title"]), (1, "OPS-568", "fleet monitor"))
        self.assertIsNone(tasks.render("  2. OPS-1 x: y")["prio"])
        self.assertEqual(tasks.render("  3. [p3] OPS-2 z: w")["prio"], 3)

    def test_sorted_by_priority_done_last_stable_within_level(self):
        lines = ["- 2026-11-18 SAM'S WORK TASKS (current):",
                 "  1. OPS-1 done one: DONE",
                 "  2. [P3] OPS-2 later: x",
                 "  3. OPS-3 untagged: x",
                 "  4. [P1] OPS-4 now a: x",
                 "  5. [P2] OPS-5 week: x",
                 "  6. [P1] OPS-6 now b: x"]
        out = tasks.build(lines)
        self.assertEqual([i["ids"] for i in out["items"]], ["OPS-4", "OPS-6", "OPS-5", "OPS-2", "OPS-3"])   # OPS-1 is done: not shown
        self.assertEqual([i["n"] for i in out["items"]], [1, 2, 3, 4, 5])   # 1..N in display order, no gaps
        self.assertEqual(out["done"], 1)


class InProgress(unittest.TestCase):
    def test_marker_parsed_counted_and_removed(self):
        lines = ["- 2026-11-18 SAM'S WORK TASKS (current):",
                 "  1. [P1] OPS-1 a: building. IN PROGRESS: Agent (background).",
                 "  2. [P2] OPS-2 b: blocked. WAITING ON: Priya. IN PROGRESS: Sam.",
                 "  3. [P3] OPS-3 c: idle."]
        out = tasks.build(lines)
        a, b, c = out["items"]
        self.assertEqual(a["active"], "Agent (background)")
        self.assertNotIn("IN PROGRESS", a["state"])
        self.assertEqual((b["waiting"], b["active"]), ("Priya", "Sam"))
        self.assertEqual(c["active"], "")
        self.assertEqual(out["active"], 1)   # waiting wins over in progress in the count

    def test_the_active_text_shows(self):
        lines = ["- 2026-11-18 SAM'S WORK TASKS (current):", "  1. OPS-1 a: x. IN PROGRESS: the dentist visit."]
        out = tasks.build(lines)
        self.assertEqual((out["active"], out["items"][0]["active"]), (1, "the dentist visit"))

