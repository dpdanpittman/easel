"""The easel's settings: one TOML file, read once at start (standard library only, `tomllib`).

Where the file is looked for, first match wins:
  1. the path in the environment variable EASEL_CONFIG;
  2. ./easel.toml (the folder the server is started from);
  3. ~/.config/easel/easel.toml.
No file: the built-in defaults below (a board with no Linear workspaces and no GitHub orgs: it runs, and shows
nothing but the status strip). `easel.example.toml` documents every key.

No secret is ever kept in the file. A Linear workspace names a COMMAND (`key_cmd`) that prints its read-only key
when the server needs it (a password manager's CLI, for example); the key lives in memory only. GitHub is read
through the `gh` CLI's own login; the easel never sees that token.

Every module reads its settings through `get()`; tests point EASEL_CONFIG at tests/easel.test.toml before importing
anything (tests/_env.py).
"""
import copy
import os
import re
import tomllib

DEFAULTS = {
    "branding": {
        # the page title and the verdict tile's label
        "title": "Easel",
        "board_label": "Board",
        # who keeps the In flight block and runs the background agents (the status tile and the scene's figure)
        "assistant": "Agent",
        # the person the board is for: a hand "WAITING ON: <who>" naming them (or one of the aliases) is theirs
        "owner": "",
        "owner_aliases": ["you", "me", "myself"],
    },
    "time": {
        "timezone": "UTC",
        "work_days": [0, 1, 2, 3, 4],          # Monday = 0
        "work_start": "08:00",
        "work_end": "16:00",
        # true: the work panels show at any hour (no "off the clock" view)
        "always_show": False,
    },
    "notes": {
        # the sketchbook: the append-only notes file holding the IN FLIGHT and the WORK TASKS blocks ("" = none)
        "path": "",
        # the heading of the work list block ("- 2026-01-05 09:00 WORK TASKS:"); "<NAME>'S WORK TASKS" also matches
        "tasks_heading": "WORK TASKS",
    },
    "linear": {
        # [{"name": "acme", "label": "Acme", "key_cmd": ["pass", "show", "linear/acme-read"]}]
        "workspaces": [],
    },
    "github": {
        "enabled": True,
        "gh": "gh",
        # whose PRs: a login, or "@me" (the gh login itself)
        "author": "@me",
        "orgs": [],
        # open PRs anyone wrote that you approved get a Waiting on you line once the approval is this many hours old
        # and the PR is still not merged (0 = off, and the search leaves the query)
        "approved_after_h": 48,
        # "repo#n" lines: PRs you approved that someone else drives; they show under Open PRs as "watching" instead
        # of in Waiting on you (a missing file mutes nothing)
        "approved_mute_file": "~/.config/easel/approved-mute.txt",
        # A green PR of yours is "merge it" only when its review is done: no conflict with its base branch and no
        # unresolved review thread, always. Optionally a review bot too (off while review_bot is ""): a pattern
        # matched against the reviewer's login; once that bot has reviewed a PR, its newest review of the head commit
        # must exist and, when review_score is set (a pattern whose first group is the score), score at least
        # review_score_min.
        "review_bot": "",
        "review_score": "",
        "review_score_min": 5,
    },
    "status": {
        # the strip's tiles, in order (see easel.example.toml for every type)
        "tiles": [{"type": "failed"}, {"type": "disk", "path": "/"}, {"type": "uptime"}],
    },
    "session": {
        # "in session" while a Claude Code transcript under this folder was written in the last window_s seconds
        "enabled": True,
        "projects_dir": "~/.claude/projects",
        "window_s": 120,
    },
    "agents": {
        # the hook-written record of background agents (scripts/agents-hook.py); "" = <state_dir>/agents.json
        "enabled": True,
        "state_file": "",
        # ticket prefixes that mark an agent as work while Linear has not answered (empty: only "[work]" tags)
        "fallback_prefixes": [],
    },
    "scene": {
        "enabled": True,
        # up to 9 nodes: {"key", "label", "units": [{"unit", "glow"}], "running": "...", "after": "..."}
        "nodes": [],
    },
    "sky": {
        # sunrise, sunset, daylight and the moon under the scene, computed locally
        "enabled": False,
        "latitude": 0.0,
        "longitude": 0.0,
    },
    "server": {
        "host": "127.0.0.1",
        "port": 8090,
        "state_dir": "~/.local/state/easel",
    },
    "remote": {
        # a phone page on a private network (a second listener that serves only the remote)
        "enabled": False,
        # listen on this box's Tailscale address (read from `tailscale ip -4`), or on `listen`
        "tailscale": True,
        "listen": "",
        "allowed_networks": ["100.64.0.0/10"],
        # extra Host names the remote answers to (the address and the Tailscale names are added by themselves)
        "hostnames": [],
    },
    "power": {
        # monitor off between these hours (local time) via wlr-randr; the remote can hold it on or off
        "enabled": False,
        "off_from": 1,
        "off_until": 6,
    },
}

