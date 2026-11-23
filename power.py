#!/usr/bin/python3 -I
"""Kiosk screen power (settings `power`): the monitor is OFF from `off_from` to `off_until` (hours, local time),
every day, and ON the rest of the day.

Clock-driven and idempotent: every run decides on/off from the local clock and switches the output only when it is
not already in that state, so a kiosk restart or a reboot in the night still ends dark and the morning always comes back on.
Run by kiosk-power.timer (user unit: every 3 minutes) as kiosk-power.service.

Remote override (remote.py, "Screen on now" / "off now"): <server.state_dir>/override.json holds
{"want": "on"|"off", "set_at": ts, "until": ts}, where `until` is the next schedule boundary after it
was set. While it is valid it wins over the clock; at the boundary it expires and the schedule resumes (the file is
removed on the next run). A missing, unreadable or odd file means "no override": the schedule decides.

The output name is read from `wlr-randr` at run time ; every output found is set. When the kiosk is
down (no Wayland socket) there is nothing to switch: exit 0 quietly. Each wlr-randr call is bounded by a timeout.

  power.py            set the screen for the current time
  power.py --print    say what it would do, change nothing
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402

TZ = ZoneInfo(config.get()["time"]["timezone"])
OFF_FROM = int(config.get()["power"]["off_from"])      # hours, local time: off at OFF_FROM, on at OFF_UNTIL
OFF_UNTIL = int(config.get()["power"]["off_until"])
TIMEOUT_S = 8
OVERRIDE = config.state_path("override.json")
MAX_OVERRIDE_S = 26 * 3600          # an override can never last longer than this, whatever the file says


def wanted(now):
    """'off' between 01:00 (inclusive) and 06:00 (exclusive) local time, else 'on'."""
    local = now.astimezone(TZ)
    return "off" if OFF_FROM <= local.hour < OFF_UNTIL else "on"


def next_boundary(now):
    """The next schedule change (01:00 or 06:00 local time) strictly after `now`, as an aware datetime."""
    local = now.astimezone(TZ)
    for days in (0, 1, 2):
        d = (local + timedelta(days=days)).date()
        for h in sorted((OFF_FROM, OFF_UNTIL)):
            b = datetime(d.year, d.month, d.day, h, tzinfo=TZ)
            if b > local:
                return b
    raise AssertionError("no boundary")      # unreachable


def read_override(now, path=None):
    """'on' or 'off' while a valid override is in force at `now`, else None (missing, expired or odd file)."""
    try:
        with open(path or OVERRIDE, encoding="utf-8") as f:
            data = json.load(f)
        want, until, set_at = data["want"], float(data["until"]), float(data["set_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    ts = now.timestamp()
    if want not in ("on", "off") or not (set_at - 60 <= ts < until) or until - set_at > MAX_OVERRIDE_S:
        return None
    return want


def effective(now, path=None):
    """(want, source): the override while it holds, else the clock. source is 'override' or 'schedule'."""
    o = read_override(now, path)
    return (o, "override") if o else (wanted(now), "schedule")


def set_override(want, now, path=None):
    """Hold the screen `want` ('on'/'off') until the next schedule boundary. Written by rename. Returns until (ts)."""
    if want not in ("on", "off"):
        raise ValueError("want")
    path = path or OVERRIDE
    until = next_boundary(now).timestamp()
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".override-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"want": want, "set_at": now.timestamp(), "until": until}, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return until


def clear_override(path=None):
    try:
        os.unlink(path or OVERRIDE)
    except FileNotFoundError:
        pass


def parse_outputs(text):
    """wlr-randr output -> {name: enabled bool}. Output headers start at column 0; 'Enabled: yes|no' is indented."""
    outs, cur = {}, None
    for line in text.splitlines():
        if line and not line[0].isspace():
            cur = line.split()[0]
            outs[cur] = None
        elif cur and line.strip().startswith("Enabled:"):
            outs[cur] = line.split(":", 1)[1].strip().lower() == "yes"
    return outs


def _env():
    run = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return dict(os.environ, XDG_RUNTIME_DIR=run, WAYLAND_DISPLAY=os.environ.get("WAYLAND_DISPLAY") or "wayland-0")


def outputs():
    """{name: enabled} from wlr-randr, or None when the kiosk is down or wlr-randr fails."""
    env = _env()
    if not os.path.exists(os.path.join(env["XDG_RUNTIME_DIR"], env["WAYLAND_DISPLAY"])):
        return None
    try:
        r = subprocess.run(["wlr-randr"], capture_output=True, text=True, timeout=TIMEOUT_S, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_outputs(r.stdout) if r.returncode == 0 else None


def main(argv, path=None):
    dry = "--print" in argv
    path = path or OVERRIDE
    env = _env()
    sock = os.path.join(env["XDG_RUNTIME_DIR"], env["WAYLAND_DISPLAY"])
    now = datetime.now(TZ)
    want, source = effective(now, path)
    if source == "schedule" and not dry and os.path.exists(path):
        clear_override(path)                 # expired or unreadable: the schedule is back in charge
    if dry and source == "override":
        print(f"screen power: remote override holds the screen {want} until "
              f"{next_boundary(now).strftime('%-I:%M %p')}")
    if not os.path.exists(sock):
        if dry:
            print(f"screen power: kiosk down (no Wayland socket); would want {want}")
        return 0
    r = subprocess.run(["wlr-randr"], capture_output=True, text=True, timeout=TIMEOUT_S, env=env)
    if r.returncode != 0:
        print(f"screen power: wlr-randr failed (rc {r.returncode})", file=sys.stderr)
        return 1
    outs = parse_outputs(r.stdout)
    if not outs:
        print("screen power: no outputs listed", file=sys.stderr)
        return 1
    rc = 0
    for name, enabled in outs.items():
        if enabled is (want == "on"):
            if dry:
                print(f"screen power: {name} already {want}")
            continue
        if dry:
            print(f"screen power: would turn {name} {want}")
            continue
        s = subprocess.run(["wlr-randr", "--output", name, "--" + want], capture_output=True, text=True,
                           timeout=TIMEOUT_S, env=env)
        if s.returncode != 0:
            print(f"screen power: could not turn {name} {want} (rc {s.returncode})", file=sys.stderr)
            rc = 1
        else:
            print(f"screen power: {name} {want}")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
