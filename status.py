"""The status strip: reads unit status, a few state files and file mtimes. Never runs a job, never reads content.

The tiles come from settings `status.tiles`, in order (easel.example.toml lists every type). collect() makes two
systemctl calls (user units) and a handful of stat() calls; server.py caches the result for 10 s.
Every check returns {"key", "label", "value", "state"} with state "ok", "warn", "bad" or "info".
Values are short words and numbers only: no message text, nothing private.
"""
import json
import os
import subprocess
import time
from datetime import datetime

import config
import sky

_C = config.get()
PROJECTS = os.path.expanduser(_C["session"]["projects_dir"])
PROPS = ["Id", "ActiveState", "SubState", "Result", "ExecMainStartTimestamp", "ExecMainExitTimestamp",
         "NextElapseUSecRealtime", "LoadState"]
BUSY_WINDOW = int(_C["session"]["window_s"])   # a transcript written this recently = the assistant is in a session
WORK_ONLY = ("linear", "github", "standup")      # tiles shown in work hours only

# The grid scene (web/grid.js): one node per thing on the box's rounds, lit only by the real state of the units
# that touch it (ActiveState and the last exit time from the same systemctl call; nothing else is read).
#   key, label, ((unit, after-glow seconds once a run has ended), ...)
GRID_NODES = [(n["key"], n.get("label") or n["key"],
               tuple((u["unit"], int(u.get("glow", 900))) for u in n.get("units") or ()))
              for n in (_C["scene"]["nodes"] if _C["scene"]["enabled"] else ())]
GRID_UNITS = [u for _, _, units in GRID_NODES for u, _ in units]
# the caption's words for each node: (while running, after a run)
GRID_WORDS = {n["key"]: (n.get("running") or "", n.get("after") or "")
              for n in (_C["scene"]["nodes"] if _C["scene"]["enabled"] else ())}


def _run(args, timeout=4):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def parse_show(text):
    """systemctl show output for several units (blank-line separated) -> {Id: {prop: value}}."""
    units = {}
    for block in text.strip().split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if props.get("Id"):
            units[props["Id"]] = props
    return units


def _ts(value):
    """'@1794900000' (systemctl --timestamp=unix) -> int seconds, or None."""
    value = (value or "").strip()
    if value.startswith("@"):
        try:
            return int(value[1:])
        except ValueError:
            return None
    # timer properties ignore --timestamp: 'Wed 2026-01-07 05:45:00 UTC' (read in the configured timezone: set the box clock to it)
    try:
        dt = datetime.strptime(" ".join(value.split()[:3]), "%a %Y-%m-%d %H:%M:%S")
        return int(dt.replace(tzinfo=sky.TZ).timestamp())
    except ValueError:
        return None


def _ago(seconds):
    seconds = max(0, int(seconds))
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400 * 2:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} days ago"


def _clock(ts):
    return datetime.fromtimestamp(ts, sky.TZ).strftime("%-I:%M %p") if ts else ""


def check_job(now, unit, key="backup", label="Backup", max_age_h=30):
    """A job that should run on a timer: running now, last run ok (and when), failed, or too long ago."""
    unit = unit or {}
    if unit.get("ActiveState") in ("activating", "active"):
        return {"key": key, "label": label, "value": "running now", "state": "info"}
    end = _ts(unit.get("ExecMainExitTimestamp"))
    if not end:
        return {"key": key, "label": label, "value": "no run yet", "state": "warn"}
    when = datetime.fromtimestamp(end, sky.TZ).strftime("%-I:%M %p")
    if unit.get("Result") != "success":
        return {"key": key, "label": label, "value": "FAILED " + when, "state": "bad"}
    if now - end > max_age_h * 3600:
        return {"key": key, "label": label, "value": "last ok " + _ago(now - end), "state": "bad"}
    return {"key": key, "label": label, "value": "ok " + when, "state": "ok"}


def check_failed(count):
    if count is None:
        return {"key": "failed", "label": "Jobs", "value": "unknown", "state": "warn"}
    if count == 0:
        return {"key": "failed", "label": "Jobs", "value": "none failed", "state": "ok"}
    return {"key": "failed", "label": "Jobs", "value": f"{count} failed", "state": "bad"}


