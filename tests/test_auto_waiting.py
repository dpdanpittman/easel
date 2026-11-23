"""Offline tests for the automatic "Waiting on you" line: inflight.auto_waiting, its sources (Linear attachments in
tasks.parse_page, closed PRs and the linked-PR lookup in derive.TicketPRs) and the /api/inflight merge.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _env  # noqa: E402,F401  (settings for the tests)
sys.path.insert(0, os.path.dirname(HERE))
import derive    # noqa: E402
import inflight  # noqa: E402
import tasks     # noqa: E402

TZ = tasks.TZ
NOW_DT = datetime(2026, 11, 23, 10, 0, tzinfo=TZ)
NOW = int(NOW_DT.timestamp())
REF1, REF2 = "acme-inc/acme#101", "acme-inc/acme#102"


def issue(ident="ACME-4896", state="In Review", prs=(REF1, REF2), title="Measure CI cache speedup"):
    return {"identifier": ident, "title": title, "priority": 3, "state": state, "backlog": False,
            "prs": list(prs) if prs is not None else None}


def linear(*issues, ws="acme"):
    return {"ws": {ws: {"issues": list(issues), "teams": {"ACME"}}}, "status": "ok", "at": NOW}


def pr(number, state="merged", ids=(), owner_repo="acme-inc/acme"):
    return {"repo": owner_repo.split("/")[1], "owner_repo": owner_repo, "number": number, "state": state,
            "draft": False, "ci": "passing", "merged_at": NOW - 86400 if state == "merged" else None,
            "ids": sorted(ids)}


def prs(search=(), closed=(), looked=None, status="ok"):
    return {"prs": [p for p in search if p["state"] != "closed"],
            "closed": [p for p in search if p["state"] == "closed"] + list(closed),
            "looked": dict(looked or {}), "status": status, "at": NOW}


MERGED_BOTH = {REF1: "merged", REF2: "merged"}


class Rule(unittest.TestCase):
    def ids(self, lin, pr_snap, hand=()):
        return [w["id"] for w in inflight.auto_waiting(lin, pr_snap, hand)]

    def test_all_merged_gives_the_line(self):
        out = inflight.auto_waiting(linear(issue()), prs(looked=MERGED_BOTH))
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["source"], "auto")
        self.assertTrue(out[0]["text"].startswith("ACME-4896: PRs merged, close or finish it"))
        self.assertIn("Measure CI cache speedup", out[0]["text"])
        self.assertLessEqual(len(out[0]["text"]), inflight.TEXT_MAX)

    def test_long_title_is_shortened_and_no_room_drops_it(self):
        text = inflight.auto_line("ACME-4896", "word " * 60)
        self.assertLessEqual(len(text), inflight.TEXT_MAX)
        self.assertTrue(text.endswith("…"))
        self.assertEqual(inflight.auto_line("ACME-4896", ""), "ACME-4896: PRs merged, close or finish it")

    # the four conditions, each false -> no line
    def test_not_assigned_or_not_open_means_not_in_the_snapshot(self):
        # Linear's own query returns only Sam's open issues: a ticket not in it (closed, someone else's) gets nothing
        self.assertEqual(self.ids(linear(), prs(looked=MERGED_BOTH)), [])

    def test_not_in_review(self):
        for state in ("In Progress", "Todo", "Ready for Dev", "In Reviewing", ""):
            self.assertEqual(self.ids(linear(issue(state=state)), prs(looked=MERGED_BOTH)), [], state)
        self.assertEqual(self.ids(linear(issue(state=" in review ")), prs(looked=MERGED_BOTH)), ["ACME-4896"])

    def test_no_linked_pr(self):
        self.assertEqual(self.ids(linear(issue(prs=())), prs()), [])

    def test_one_open_pr(self):
        self.assertEqual(self.ids(linear(issue()), prs(looked={REF1: "merged", REF2: "open"})), [])

    def test_closed_unmerged_pr_is_ignored(self):
        # a superseded (closed, never merged) PR does not block the line
        self.assertEqual(self.ids(linear(issue()), prs(looked={REF1: "merged", REF2: "closed"})), ["ACME-4896"])
        # but closed only, nothing merged: no line
        self.assertEqual(self.ids(linear(issue()), prs(looked={REF1: "closed", REF2: "closed"})), [])
        # and one open PR still blocks, closed beside it or not
        self.assertEqual(self.ids(linear(issue(prs=(REF1, REF2, "acme-inc/acme#103"))),
                                  prs(looked={REF1: "merged", REF2: "closed", "acme-inc/acme#103": "open"})), [])

    def test_pr_matched_by_body_or_branch(self):
        # no attachment, a merged PR naming the ticket (derive.py's match): the line; an open one beside it: none
        self.assertEqual(self.ids(linear(issue(prs=())), prs([pr(7, ids={"ACME-4896"})])), ["ACME-4896"])
        self.assertEqual(self.ids(linear(issue(prs=())), prs([pr(7, ids={"ACME-4896"}),
                                                             pr(8, "open", ids={"ACME-4896"})])), [])
        # attachments all merged and a closed-unmerged PR naming it in its branch: the line (closed is ignored)
        self.assertEqual(self.ids(linear(issue()), prs([pr(9, "closed", ids={"ACME-4896"})], looked=MERGED_BOTH)),
                         ["ACME-4896"])

    def test_search_state_used_for_attached_pr(self):
        # the search returned the attached PR itself (open): it is not merged, whatever the lookup says
        self.assertEqual(self.ids(linear(issue()), prs([pr(101, "open")], looked=MERGED_BOTH)), [])
        self.assertEqual(self.ids(linear(issue()), prs([pr(101), pr(102)])), ["ACME-4896"])

    def test_hand_w_line_wins(self):
        self.assertEqual(self.ids(linear(issue()), prs(looked=MERGED_BOTH),
                                  ["ACME-4896: run the final measurement"]), [])
        self.assertEqual(self.ids(linear(issue()), prs(looked=MERGED_BOTH), ["ACME-48961 something else"]),
                         ["ACME-4896"])

    # fail closed
    def test_sources_missing(self):
        self.assertEqual(self.ids(None, prs(looked=MERGED_BOTH)), [])
        self.assertEqual(self.ids({"ws": {}, "status": "off", "at": None}, prs(looked=MERGED_BOTH)), [])
        self.assertEqual(self.ids(linear(issue()), None), [])
        for status in ("off", "pending"):
            self.assertEqual(self.ids(linear(issue()), prs(looked=MERGED_BOTH, status=status)), [], status)
        old = dict(prs(looked=MERGED_BOTH), closed=None)       # an older saved copy: closed PRs not known
        self.assertEqual(self.ids(linear(issue()), old), [])

    def test_unknown_states_or_attachments(self):
        self.assertEqual(self.ids(linear(issue()), prs(looked={REF1: "merged"})), [])     # REF2 never answered
        self.assertEqual(self.ids(linear(issue()), prs(looked={REF1: "merged", REF2: "weird"})), [])  # odd state
        self.assertEqual(self.ids(linear(issue(prs=None)), prs(looked=MERGED_BOTH)), [])  # attachments unknown
        foreign = issue(prs=("someone/else#1",))
        self.assertEqual(self.ids(linear(foreign), prs()), [])

    def test_never_raises(self):
        self.assertEqual(inflight.auto_waiting({"ws": {"x": None}}, prs(looked=MERGED_BOTH)), [])
        self.assertEqual(inflight.auto_waiting("junk", "junk"), [])

    def test_wanted_refs(self):
        lin = linear(issue(), issue("ACME-1", "In Progress", prs=("acme-inc/x#1",)), issue("ACME-2", prs=None))
        self.assertEqual(inflight.wanted_refs(lin), {REF1, REF2})
        self.assertEqual(inflight.wanted_refs(None), set())


class Merge(unittest.TestCase):
    def block(self, w=()):
        return {"waiting": [{"text": t, "source": "notes", "ref": ""} for t in w], "running": [], "finished": [],
                "counts": {"waiting": len(w), "running": 0, "finished": 0}}

    def test_json_field_order_and_count(self):
        auto = inflight.auto_waiting(linear(issue()), prs(looked=MERGED_BOTH))
        gh = {"prs": [{"repo": "x", "number": 3, "title": "t", "ci": "passing", "review": ""}]}
        out = inflight.merge(self.block(["OPS-1: do a thing"]), gh, auto)
        self.assertEqual([w["source"] for w in out["waiting"]], ["notes", "auto", "github"])
        self.assertEqual(out["counts"]["waiting"], 3)
        self.assertEqual(out["waiting"][1]["id"], "ACME-4896")
        self.assertEqual(out["waiting"][0], {"text": "OPS-1: do a thing", "source": "notes"})
        json.dumps(out)

    def test_hand_lines_unchanged_without_auto(self):
        a = inflight.merge(self.block(["OPS-1: x"]), {"prs": []})
        b = inflight.merge(self.block(["OPS-1: x"]), {"prs": []}, [])
        self.assertEqual(a, b)
        self.assertEqual(a["waiting"], [{"text": "OPS-1: x", "source": "notes"}])


class Fake:
    def __init__(self, snap):
        self.snap, self.refreshed = snap, 0

    def maybe_refresh(self):
        self.refreshed += 1

    def snapshot(self):
        return self.snap


class SourceWiring(unittest.TestCase):
    def run_source(self, lin, pr_snap, w_lines=()):
        with tempfile.TemporaryDirectory() as d:
            notes = os.path.join(d, "notes.md")
            with open(notes, "w") as f:
                f.write("- 2026-11-23 09:55 IN FLIGHT (current):\n" + "".join("  W. %s\n" % w for w in w_lines))
            cfg = os.path.join(d, "config.json")
            with open(cfg, "w") as f:
                json.dump({"notes": notes}, f)
            src = inflight.Source(notes=_env.notes_of(cfg), now=lambda: NOW_DT, linear=lin, ticket_prs=pr_snap)
            return src.current()

    def test_auto_line_in_payload_and_hand_wins(self):
        lin, tp = Fake(linear(issue())), Fake(prs(looked=MERGED_BOTH))
        out = self.run_source(lin, tp, ["OPS-1: something"])
        self.assertEqual([w["source"] for w in out["waiting"]], ["notes", "auto"])
        self.assertEqual(out["counts"]["waiting"], 2)
        self.assertEqual((lin.refreshed, tp.refreshed), (1, 1))
        out = self.run_source(lin, tp, ["ACME-4896: final measurement, then close"])
        self.assertEqual([w["source"] for w in out["waiting"]], ["notes"])

    def test_source_down_adds_nothing_and_keeps_hand_lines(self):
        class Boom(Fake):
            def snapshot(self):
                raise RuntimeError("down")
        out = self.run_source(Boom(None), Fake(prs(looked=MERGED_BOTH)), ["OPS-1: something"])
        self.assertEqual([w["source"] for w in out["waiting"]], ["notes"])
        out = self.run_source(None, None, ["OPS-1: something"])
        self.assertEqual([w["source"] for w in out["waiting"]], ["notes"])


class Sources(unittest.TestCase):
    def test_linear_attachments_parsed(self):
        doc = {"data": {"viewer": {"assignedIssues": {"nodes": [
            {"identifier": "ACME-4896", "title": "t", "priority": 3, "state": {"name": "In Review", "type": "started"},
             "attachments": {"nodes": [{"url": "https://github.com/acme-inc/Acme/pull/101"},
                                       {"url": "https://github.com/acme-inc/acme/pull/102/files"},
                                       {"url": "https://linear.app/x"}, {"url": None}, "junk"]}},
            {"identifier": "ACME-1", "title": "t", "priority": 3, "state": {"name": "Todo", "type": "unstarted"}}],
            "pageInfo": {"hasNextPage": False}}}, "teams": {"nodes": [{"key": "ACME"}]}}}
        issues, _, _ = tasks.parse_page(doc)
        self.assertEqual(issues[0]["prs"], [REF1, REF2])
        self.assertIsNone(issues[1]["prs"])
        self.assertIn("attachments(first: 25) { nodes { url } }", tasks.QUERY)

    def test_linear_saved_copy_without_prs_is_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "lin.json")
            import diskcache
            diskcache.save(path, {"v": 1, "ws": {"acme": {"wall": NOW - 60, "teams": ["ACME"], "issues": [
                {"identifier": "ACME-1", "title": "t", "state": "In Review", "priority": 3},
                {"identifier": "ACME-2", "title": "t", "state": "In Review", "priority": 3, "prs": [REF1]},
                {"identifier": "ACME-3", "title": "t", "state": "In Review", "priority": 3, "prs": ["bad ref"]}]}}})
            lin = tasks.Linear(get_key=lambda i: None, post=None, wall=lambda: NOW, cache_file=path, log=lambda x: 0)
            got = {i["identifier"]: i["prs"] for i in lin.snapshot()["ws"]["acme"]["issues"]}
            self.assertEqual(got, {"ACME-1": None, "ACME-2": [REF1], "ACME-3": None})

    def node(self, num, state, body="", branch=""):
        return {"number": num, "state": state, "isDraft": False, "mergedAt": None, "headRefName": branch,
                "body": body, "repository": {"name": "acme", "nameWithOwner": "acme-inc/acme"},
                "commits": {"nodes": []}}

    def test_closed_kept_aside_and_lookup(self):
        search = {"data": {"t0": {"nodes": []}, "t1": {"nodes": [
            self.node(101, "MERGED"), self.node(9, "CLOSED", branch="work/sam/acme-4896-x")]}}}
        lookup = {"data": {"l0": {"pullRequest": {"state": "MERGED"}}, "l1": None},
                  "errors": [{"type": "NOT_FOUND"}]}
        calls = []

        def run(args, timeout):
            calls.append(args[-1])
            if len(calls) == 1:
                return 0, json.dumps(search)
            return 1, json.dumps(lookup)          # gh exits 1 when one repository does not resolve
        t = derive.TicketPRs(run=run, clock=lambda: 1000.0, wall=lambda: NOW, log=lambda x: 0,
                             wanted=lambda: {REF1, REF2, "acme-inc/gone#5", "evil/x#1"})
        t.refresh()
        snap = t.snapshot()
        self.assertEqual([p["number"] for p in snap["prs"]], [101])
        self.assertEqual(snap["prs"][0]["owner_repo"], "acme-inc/acme")
        self.assertEqual([(p["number"], p["ids"]) for p in snap["closed"]], [(9, ["ACME-4896"])])
        # REF1 came from the search; only REF2 and acme-inc/gone#5 were looked up (never a foreign org)
        self.assertEqual(len(calls), 2)
        self.assertIn('repository(owner: "acme-inc", name: "acme") { pullRequest(number: 102)', calls[1])
        self.assertNotIn("evil", calls[1])
        self.assertEqual(snap["looked"], {"acme-inc/acme#102": "merged"})
        # PR #9 is closed unmerged: ignored since 2026-11-23, #101 and #102 merged -> the line
        self.assertEqual([w["id"] for w in inflight.auto_waiting(linear(issue()), snap)], ["ACME-4896"])
        self.assertEqual([w["id"] for w in inflight.auto_waiting(linear(issue("ACME-4897")), snap)], ["ACME-4897"])

    def test_lookup_failure_keeps_the_rest(self):
        search = {"data": {"t0": {"nodes": []}, "t1": {"nodes": []}}}
        logs = []
        calls = []

        def run(args, timeout):
            calls.append(1)
            return (0, json.dumps(search)) if len(calls) == 1 else (1, "")
        t = derive.TicketPRs(run=run, clock=lambda: 1000.0, wall=lambda: NOW, log=logs.append, wanted=lambda: {REF2})
        t.refresh()
        snap = t.snapshot()
        self.assertEqual(snap["status"], "ok")
        self.assertEqual(snap["looked"], {})
        self.assertEqual(logs, ["tasks: github linked PRs lookup failed (RuntimeError exit 1)"])
        self.assertEqual(inflight.auto_waiting(linear(issue()), snap), [])

    def test_saved_copy_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "prs.json")
            search = {"data": {"t0": {"nodes": []}, "t1": {"nodes": [self.node(101, "MERGED")]}}}
            lookup = {"data": {"l0": {"pullRequest": {"state": "MERGED"}}}}
            seq = [json.dumps(search), json.dumps(lookup)]
            t = derive.TicketPRs(run=lambda a, timeout: (0, seq.pop(0)), clock=lambda: 1000.0, wall=lambda: NOW,
                                 log=lambda x: 0, cache_file=path, wanted=lambda: {REF2})
            t.refresh()
            again = derive.TicketPRs(run=None, clock=lambda: 5000.0, wall=lambda: NOW + 60, log=lambda x: 0,
                                     cache_file=path)
            snap = again.snapshot()
            self.assertEqual(snap["status"], "stale")
            self.assertEqual(snap["closed"], [])
            self.assertEqual(snap["looked"], {REF2: "merged"})
            self.assertEqual([w["id"] for w in inflight.auto_waiting(linear(issue()), snap)], ["ACME-4896"])


if __name__ == "__main__":
    unittest.main()