_cfg = None


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def find():
    """The settings file in use, or None."""
    env = os.environ.get("EASEL_CONFIG")
    if env:
        return os.path.expanduser(env)
    for p in ("easel.toml", os.path.expanduser("~/.config/easel/easel.toml")):
        if os.path.isfile(p):
            return p
    return None


def load(path=None):
    """Read and check a settings file -> the full settings dict (defaults filled in). Raises ValueError on a bad
    file: an easel that cannot read its settings should say so at start, not run on half of them."""
    path = path or find()
    data = {}
    if path:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    cfg = _merge(DEFAULTS, data)
    check(cfg)
    return cfg


def check(cfg):
    for ws in cfg["linear"]["workspaces"]:
        if not isinstance(ws, dict) or not isinstance(ws.get("name"), str) or not ws["name"]:
            raise ValueError("linear.workspaces: each needs a name")
        cmd = ws.get("key_cmd")
        if not (isinstance(cmd, list) and cmd and all(isinstance(c, str) for c in cmd)):
            raise ValueError("linear.workspaces.%s: key_cmd must be a list of strings (a command)" % ws["name"])
        if "key" in ws or "api_key" in ws:
            raise ValueError("linear.workspaces.%s: never put a key in the file; use key_cmd" % ws["name"])
    for org in cfg["github"]["orgs"]:
        if not isinstance(org, str) or not org.replace("-", "").replace("_", "").replace(".", "").isalnum():
            raise ValueError("github.orgs: %r is not an organisation name" % (org,))
    h = cfg["github"]["approved_after_h"]
    if isinstance(h, bool) or not isinstance(h, (int, float)) or h < 0:
        raise ValueError("github.approved_after_h: a number of hours, 0 or more")
    g = cfg["github"]
    for k in ("review_bot", "review_score"):
        if not isinstance(g[k], str):
            raise ValueError("github.%s: a pattern (a string)" % k)
        try:
            pat = re.compile(g[k])
        except re.error as e:
            raise ValueError("github.%s: not a valid pattern (%s)" % (k, e))
        if k == "review_score" and g[k] and pat.groups < 1:
            raise ValueError("github.review_score: the pattern needs a group around the score")
    if g["review_score"] and not g["review_bot"]:
        raise ValueError("github.review_score needs github.review_bot (whose reviews carry the score)")
    if isinstance(g["review_score_min"], bool) or not isinstance(g["review_score_min"], int):
        raise ValueError("github.review_score_min: a whole number")
    for t in ("work_start", "work_end"):
        hm(cfg["time"][t])
    if len(cfg["scene"]["nodes"]) > 9:
        raise ValueError("scene.nodes: at most 9")


def hm(text):
    """"08:00" -> (8, 0)."""
    h, _, m = str(text).partition(":")
    h, m = int(h), int(m or 0)
    if not (0 <= h <= 24 and 0 <= m < 60):
        raise ValueError("time %r" % text)
    return h, m


def get():
    """The settings (loaded once)."""
    global _cfg
    if _cfg is None:
        _cfg = load()
    return _cfg


def reset(cfg=None):
    """Tests: replace the loaded settings (None: read the file again at the next get())."""
    global _cfg
    _cfg = cfg


def state_dir():
    return os.path.expanduser(get()["server"]["state_dir"])


def state_path(name):
    return os.path.join(state_dir(), name)


def workspaces():
    """((name, key_cmd), ...) of the configured Linear workspaces."""
    return tuple((w["name"], tuple(w["key_cmd"])) for w in get()["linear"]["workspaces"])


def workspace_labels():
    return {w["name"]: w.get("label") or w["name"] for w in get()["linear"]["workspaces"]}
