"""The newest-run CI rule: one verdict for a PR's checks, from its last commit's statusCheckRollup.

GitHub keeps old check runs in the rollup after a rerun or a new push (often CANCELLED), and the rollup's own `state`
counts them: acme-inc/acme#7212 said FAILURE because a gitleaks run was cancelled at 19:46 and rerun green at 19:47.
So the easel never trusts the rollup state when it has the runs themselves:

  1. keep only the NEWEST run of each check, keyed by workflow name + check name (a CheckRun) or by context (a
     commit StatusContext); newest = the latest start (startedAt, else completedAt; a status context's createdAt);
     a run that has not started yet (queued, no times) counts as the newest of its key;
  2. judge what is left: failing when any is FAILURE, ERROR, TIMED_OUT, CANCELLED, ACTION_REQUIRED or STARTUP_FAILURE;
     else running when any is PENDING, EXPECTED, QUEUED, IN_PROGRESS or not completed; else passing (SUCCESS; NEUTRAL,
     SKIPPED and STALE never fail). No runs at all: "none".

When the runs are not in the answer (an older query shape) or there are more than one page of them, the rollup's own
state is used, as before. Everything is untrusted input: odd nodes are skipped, never raised on.
"""

# the GraphQL selection for a commit's rollup (read-only); used by derive.py and inflight.py
ROLLUP = ("statusCheckRollup { state contexts(first: 100) { pageInfo { hasNextPage } nodes { __typename "
          "... on CheckRun { name status conclusion startedAt completedAt "
          "checkSuite { workflowRun { workflow { name } } } } "
          "... on StatusContext { context state createdAt } } } }")

ROLLUP_STATE = {"SUCCESS": "passing", "FAILURE": "failing", "ERROR": "failing", "PENDING": "running",
                "EXPECTED": "running"}
FAILING = {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
RUNNING = {"PENDING", "EXPECTED", "QUEUED", "IN_PROGRESS", "WAITING", "REQUESTED"}
_LATEST = "￿"               # sorts after any ISO time: a run with no time yet is the newest of its key


def _s(v):
    return v if isinstance(v, str) else ""


def _run_of(node):
    """One rollup node -> (key, when, outcome) with outcome "failing"|"running"|"passing"|"neutral", or None."""
    if not isinstance(node, dict):
        return None
    kind = node.get("__typename")
    if kind == "CheckRun":
        name = _s(node.get("name"))
        if not name:
            return None
        wf = ""
        try:
            wf = _s(node["checkSuite"]["workflowRun"]["workflow"]["name"])
        except (KeyError, TypeError):
            wf = ""
        status, conclusion = _s(node.get("status")).upper(), _s(node.get("conclusion")).upper()
        started, completed = _s(node.get("startedAt")), _s(node.get("completedAt"))
        when = (started or completed or _LATEST, completed or _LATEST)
        if status != "COMPLETED" or not conclusion:
            outcome = "running"
        elif conclusion in FAILING:
            outcome = "failing"
        elif conclusion == "SUCCESS":
            outcome = "passing"
        else:                        # NEUTRAL, SKIPPED, STALE: never a failure
            outcome = "neutral"
        return ("run", wf, name), when, outcome
    if kind == "StatusContext":
        ctx = _s(node.get("context"))
        if not ctx:
            return None
        state = _s(node.get("state")).upper()
        created = _s(node.get("createdAt")) or _LATEST
        outcome = ("failing" if state in FAILING else "running" if state in RUNNING or not state
                   else "passing" if state == "SUCCESS" else "neutral")
        return ("status", "", ctx), (created, created), outcome
    return None


def newest_runs(nodes):
    """{key: outcome} keeping only the newest run of each check (ties: the later node in the list)."""
    best = {}
    for node in nodes or ():
        r = _run_of(node)
        if r is None:
            continue
        key, when, outcome = r
        if key not in best or when >= best[key][0]:
            best[key] = (when, outcome)
    return {k: v[1] for k, v in best.items()}


def judge(roll):
    """A statusCheckRollup dict (or None) -> "passing"|"failing"|"running"|"none". Never raises."""
    try:
        if not isinstance(roll, dict):
            return "none"
        ctx = roll.get("contexts")
        nodes = ctx.get("nodes") if isinstance(ctx, dict) else None
        more = isinstance(ctx, dict) and (ctx.get("pageInfo") or {}).get("hasNextPage") is True
        if not isinstance(nodes, list) or more:
            return ROLLUP_STATE.get(roll.get("state"), "none")
        outcomes = set(newest_runs(nodes).values())
        if not outcomes:
            return ROLLUP_STATE.get(roll.get("state"), "none") if not nodes else "none"
        if "failing" in outcomes:
            return "failing"
        if "running" in outcomes:
            return "running"
        return "passing"
    except Exception:
        return "none"


def judge_commits(commits):
    """A PR's `commits(last: 1)` connection -> the verdict of its last commit's rollup."""
    nodes = (commits or {}).get("nodes") if isinstance(commits, dict) else None
    if not nodes:
        return "none"
    last = nodes[-1] if isinstance(nodes[-1], dict) else {}
    return judge((last.get("commit") or {}).get("statusCheckRollup") if isinstance(last.get("commit"), dict) else None)
