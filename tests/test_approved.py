"""Offline tests for the "approved by you, not merged" Waiting line: inflight.parse_approved, inflight.auto_approved,
the GitHub fetch and its disk cache, and the /api/inflight merge.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _env  # noqa: E402,F401  (settings for the tests)
sys.path.insert(0, os.path.dirname(HERE))
import inflight  # noqa: E402
import tasks     # noqa: E402

TZ = tasks.TZ
NOW_DT = datetime(2026, 11, 24, 10, 0, tzinfo=TZ)
NOW = NOW_DT.timestamp()
DAY = 86400


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def node(number=88, reviews=(("APPROVED", NOW - 11 * DAY),), draft=False, state="OPEN", repo="api",
         owner="acme-inc", title="Rate limits on the public API (ACME-198)", body="Refs ACME-198",
         branch="work/acme-198-limits", merge_state="BLOCKED", mergeable="MERGEABLE", ci="SUCCESS", author=None):
    roll = None if ci is None else {"state": ci}
    return {"number": number, "title": title, "body": body, "headRefName": branch, "isDraft": draft, "state": state,
            "mergeable": mergeable, "mergeStateStatus": merge_state,
            "repository": {"name": repo, "nameWithOwner": "%s/%s" % (owner, repo)},
            "reviews": {"nodes": [dict({"state": s, "submittedAt": iso(t)},
                                       **({"author": {"login": author}} if author else {})) for s, t in reviews]},
            "commits": {"nodes": [{"commit": {"statusCheckRollup": roll}}]}}


def doc(*approved, own=(), viewer=None):
    d = {"data": {"o0": {"nodes": list(own)}, "o1": {"nodes": []}, "a0": {"nodes": list(approved)}}}
    if viewer:
        d["data"]["viewer"] = {"login": viewer}
    return d


def snap(*nodes, status="ok", own=()):
    return {"prs": list(own), "approved": inflight.parse_approved(doc(*nodes)), "status": status, "at": int(NOW)}


def lines(gh, hand=(), prefixes=("ACME", "OPS")):
    return inflight.auto_approved(gh, hand, set(prefixes), NOW)


class Rule(unittest.TestCase):
    def test_approved_over_48h_shows(self):
        out = lines(snap(node()))
        self.assertEqual(len(out), 1)
        w = out[0]
        self.assertEqual((w["source"], w["id"], w["ci"]), ("auto", "ACME-198", "passing"))
        self.assertTrue(w["text"].startswith("ACME-198 #88 api: approved 11d ago, not merged · blocked"), w["text"])
        self.assertLessEqual(len(w["text"]), inflight.TEXT_MAX)
        # the task list attaches it by the leading id
        self.assertIn("ACME-198", tasks.ticket_ids(w["text"]))

    def test_under_48h_does_not_show(self):
        self.assertEqual(lines(snap(node(reviews=(("APPROVED", NOW - 47 * 3600),)))), [])
        self.assertEqual(len(lines(snap(node(reviews=(("APPROVED", NOW - 49 * 3600),))))), 1)

    def test_threshold_comes_from_settings_and_zero_is_off(self):
        old = inflight.APPROVED_AFTER_S
        try:
            inflight.APPROVED_AFTER_S = 24 * 3600
            self.assertEqual(len(lines(snap(node(reviews=(("APPROVED", NOW - 25 * 3600),))))), 1)
            inflight.APPROVED_AFTER_S = 0
            self.assertEqual(lines(snap(node())), [])
        finally:
            inflight.APPROVED_AFTER_S = old

    def test_later_changes_requested_hides_it(self):
        reviews = (("APPROVED", NOW - 5 * DAY), ("CHANGES_REQUESTED", NOW - 4 * DAY))
        self.assertEqual(lines(snap(node(reviews=reviews))), [])
        # a dismissed approval hides it too; a later comment does not
        self.assertEqual(lines(snap(node(reviews=(("APPROVED", NOW - 5 * DAY), ("DISMISSED", NOW - 4 * DAY))))), [])
        out = lines(snap(node(reviews=(("APPROVED", NOW - 5 * DAY), ("COMMENTED", NOW - 1 * DAY)))))
        self.assertEqual(len(out), 1)
        self.assertIn("approved 5d ago", out[0]["text"])
        # changes requested first, then approved: shows
        reviews = (("CHANGES_REQUESTED", NOW - 11 * DAY - 3600), ("APPROVED", NOW - 11 * DAY))
        self.assertEqual(len(lines(snap(node(reviews=reviews)))), 1)

    def test_draft_merged_closed_do_not_show(self):
        self.assertEqual(lines(snap(node(draft=True))), [])
        self.assertEqual(lines(snap(node(state="MERGED"))), [])
        self.assertEqual(lines(snap(node(state="CLOSED"))), [])

    def test_hand_w_line_wins(self):
        gh = snap(node())
        self.assertEqual(lines(gh, ["ACME-198: ask Priya about the limits"]), [])
        self.assertEqual(lines(gh, ["merge acme-inc/api#88 after the owner looks"]), [])
        self.assertEqual(lines(gh, ["api#88 needs a rebase"]), [])
        self.assertEqual(len(lines(gh, ["OPS-1: something else", "api#89 other"])), 1)

    def test_own_pr_is_left_out(self):
        own = [{"repo": "api", "number": 88, "title": "t", "ci": "passing", "review": ""}]
        self.assertEqual(lines(snap(node(), own=own)), [])

    def test_github_failure_shows_nothing(self):
        for status in ("off", "pending"):
            self.assertEqual(lines(snap(node(), status=status)), [], status)
        self.assertEqual(lines(None), [])
        self.assertEqual(lines({"prs": [], "approved": None, "status": "ok", "at": int(NOW)}), [])
        # the stale last-good copy is still used (shown with its age like the PR lists)
        self.assertEqual(len(lines(snap(node(), status="stale"))), 1)

    def test_no_ticket_starts_with_number_and_merge_states(self):
        n = node(title="chore: bump deps", body="", branch="bump", merge_state="BEHIND", ci="FAILURE")
        w = lines(snap(n))[0]
        self.assertTrue(w["text"].startswith("#88 api: approved 11d ago, not merged · behind"), w["text"])
        self.assertEqual((w["id"], w["ci"]), ("api#88", "failing"))
        w = lines(snap(node(mergeable="CONFLICTING", merge_state="DIRTY")))[0]
        self.assertIn("· conflicting", w["text"])
        w = lines(snap(node(merge_state="CLEAN")))[0]
        self.assertIn("· clean", w["text"])

    def test_non_team_ids_are_dropped(self):
        n = node(title="Use UTF-8 everywhere", body="", branch="work/bot/guards-20261112T1")
        w = lines(snap(n))[0]
        self.assertTrue(w["text"].startswith("#88 "), w["text"])

    def test_oldest_first_and_foreign_org_ignored(self):
        a = node(number=1, reviews=(("APPROVED", NOW - 3 * DAY),), title="a", body="", branch="a")
        b = node(number=2, reviews=(("APPROVED", NOW - 9 * DAY),), title="b", body="", branch="b")
        c = node(number=3, owner="someone-else", title="c", body="", branch="c")
        self.assertEqual([w["id"] for w in lines(snap(a, b, c))], ["api#2", "api#1"])

    def test_missing_a0_part_is_unknown(self):
        self.assertIsNone(inflight.parse_approved({"data": {"o0": {"nodes": []}, "o1": {"nodes": []}}}))
        self.assertIsNone(inflight.parse_approved({"data": {"a0": None}}))


class AuthorMe(unittest.TestCase):
    """author = "@me": the reviews are not filtered in the query, so only the viewer's own count."""

    def setUp(self):
        self.old = inflight._ME
        inflight._ME = True

    def tearDown(self):
        inflight._ME = self.old

    def test_only_the_viewers_reviews_count(self):
        mine = node(author="octocat")
        theirs = node(number=89, author="someone")
        out = inflight.parse_approved(doc(mine, theirs, viewer="OctoCat"))
        self.assertEqual([p["number"] for p in out], [88])
        # no viewer login in the answer: unknown, no lines
        self.assertIsNone(inflight.parse_approved(doc(mine)))