def check_service(key, label, unit):
    a = (unit or {}).get("ActiveState")
    if a == "active":
        return {"key": key, "label": label, "value": "running", "state": "ok"}
    if a in ("activating", "reloading"):
        return {"key": key, "label": label, "value": "starting", "state": "warn"}
    return {"key": key, "label": label, "value": a or "unknown", "state": "bad"}


def check_disk(path="/"):
    try:
        s = os.statvfs(path)
    except OSError:
        return {"key": "disk", "label": "Disk", "value": "unknown", "state": "warn"}
    free = s.f_bavail * s.f_frsize
    pct = 100 * s.f_bavail / s.f_blocks if s.f_blocks else 0
    state = "ok" if pct >= 15 else ("warn" if pct >= 8 else "bad")
    return {"key": "disk", "label": "Disk free", "value": f"{free / 1e9:.0f} GB · {pct:.0f}%", "state": state}


def check_uptime(path="/proc/uptime"):
    try:
        with open(path) as f:
            secs = float(f.read().split()[0])
    except Exception:
        return {"key": "uptime", "label": "Up", "value": "unknown", "state": "info"}
    d, h = int(secs // 86400), int(secs % 86400 // 3600)
    m = int(secs % 3600 // 60)
    value = f"{d}d {h}h" if d else f"{h}h {m}m"
    return {"key": "uptime", "label": "Up", "value": value, "state": "info"}


def _headless_project(name):
    """Project folder names of runs nobody is sitting at: job run dirs and /tmp scratch dirs."""
    return name.endswith("-run") or name == "-tmp" or name.startswith("-tmp-")


def in_session(now, root=PROJECTS, window=BUSY_WINDOW):
    """True when a Claude Code session wrote its top-level transcript in the last `window` seconds. Reads mtimes only.

    Only top-level session transcripts count (subagents and background workflow agents write under a session's
    own subfolder). Project folders of headless runs (a working folder ending in "/run" -> "...-run") and of scratch
    folders under /tmp are skipped, so scheduled headless jobs do not make the screen say "in session"."""
    try:
        with os.scandir(root) as projects:
            dirs = [e.path for e in projects if e.is_dir(follow_symlinks=False) and not _headless_project(e.name)]
    except OSError:
        return False
    for d in dirs:
        try:
            with os.scandir(d) as it:
                for e in it:
                    if e.name.endswith(".jsonl") and e.is_file(follow_symlinks=False):
                        if now - e.stat(follow_symlinks=False).st_mtime < window:
                            return True
        except OSError:
            continue
    return False


def grid_nodes(now, units):
    """The grid scene's nodes: [{"key", "label", "state", "age"}] in GRID_NODES order.

    state: "failed" (a unit of the node is in systemd's failed state), "running" (active or activating), "ran"
    (a run ended within that unit's after-glow) or "quiet". age: seconds since the run started (running) or ended
    (ran), else None. Worst first: failed beats running beats ran. Unit states and times only, never content."""
    out = []
    for key, label, members in GRID_NODES:
        state, age = "quiet", None
        for unit, glow in members:
            u = units.get(unit) or {}
            a = u.get("ActiveState")
            if a == "failed":
                state, age = "failed", None
                break
            if a in ("active", "activating", "reloading", "deactivating"):
                start = _ts(u.get("ExecMainStartTimestamp"))
                if state != "running":
                    state, age = "running", (max(0, int(now - start)) if start else None)
                continue
            end = _ts(u.get("ExecMainExitTimestamp"))
            if state == "quiet" and end and 0 <= now - end < glow:
                state, age = "ran", int(now - end)
            elif state == "ran" and end and 0 <= now - end < min(glow, age or glow):
                age = int(now - end)
        words = GRID_WORDS.get(key, ("", ""))
        out.append({"key": key, "label": label, "state": state, "age": age, "running": words[0], "after": words[1]})
    return out


# --- the work-hours health tiles ------------------------------------------------------------------------------------
# Linear and GitHub: the last good fetch of the server's own slow sources (server.feeds(), objects in that process;
# nothing is fetched here). Standup: a standup job's own records in a folder (settings: the tile's "dir").
LINEAR_WARN_S, LINEAR_BAD_S = 15 * 60, 2 * 3600
GITHUB_WARN_S, GITHUB_BAD_S = 10 * 60, 3600


def _hm(ts):
    return datetime.fromtimestamp(ts, sky.TZ).strftime("%-I:%M") if ts else ""


def _since(ts, now):
    """"12:31" today, "Nov 22" before."""
    d, n = datetime.fromtimestamp(ts, sky.TZ), datetime.fromtimestamp(now, sky.TZ)
    return _hm(ts) if d.date() == n.date() else d.strftime("%b %-d")


def check_linear(now, feed):
    """Linear: green while the last good fetch (the oldest workspace's) is under 15 min old, amber under 2 h, red
    after that or when a workspace has never answered."""
    feed = feed or {}
    at, missing = feed.get("at"), feed.get("missing") or []
    if missing or not at:
        return {"key": "linear", "label": "Linear", "value": "no answer" + (" since " + _since(at, now) if at else ""),
                "state": "bad"}
    age = now - at
    if age < LINEAR_WARN_S:
        return {"key": "linear", "label": "Linear", "value": "ok " + _hm(at), "state": "ok"}
    return {"key": "linear", "label": "Linear", "value": "as of " + _since(at, now),
            "state": "warn" if age < LINEAR_BAD_S else "bad"}


def check_github(now, feeds):
    """GitHub: the oldest last good fetch of the easel's gh searches (open PRs, ticket PRs, merged PRs). Green under
    10 min, amber under 1 h, red after that or when the last attempt of any of them failed ("failing")."""
    live = [f for f in feeds or () if not (f.get("status") == "pending" and not f.get("at"))]
    if not live:
        return {"key": "github", "label": "GitHub", "value": "loading", "state": "info"}
    if any(not f.get("at") for f in live):
        return {"key": "github", "label": "GitHub", "value": "no answer", "state": "bad"}
    at = min(f["at"] for f in live)
    if any(f.get("failed") for f in live):
        return {"key": "github", "label": "GitHub", "value": "failing", "state": "bad"}
    age = now - at
    if age < GITHUB_WARN_S:
        return {"key": "github", "label": "GitHub", "value": "ok " + _hm(at), "state": "ok"}
    return {"key": "github", "label": "GitHub", "value": "as of " + _since(at, now),
            "state": "warn" if age < GITHUB_BAD_S else "bad"}


def _read_json(path):
    try:
        if os.path.getsize(path) > 64 * 1024:
            return None
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def check_standup(now_dt, unit=None, state_dir="", due=(9, 0), label="Standup"):
    """Standup: the day's record <dir>/posted-<YYYY-MM-DD>.json, {"state": ..., "ts": epoch} with state one of
    posting, posted-top, posted, failed, unclear, reply-unclear or skip; else <dir>/held-<YYYY-MM-DD>.json with
    {"state": "held"} (a draft held back for a person to post); else the clock: grey before `due` and on weekends.
    Reads states and times only, never the text."""
    key = "standup"
    if now_dt.weekday() >= 5:
        return {"key": key, "label": label, "value": "weekend", "state": "info"}
    d = now_dt.date()
    rec = _read_json(os.path.join(state_dir, "posted-%s.json" % d.isoformat()))
    active = (unit or {}).get("ActiveState") in ("activating", "active")
    if rec is not None:
        st = rec.get("state")
        if st == "posted":
            try:
                ts = int(float(rec.get("ts")))
            except (TypeError, ValueError):
                ts = None
            return {"key": key, "label": label, "value": "posted" + (" " + _hm(ts) if ts else ""), "state": "ok"}
        if st in ("posting", "posted-top"):
            if active:
                return {"key": key, "label": label, "value": "posting now", "state": "info"}
            return {"key": key, "label": label, "value": "thread missing" if st == "posted-top" else "unclear",
                    "state": "bad"}
        if st in ("failed", "unclear", "reply-unclear"):
            return {"key": key, "label": label, "value": "failed" if st == "failed" else "unclear", "state": "bad"}
        if st == "skip":
            return {"key": key, "label": label, "value": "skipped today", "state": "info"}
        return {"key": key, "label": label, "value": "odd record", "state": "warn"}
    held = _read_json(os.path.join(state_dir, "held-%s.json" % d.isoformat()))
    if held is not None and held.get("state") == "held":
        return {"key": key, "label": label, "value": "held back", "state": "warn"}
    if active:
        return {"key": key, "label": label, "value": "drafting now", "state": "info"}
    at = now_dt.replace(hour=due[0], minute=due[1], second=0, microsecond=0)
    if now_dt < at:
        return {"key": key, "label": label, "value": "at %d:%02d" % tuple(due), "state": "info"}
    return {"key": key, "label": label, "value": "no record", "state": "warn"}


def failed_count():
    out = _run(["systemctl", "--user", "show", "--property=NFailedUnits", "--value"])
    try:
        return int(out.strip())
    except ValueError:
        return None




def _units_for(tiles):
    out = []
    for tile in tiles:
        if tile.get("unit"):
            out.append(tile["unit"])
    return out


def tile(spec, now, now_dt, units, feeds, failed):
    """One configured tile -> a check dict, or None (a work-hours tile outside work hours, or an unknown type)."""
    kind = spec.get("type")
    label = spec.get("label")
    if kind == "failed":
        c = check_failed(failed)
    elif kind == "disk":
        c = check_disk(spec.get("path", "/"))
    elif kind == "uptime":
        if feeds is not None and spec.get("hide_in_work_hours", True):
            return None
        c = check_uptime()
    elif kind == "service":
        c = check_service(spec.get("key") or spec["unit"], label or spec["unit"], units.get(spec["unit"]))
    elif kind == "job":
        c = check_job(now, units.get(spec["unit"]), spec.get("key") or spec["unit"], label or spec["unit"],
                      float(spec.get("max_age_h", 30)))
    elif kind == "linear":
        if feeds is None:
            return None
        c = check_linear(now, feeds.get("linear"))
    elif kind == "github":
        if feeds is None:
            return None
        c = check_github(now, feeds.get("github"))
    elif kind == "standup":
        if feeds is None:
            return None
        c = check_standup(now_dt, units.get(spec.get("unit") or ""), os.path.expanduser(spec.get("dir", "")),
                          config.hm(spec.get("due", "09:00")), label or "Standup")
    else:
        return None
    if label and kind in ("failed", "disk", "uptime", "linear", "github"):
        c["label"] = label
    return c


def _dark(now_dt):
    """True during the power schedule's dark hours (the scene's figure then stands still)."""
    if not _C["power"]["enabled"]:
        return False
    a, b = int(_C["power"]["off_from"]), int(_C["power"]["off_until"])
    h = now_dt.hour
    return (a <= h < b) if a <= b else (h >= a or h < b)


def collect(now=None, feeds=None, tiles=None):
    """feeds: server.feeds() in work hours (the Linear, GitHub and Standup tiles then show, Up is left out so the
    strip keeps two rows), None outside them."""
    now = now or time.time()
    now_dt = datetime.fromtimestamp(now, sky.TZ)
    tiles = _C["status"]["tiles"] if tiles is None else tiles
    names = list(dict.fromkeys(_units_for(tiles) + GRID_UNITS))
    units = parse_show(_run(["systemctl", "--user", "--timestamp=unix", "show", "--property=" + ",".join(PROPS),
                             *names])) if names else {}
    failed = failed_count() if any(t.get("type") == "failed" for t in tiles) else None
    checks = [c for c in (tile(t, now, now_dt, units, feeds, failed) for t in tiles) if c]
    bad = [c for c in checks if c["state"] == "bad"]
    warn = [c for c in checks if c["state"] == "warn"]
    out = {
        "now": int(now),
        "overall": "bad" if bad else ("warn" if warn else "ok"),
        "problems": [f'{c["label"]}: {c["value"]}' for c in bad + warn],
        "checks": checks,
        "work": feeds is not None,
        "session": in_session(now) if _C["session"]["enabled"] else False,
        "grid": grid_nodes(now, units),
        "scene": bool(_C["scene"]["enabled"]),
        "dark": _dark(now_dt),
        "branding": {"assistant": _C["branding"]["assistant"], "board": _C["branding"]["board_label"],
                     "title": _C["branding"]["title"], "tz": _C["time"]["timezone"]},
    }
    if _C["sky"]["enabled"]:
        out["sky"] = sky.snapshot(now_dt)
    return out
