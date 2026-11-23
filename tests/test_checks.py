"""Offline tests for the newest-run CI rule (checks.py) and its use in derive.py and inflight.py.

Run: python3 -m unittest discover -s tests
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _env  # noqa: E402,F401  (settings for the tests)
sys.path.insert(0, os.path.dirname(HERE))
import checks    # noqa: E402
import derive    # noqa: E402
import inflight  # noqa: E402


def run(name, conclusion, started, completed=None, status="COMPLETED", workflow="CI"):
    return {"__typename": "CheckRun", "name": name, "status": status, "conclusion": conclusion,
            "startedAt": started, "completedAt": completed or (started if status == "COMPLETED" else None),
            "checkSuite": {"workflowRun": {"workflow": {"name": workflow}} if workflow else None}}


def ctx(context, state, created):
    return {"__typename": "StatusContext", "context": context, "state": state, "createdAt": created}


def roll(nodes, state="FAILURE", more=False):
    return {"state": state, "contexts": {"pageInfo": {"hasNextPage": more}, "nodes": nodes}}


# acme-inc/acme#4321: gitleaks cancelled 19:46, rerun green 19:47; the rollup said FAILURE
PR4321 = [run("gitleaks", "CANCELLED", "2026-11-23T19:46:13Z", "2026-11-23T19:46:17Z", workflow="Secret scan"),
          run("Build", "SUCCESS", "2026-11-23T19:47:00Z", "2026-11-23T19:55:51Z"),
          run("gitleaks", "SUCCESS", "2026-11-23T19:46:20Z", "2026-11-23T19:47:18Z", workflow="Secret scan"),
          run("Admin Composer E2E", "SKIPPED", "2026-11-23T19:54:58Z"),
          run("Proof", "SKIPPED", "2026-11-23T19:46:10Z", "2026-11-23T19:46:09Z", workflow=None)]


class Judge(unittest.TestCase):
    def test_superseded_cancelled_run_counts_as_passing(self):
        self.assertEqual(checks.judge(roll(PR4321)), "passing")

    def test_order_in_the_list_does_not_matter(self):
        self.assertEqual(checks.judge(roll(list(reversed(PR4321)))), "passing")

    def test_newer_failure_after_older_success_is_failing(self):
        nodes = [run("lint", "SUCCESS", "2026-11-23T10:00:00Z"), run("lint", "FAILURE", "2026-11-23T11:00:00Z")]
        self.assertEqual(checks.judge(roll(nodes, state="SUCCESS")), "failing")

    def test_same_name_in_another_workflow_is_another_check(self):
        nodes = [run("test", "FAILURE", "2026-11-23T10:00:00Z", workflow="A"),
                 run("test", "SUCCESS", "2026-11-23T11:00:00Z", workflow="B")]
        self.assertEqual(checks.judge(roll(nodes)), "failing")

    def test_each_failing_conclusion(self):
        for c in ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"):
            self.assertEqual(checks.judge(roll([run("x", c, "2026-11-23T10:00:00Z")])), "failing", c)

    def test_running_states(self):
        for st in ("QUEUED", "IN_PROGRESS", "PENDING"):
            n = run("x", None, "2026-11-23T10:00:00Z", status=st)
            self.assertEqual(checks.judge(roll([n], state="PENDING")), "running", st)
        for st in ("PENDING", "EXPECTED"):
            self.assertEqual(checks.judge(roll([ctx("ci/x", st, "2026-11-23T10:00:00Z")])), "running", st)

    def test_a_queued_rerun_without_times_is_the_newest(self):
        nodes = [run("x", "FAILURE", "2026-11-23T10:00:00Z"),
                 {"__typename": "CheckRun", "name": "x", "status": "QUEUED", "conclusion": None,
                  "startedAt": None, "completedAt": None, "checkSuite": {"workflowRun": {"workflow": {"name": "CI"}}}}]
        self.assertEqual(checks.judge(roll(nodes)), "running")

    def test_failing_beats_running(self):
        nodes = [run("a", "FAILURE", "2026-11-23T10:00:00Z"), run("b", None, "2026-11-23T10:00:00Z", status="QUEUED")]
        self.assertEqual(checks.judge(roll(nodes)), "failing")

    def test_neutral_and_skipped_do_not_fail(self):
        nodes = [run("a", "NEUTRAL", "2026-11-23T10:00:00Z"), run("b", "SKIPPED", "2026-11-23T10:00:00Z"),
                 run("c", "SUCCESS", "2026-11-23T10:00:00Z")]
        self.assertEqual(checks.judge(roll(nodes)), "passing")

    def test_status_contexts_newest_by_created(self):
        nodes = [ctx("ci/legacy", "FAILURE", "2026-11-23T10:00:00Z"), ctx("ci/legacy", "SUCCESS", "2026-11-23T10:05:00Z")]
        self.assertEqual(checks.judge(roll(nodes)), "passing")
        self.assertEqual(checks.judge(roll(list(reversed(nodes)))), "passing")

    def test_no_runs_is_none(self):
        self.assertEqual(checks.judge(roll([], state=None)), "none")
        self.assertEqual(checks.judge(None), "none")

    def test_old_shape_or_more_pages_falls_back_to_the_rollup_state(self):
        self.assertEqual(checks.judge({"state": "FAILURE"}), "failing")
        self.assertEqual(checks.judge(roll(PR4321, state="FAILURE", more=True)), "failing")
        self.assertEqual(checks.judge({"state": "SUCCESS"}), "passing")

    def test_odd_nodes_are_skipped(self):
        nodes = [None, 5, {"__typename": "CheckRun"}, {"__typename": "Other"}, run("ok", "SUCCESS", "2026-11-23T10:00:00Z")]
        self.assertEqual(checks.judge(roll(nodes)), "passing")
        self.assertEqual(checks.judge({"contexts": "x", "state": 3}), "none")


class Wired(unittest.TestCase):
    """The query asks for the runs, and both parsers use the rule."""

    def commits(self, nodes, state="FAILURE"):
        return {"nodes": [{"commit": {"statusCheckRollup": roll(nodes, state)}}]}

    def test_queries_ask_for_the_runs(self):
        from datetime import datetime, timezone
        for q in (inflight.QUERY, derive.query(datetime(2026, 11, 23, tzinfo=timezone.utc))):
            for want in ("contexts(first: 100)", "startedAt", "completedAt", "workflow { name }", "createdAt"):
                self.assertIn(want, q)

    def test_inflight_green_pr_waits_on_dan(self):
        node = {"number": 4321, "title": "fix", "isDraft": False, "reviewDecision": None,
                "repository": {"name": "acme", "nameWithOwner": "acme-inc/acme"}, "commits": self.commits(PR4321)}
        prs = inflight.parse_prs({"data": {"o0": {"nodes": []}, "o1": {"nodes": [node]}}})
        self.assertEqual(prs[0]["ci"], "passing")
        groups = inflight.merge({"waiting": [], "running": [], "finished": [], "counts": {}},
                                {"prs": prs})
        self.assertTrue(groups["waiting"][0]["text"].startswith("merge acme#4321"))
        self.assertEqual(groups["prs"], [])

    def test_derive_state_line_says_passing(self):
        node = {"number": 4321, "state": "OPEN", "isDraft": False, "mergedAt": None, "headRefName": "acme-1-x",
                "body": "Refs ACME-1", "repository": {"name": "acme", "nameWithOwner": "acme-inc/acme"},
                "commits": self.commits(PR4321)}
        prs = derive.parse(json.loads(json.dumps({"data": {"t0": {"nodes": []}, "t1": {"nodes": [node]}}})))
        self.assertEqual(derive.pr_text(prs, 0), "#4321 checks passing")

    def test_derive_newer_failure(self):
        nodes = [run("lint", "SUCCESS", "2026-11-23T10:00:00Z"), run("lint", "FAILURE", "2026-11-23T11:00:00Z")]
        node = {"number": 9, "state": "OPEN", "isDraft": False, "mergedAt": None, "headRefName": "x", "body": "Refs ACME-1",
                "repository": {"name": "acme", "nameWithOwner": "acme-inc/acme"}, "commits": self.commits(nodes, "SUCCESS")}
        prs = derive.parse({"data": {"t0": {"nodes": []}, "t1": {"nodes": [node]}}})
        self.assertEqual(prs[0]["ci"], "failing")


if __name__ == "__main__":
    unittest.main()