class Fetch(unittest.TestCase):
    def test_query_has_the_search_and_cache_keeps_approved(self):
        self.assertIn('a0: search(query: "is:pr is:open reviewed-by:octocat org:globex-labs org:acme-inc"',
                      inflight.QUERY)
        self.assertIn('reviews(author: "octocat"', inflight.QUERY)
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, "gh.json")
            g = inflight.GitHub(run=lambda a, t: (0, json.dumps(doc(node()))), wall=lambda: NOW,
                                log=lambda line: None, cache_file=f)
            g.refresh()
            self.assertEqual([p["number"] for p in g.snapshot()["approved"]], [88])
            g2 = inflight.GitHub(run=lambda a, t: (1, ""), wall=lambda: NOW + 60, log=lambda line: None,
                                 cache_file=f)
            s2 = g2.snapshot()
            self.assertEqual((s2["status"], [p["number"] for p in s2["approved"]]), ("stale", [88]))

    def test_failed_fetch_gives_no_approved(self):
        g = inflight.GitHub(run=lambda a, t: (1, ""), log=lambda line: None)
        g.refresh()
        s = g.snapshot()
        self.assertEqual((s["status"], s["approved"]), ("off", None))
        self.assertEqual(lines(s), [])


class Fake:
    def __init__(self, snapshot):
        self.snap = snapshot

    def maybe_refresh(self):
        pass

    def snapshot(self):
        return self.snap


class SourceWiring(unittest.TestCase):
    def current(self, gh, w_lines=()):
        with tempfile.TemporaryDirectory() as d:
            notes = os.path.join(d, "notes.md")
            with open(notes, "w") as f:
                f.write("- 2026-11-24 09:55 IN FLIGHT (current):\n" + "".join("  W. %s\n" % w for w in w_lines))
            src = inflight.Source(notes=notes, github=gh, now=lambda: NOW_DT, prefixes=lambda: {"ACME"})
            return src.current()

    def test_line_in_payload_after_hand_lines(self):
        out = self.current(Fake(snap(node())), ["OPS-1: something"])
        self.assertEqual([w["source"] for w in out["waiting"]], ["notes", "auto"])
        self.assertEqual(out["waiting"][1]["ci"], "passing")
        self.assertEqual(out["counts"]["waiting"], 2)
        out = self.current(Fake(snap(node())), ["ACME-198: talk to the owner"])
        self.assertEqual([w["source"] for w in out["waiting"]], ["notes"])

    def test_github_down_keeps_hand_lines_only(self):
        class Boom(Fake):
            def snapshot(self):
                raise RuntimeError("down")
        out = self.current(Boom(None), ["OPS-1: something"])
        self.assertEqual([w["source"] for w in out["waiting"]], ["notes"])
        out = self.current(Fake(snap(node(), status="off")), [])
        self.assertEqual(out["waiting"], [])


if __name__ == "__main__":
    unittest.main()
