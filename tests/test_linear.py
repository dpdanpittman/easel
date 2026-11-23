"""Offline tests for the Linear half of the work task list (tasks.py): merge rules, numbering, cache, keys, untrusted titles.

No network and no key command: the HTTP post and the key lookup are fakes. Run:
python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _env  # noqa: E402,F401  (settings for the tests)
sys.path.insert(0, os.path.dirname(HERE))
import tasks    # noqa: E402

KEYS = {("kiosk-test-key", "globex"): "lin_api_" + "globexFAKE" * 4,
        ("kiosk-test-key", "acme"): "lin_api_" + "acmeFAKE" * 4}
GLOBEX_KEY, ACME_KEY = KEYS.values()


def node(ident, title, prio=3, state="Todo", typ="unstarted"):
    return {"identifier": ident, "title": title, "priority": prio, "state": {"name": state, "type": typ}}


def page(nodes, teams, cursor=None):
    return {"data": {"viewer": {"assignedIssues": {"nodes": nodes,
                                                   "pageInfo": {"hasNextPage": bool(cursor), "endCursor": cursor}}},
                     "teams": {"nodes": [{"key": k} for k in teams]}}}


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeLinear:
    """Both workspaces as a fake HTTP post, keyed by the Authorization key."""

    def __init__(self, globex=None, acme=None):
        self.data = {GLOBEX_KEY: globex, ACME_KEY: acme}
        self.calls = []

    def __call__(self, key, body):
        self.calls.append((key, body["variables"]["after"]))
        resp = self.data[key]
        if isinstance(resp, Exception):
            raise resp
        if isinstance(resp, list):                 # pages by cursor
            return resp[0] if body["variables"]["after"] is None else resp[int(body["variables"]["after"])]
        return resp


def make(post, keys=KEYS, clock=None):
    lines, got = [], []

    def get_key(item):
        got.append(item)
        return keys.get(item)
    lin = tasks.Linear(get_key=get_key, post=post, clock=clock or Clock(), log=lines.append)
    return lin, lines, got


GLOBEX = page([node("OPS-572", "Device check", 2, "In Progress", "started"),
              node("OPS-600", "New ticket from Linear", 1, "Todo", "unstarted"),
              node("WEB-12", "Engineering thing", 4)], ["OPS", "WEB"])
ACME = page([node("ACME-2410", "Billing export", 2, "In Review", "started"),
               node("ACME-4726", "Follow-on", 3),
               node("ACME-4893", "CI shorthand", 0)], ["ACME"])

NOTES = ["- 2026-11-19 SAM'S WORK TASKS (current):",
           "  1. [P1] OPS-572 Device check on a phone: staging build. WAITING ON: Marco.",
           "  2. [P2] OPS-573 registry user: published.",
           "  3. [P3] ACME-2410 follow-ons ACME-2420, ACME-4726: reminder 11-19.",
           "  4. [P2] Acme CI speed ACME-4892/4893: measuring. IN PROGRESS: Agent.",
           "  5. [P1] XYZ-1 not a Linear team: keep.",
           "  Off Sam's plate: OPS-569 (Priya's)."]


def ok(lin):
    lin.refresh()
    return lin.snapshot()


class Merge(unittest.TestCase):
    def build(self, snap, lines=NOTES):
        return tasks.build(lines, snap)

    def test_overrides_drop_closed_and_add_linear_only(self):
        lin, _, _ = make(FakeLinear(GLOBEX, ACME))
        out = self.build(ok(lin))
        by = {(it["ids"] or it["title"]).split()[0]: it for it in out["items"]}
        # notes override kept whole
        self.assertEqual((by["OPS-572"]["title"], by["OPS-572"]["waiting"], by["OPS-572"]["prio"],
                          by["OPS-572"]["source"]), ("Device check on a phone", "Marco", 1, "notes"))
        # OPS-573 is not open in Linear (closed or not Sam's): dropped, not counted
        self.assertNotIn("OPS-573", by)
        # "total" counts tickets: the ACME-2410/4726 grouped row counts 2 (2026-11-23)
        self.assertEqual((out["total"], out["rows"]), (len(out["items"]) + 1, len(out["items"])))
        # a prefix no workspace has stays as written
        self.assertIn("XYZ-1", by)
        # the CI line names ACME-4892 (closed) and ACME-4893 (open): one ticket left, a normal row for it with the
        # hand title (ids taken out) and the hand note (2026-11-23)
        self.assertEqual((by["ACME-4893"]["title"], by["ACME-4893"]["source"]), ("Acme CI speed", "notes"))
        # Linear-only issues appear with Linear's title, state name and mapped priority
        self.assertEqual((by["OPS-600"]["title"], by["OPS-600"]["state"], by["OPS-600"]["prio"], by["OPS-600"]["source"]),
                         ("New ticket from Linear", "Todo", 1, "linear"))
        self.assertEqual((by["WEB-12"]["prio"], by["WEB-12"]["state"], by["WEB-12"]["active"]), (3, "Todo", ""))
        self.assertEqual(by["OPS-600"]["active"], "")      # IN PROGRESS comes from the notes only

    def test_backlog_shows_only_when_the_notes_names_it(self):
        globex = page([node("OPS-700", "Old backlog by type", 2, "Icebox", "backlog"),
                      node("OPS-701", "Old backlog by name", 2, "Backlog", "unstarted"),
                      node("OPS-702", "Named backlog", 3, "Backlog", "backlog"),
                      node("OPS-703", "Mentioned backlog", 3, "Backlog", "backlog"),
                      node("OPS-704", "Live ticket", 3, "In Progress", "started")], ["OPS"])
        lin, _, _ = make(FakeLinear(globex, page([], ["ACME"])))
        lines = ["- 2026-11-19 SAM'S WORK TASKS (current):",
                 "  1. [P3] OPS-702 the one Sam picked: next up.",
                 "  2. [P3] Cleanup week OPS-703: later.",
                 "  3. [P2] OPS-699 follow-ons OPS-701: the parent is closed."]
        out = self.build(ok(lin), lines)
        # OPS-699 is closed, OPS-701 (backlog) is the line's one open ticket left: a notes row for it (2026-11-23)
        self.assertEqual(sorted((it["ids"] or it["title"], it["source"]) for it in out["items"]),
                         [("Cleanup week OPS-703", "notes"), ("OPS-701", "notes"), ("OPS-702", "notes"),
                          ("OPS-704", "linear")])
        self.assertEqual(out["total"], 4)

    def test_duplicate_state_is_closed(self):
        globex = page([node("OPS-710", "Marked duplicate", 3, "Duplicate", "duplicate"),
                      node("OPS-711", "Live ticket", 3, "In Progress", "started")], ["OPS"])
        lin, _, _ = make(FakeLinear(globex, page([], ["ACME"])))
        lines = ["- 2026-11-19 SAM'S WORK TASKS (current):", "  1. [P3] OPS-710 named but a duplicate: drop."]
        out = self.build(ok(lin), lines)
        self.assertEqual([it["ids"] for it in out["items"]], ["OPS-711"])
        self.assertIn('"duplicate"', tasks.QUERY)

    def test_multi_id_line_covers_the_others(self):
        lin, _, _ = make(FakeLinear(GLOBEX, ACME))
        out = self.build(ok(lin))
        ids = [it["ids"] for it in out["items"]]
        self.assertEqual(sum(1 for i in ids if i.startswith("ACME-2410")), 1)
        self.assertNotIn("ACME-4726", ids)      # a step of the ACME-2410 grouped row, not a row of its own
        self.assertEqual(ids.count("ACME-4893"), 1)   # the CI line's one open ticket: shown once, on that line
        self.assertEqual([it["source"] for it in out["items"] if it["ids"] == "ACME-4893"], ["notes"])

    def test_dropped_line_does_not_hide_its_open_tickets(self):
        # ACME-2410 is canceled (not open), its follow-on ACME-4726 is open: ACME-4726 shows (since 2026-11-23 as the
        # line's one ticket left, a normal notes row keeping Sam's words; before that as a Linear-only row).
        acme = page([node("ACME-4726", "Follow-on", 3), node("ACME-4893", "CI shorthand", 0)], ["ACME"])
        lin, _, _ = make(FakeLinear(GLOBEX, acme))
        out = self.build(ok(lin))
        ids = [it["ids"] for it in out["items"]]
        self.assertFalse(any(i.startswith("ACME-2410") for i in ids))
        self.assertIn("ACME-4726", ids)
        self.assertEqual([(it["source"], it["title"]) for it in out["items"] if it["ids"] == "ACME-4726"],
                         [("notes", "follow-ons")])

    def test_sort_and_numbering(self):
        lin, _, _ = make(FakeLinear(GLOBEX, ACME))
        out = self.build(ok(lin))
        order = [(it["prio"], it["source"], it["ids"]) for it in out["items"]]
        self.assertEqual(order, [(1, "notes", "OPS-572"), (1, "notes", "XYZ-1"), (1, "linear", "OPS-600"),
                                 (2, "notes", "ACME-4893"), (3, "notes", "ACME-2410/4726"), (3, "linear", "WEB-12")])
        # 1..N in display order, no gaps; the grouped row takes a number per ticket (5 and 6)
        self.assertEqual([it["n"] for it in out["items"]], [1, 2, 3, 4, 5, 7])
        self.assertEqual([t["n"] for t in out["items"][4]["group"]["tickets"]], [5, 6])
        self.assertNotIn("_order", json.dumps(out))
        self.assertEqual((out["total"], out["rows"]), (7, 6))

    def test_workspace_failure_keeps_its_notes_items(self):
        lin, lines, _ = make(FakeLinear(GLOBEX, urllib.error.URLError("down")))
        snap = ok(lin)
        self.assertEqual(snap["status"], "stale")
        lines2 = NOTES[:1] + ["  6. [P2] ACME-9999 closed in Acme maybe: x.", "  7. [P2] OPS-9999 closed in Globex: x."]
        out = self.build(snap, lines2)
        ids = [it["ids"] for it in out["items"]]
        self.assertIn("ACME-9999", ids)        # Acme fetch failed: kept
        self.assertNotIn("OPS-9999", ids)      # Globex fetch fine and not open: dropped
        self.assertEqual(lines, ["tasks: linear acme fetch failed (URLError)"])

    def test_no_privacy_screen_on_linear_titles(self):
        # Words like these no longer hide an item.
        globex = page([node("OPS-700", "dentist appointment"), node("OPS-701", "the quarterly invoice"),
                      node("OPS-702", "rotate the secret key"), node("OPS-703", "fine one")], ["OPS"])
        lin, _, _ = make(FakeLinear(globex, page([], ["ACME"])))
        out = tasks.build([], ok(lin))
        self.assertEqual([it["ids"] for it in out["items"]], ["OPS-700", "OPS-701", "OPS-702", "OPS-703"])
        self.assertEqual(([it["n"] for it in out["items"]], out["total"]), ([1, 2, 3, 4], 4))
        self.assertNotIn("hidden", out)
        text = json.dumps(out)
        for word in ("dentist", "invoice", "secret"):
            self.assertIn(word, text)

    def test_numbering_has_no_gaps_after_drops(self):
        # Notes numbers 1, 4, 9 (closed tickets left gaps) plus two Linear-only issues: shown as 1..5.
        globex = page([node("OPS-1", "a"), node("OPS-4", "b"), node("OPS-9", "c"), node("OPS-20", "d"), node("OPS-21", "e")],
                     ["OPS"])
        lin, _, _ = make(FakeLinear(globex, page([], ["ACME"])))
        lines = ["- 2026-11-19 SAM'S WORK TASKS (current):", "  1. [P2] OPS-1 a: x.", "  2. [P2] OPS-2 closed: x.",
                 "  4. [P3] OPS-4 b: x.", "  9. [P1] OPS-9 c: x."]
        out = tasks.build(lines, ok(lin))
        self.assertEqual([(it["n"], it["ids"]) for it in out["items"]],
                         [(1, "OPS-9"), (2, "OPS-1"), (3, "OPS-4"), (4, "OPS-20"), (5, "OPS-21")])
        self.assertEqual(out["total"], 5)

    def test_untrusted_titles_cleaned(self):
        globex = page([node("OPS-800", "see https://evil.example/x `code` ‮done\x07" + "x" * 200),
                      node("bad id", "skipped"), node("OPS-801", 42), node("OPS-802", "closed", typ="completed")],
                     ["OPS"])
        lin, _, _ = make(FakeLinear(globex, page([], ["ACME"])))
        out = tasks.build([], ok(lin))
        self.assertEqual([it["ids"] for it in out["items"]], ["OPS-800"])
        title = out["items"][0]["title"]
        self.assertNotIn("http", title)
        self.assertNotIn("`", title)
        self.assertNotIn("‮", title)
        self.assertLessEqual(len(title), tasks.TITLE_MAX)

    def test_priority_mapping(self):
        nodes = [node("OPS-%d" % p, "t", p) for p in range(5)]
        lin, _, _ = make(FakeLinear(page(nodes, ["OPS"]), page([], ["ACME"])))
        out = tasks.build([], ok(lin))
        self.assertEqual({it["ids"]: it["prio"] for it in out["items"]},
                         {"OPS-0": 3, "OPS-1": 1, "OPS-2": 2, "OPS-3": 3, "OPS-4": 3})

    def test_off_is_exactly_the_notes_behaviour(self):
        off = {"ws": {}, "status": "off"}
        self.assertEqual(tasks.build(NOTES, None)["items"],
                         [{k: v for k, v in it.items() if k != "source"}
                          for it in tasks.build(NOTES, off)["items"]])
        self.assertEqual(tasks.build([], off), {"show": False, "items": [], "total": 0, "done": 0})

    def test_done_notes_line_still_covers(self):
        lines = NOTES[:1] + ["  1. OPS-600 shipped: DONE."]
        lin, _, _ = make(FakeLinear(GLOBEX, ACME))
        out = tasks.build(lines, ok(lin))
        self.assertNotIn("OPS-600", [it["ids"] for it in out["items"]])


class Cache(unittest.TestCase):
    def test_pagination(self):
        p1 = page([node("OPS-1", "a")], ["OPS"], cursor="1")
        p2 = page([node("OPS-2", "b")], ["OPS"])
        fake = FakeLinear([p1, p2], page([], ["ACME"]))
        lin, _, _ = make(fake)
        snap = ok(lin)
        self.assertEqual([i["identifier"] for i in snap["ws"]["globex"]["issues"]], ["OPS-1", "OPS-2"])
        self.assertEqual(snap["status"], "ok")

    def test_ttl_five_minutes_and_one_hour(self):
        clock = Clock()
        fake = FakeLinear(GLOBEX, ACME)
        lin, _, _ = make(fake, clock=clock)
        lin.maybe_refresh()
        for t in list(__import__("threading").enumerate()):
            if t.name == "linear-fetch":
                t.join(5)
        n = len(fake.calls)
        self.assertEqual(n, 2)
        clock.t += 299
        lin.maybe_refresh()                      # under 5 minutes: no fetch
        self.assertEqual(len(fake.calls), n)
        self.assertFalse(lin._running)
        # a failure later keeps the last good result for an hour, then off
        fake.data[GLOBEX_KEY] = fake.data[ACME_KEY] = urllib.error.URLError("down")
        clock.t += 2
        lin.refresh()
        self.assertEqual(lin.snapshot()["status"], "stale")
        self.assertEqual(set(lin.snapshot()["ws"]), {"globex", "acme"})
        clock.t += tasks.GOOD_FOR_S
        snap = lin.snapshot()
        self.assertEqual((snap["ws"], snap["status"], snap["at"]), ({}, "off", None))
        self.assertEqual({w for w, (why, _) in snap["missing"].items() if why == "down"}, {"globex", "acme"})

    def test_missing_key_falls_back_silently_and_retries_later(self):
        clock = Clock()
        fake = FakeLinear(GLOBEX, ACME)
        lin, lines, got = make(fake, keys={}, clock=clock)
        lin.refresh()
        lin.refresh()
        self.assertEqual(fake.calls, [])
        snap = lin.snapshot()
        self.assertEqual((snap["ws"], snap["status"], snap["at"]), ({}, "off", None))
        self.assertEqual({w: why for w, (why, _) in snap["missing"].items()}, {"globex": "down", "acme": "down"})
        self.assertEqual(len(got), 2)            # second refresh inside the retry window: key_cmd not run again
        self.assertEqual(lines, ["tasks: linear globex: no key yet, using the notes"])   # one line per hour
        clock.t += tasks.KEY_RETRY_S
        lin.refresh()
        self.assertEqual(len(got), 4)
        self.assertEqual(len(lines), 1)
        clock.t += tasks.LOG_EVERY_S
        lin.refresh()
        self.assertEqual(len(lines), 2)

    def test_401_drops_the_key(self):
        fake = FakeLinear(urllib.error.HTTPError(tasks.LINEAR_URL, 401, "no", {}, None), ACME)
        lin, lines, got = make(fake)
        lin.refresh()
        self.assertNotIn("globex", lin._keys)
        self.assertIn("acme", lin._keys)
        self.assertEqual(lines, ["tasks: linear globex fetch failed (HTTPError 401)"])

    def test_graphql_errors_are_a_failure(self):
        lin, lines, _ = make(FakeLinear({"errors": [{"message": "bad"}]}, ACME))
        snap = ok(lin)
        self.assertEqual((set(snap["ws"]), snap["status"]), ({"acme"}, "stale"))

    def test_key_never_in_logs_or_output(self):
        clock = Clock()
        fake = FakeLinear(RuntimeError("boom " + GLOBEX_KEY), ValueError(ACME_KEY))
        lin, lines, _ = make(fake, clock=clock)
        lin.refresh()
        clock.t += tasks.LOG_EVERY_S
        lin.refresh()
        fake.data = {GLOBEX_KEY: GLOBEX, ACME_KEY: ACME}
        clock.t += tasks.FETCH_EVERY_S
        out = tasks.build(NOTES, ok(lin))
        blob = "\n".join(lines) + json.dumps(out) + repr(lin.snapshot())
        for key in KEYS.values():
            self.assertNotIn(key, blob)
            self.assertNotIn(key[:12], blob)
        self.assertTrue(lines)

    def test_key_cmd_validation(self):
        class R:
            def __init__(self, code, out):
                self.returncode, self.stdout = code, out
        with unittest.mock.patch.object(tasks.subprocess, "run", return_value=R(0, "lin_api_x\n")) as run:
            self.assertEqual(tasks.cmd_key(("pass", "show", "linear")), "lin_api_x")
            argv = run.call_args[0][0]
            self.assertEqual(argv, ["pass", "show", "linear"])          # the command as given, never a shell
            self.assertNotIn("shell", run.call_args[1])
        with unittest.mock.patch.object(tasks.subprocess, "run", return_value=R(1, "")):
            self.assertIsNone(tasks.cmd_key(("pass", "show", "linear")))
        with unittest.mock.patch.object(tasks.subprocess, "run", return_value=R(0, "two words")):
            self.assertIsNone(tasks.cmd_key(("pass", "show", "linear")))
        with unittest.mock.patch.object(tasks.subprocess, "run", side_effect=OSError("x")):
            self.assertIsNone(tasks.cmd_key(("pass", "show", "linear")))


class SourceWithLinear(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.cfg = os.path.join(self.d.name, "config.json")
        self.notes = os.path.join(self.d.name, "notes.md")
        with open(self.cfg, "w") as f:
            json.dump({"notes": self.notes}, f)

    def tearDown(self):
        self.d.cleanup()

    def test_missing_notes_shows_linear_and_status(self):
        lin, _, _ = make(FakeLinear(GLOBEX, ACME))
        lin.refresh()
        lin.maybe_refresh = lambda: None
        src = tasks.Source(notes=_env.notes_of(self.cfg), linear=lin)
        out = src.current()
        self.assertEqual(out["linear"], "ok")
        self.assertEqual(len(out["items"]), 6)
        with open(self.notes, "w") as f:
            f.write("\n".join(NOTES) + "\n")
        out = src.current()
        self.assertEqual(out["items"][0]["ids"], "OPS-572")
        self.assertEqual(out["items"][0]["source"], "notes")

    def test_never_raises(self):
        class Broken:
            def maybe_refresh(self):
                raise RuntimeError("x")
        with open(self.notes, "w") as f:
            f.write("\n".join(NOTES) + "\n")
        out = tasks.Source(notes=_env.notes_of(self.cfg), linear=Broken()).current()
        self.assertEqual(out["linear"], "off")
        self.assertEqual(len(out["items"]), 5)      # the notes alone (done items aside)

    def test_server_wiring(self):
        with open(os.path.join(os.path.dirname(HERE), "server.py")) as f:
            src = f.read()
            self.assertIn("_linear = tasks.Linear(cache_file=diskcache.path(tasks.LINEAR_CACHE))", src)
            self.assertIn("tasks.Source(linear=_linear, flight=lambda: _inflight_src.flight(),", src)
            self.assertIn('_ticket_prs = derive.TicketPRs(cache_file=diskcache.path("github-ticket-prs-last-good.json"),', src)
            self.assertIn("prs=_ticket_prs)", src)
            self.assertIn("linear=_linear, ticket_prs=_ticket_prs)", src)
            self.assertIn('inflight.GitHub(cache_file=diskcache.path("github-prs-last-good.json"))', src)


import unittest.mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
