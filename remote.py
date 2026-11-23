"""The remote: a phone page that chooses what the kiosk's left panel shows, and holds the monitor on or off.

State lives in memory only (a server restart is "back to normal"); the one file is power.py's screen override.

Panels: "auto" (normal: in work hours the task list with the In flight panel under it, outside them the off-hours
view), "tasks" (the task list full size; the same list, the remote only lifts the hours) and "inflight" (the In
flight panel full size; likewise). A panel chosen from the remote holds until "back to normal", or until the screen
goes dark at the power schedule's off hour, whichever comes first.

Access (decided by gate(), fail closed):
  - the local listener (127.0.0.1) answers only peers 127.0.0.1, with Host 127.0.0.1:PORT or localhost:PORT;
  - the remote listener (settings `remote`: this box's Tailscale address, or `listen`) answers only peers in
    `remote.allowed_networks` (default the Tailscale range 100.64.0.0/10), with Host set to one of the listener's own
    names, and only the REMOTE_PATHS (no task list, no status);
  - anything else is 403. The Host check stops DNS rebinding.
Control POSTs need the header X-Kiosk-Remote: 1 and a JSON body (a form on another site cannot send either without
a CORS preflight, which this server never grants). Actions are rate limited per peer and logged as name and count.
"""
import ipaddress
import re
import threading
import time
from collections import Counter
from datetime import datetime, timedelta

import config
import power
import tasks

ALLOWED = tuple(ipaddress.ip_network(n) for n in config.get()["remote"]["allowed_networks"])
TAILNET = ipaddress.ip_network("100.64.0.0/10")
REMOTE_PATHS = {"/remote", "/static/remote.css", "/static/remote.js", "/api/remote/state", "/api/control"}
PANELS = ("auto", "tasks", "inflight")
CONTROL_HEADER = "X-Kiosk-Remote"
MAX_BODY = 4096
POWER = bool(config.get()["power"]["enabled"])


def gate(listener, peer, path, host, port, names=()):
    """0 when the request may be served, else the HTTP status to refuse it with (403)."""
    try:
        ip = ipaddress.ip_address(peer)
    except ValueError:
        return 403
    host = (host or "").strip().lower()
    if listener == "local":
        if str(ip) != "127.0.0.1":
            return 403
        if host not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return 403
        return 0
    if listener == "remote":
        if not any(ip.version == n.version and ip in n for n in ALLOWED):
            return 403
        if host not in {f"{n.lower()}:{port}" for n in names if n}:
            return 403
        return 0 if path in REMOTE_PATHS else 403
    return 403


def csrf_ok(headers, method):
    """POST /api/control: the custom header and a JSON content type; Origin, when sent, must be this host."""
    if method != "POST":
        return False
    if headers.get(CONTROL_HEADER) != "1":
        return False
    if not (headers.get("Content-Type") or "").lower().startswith("application/json"):
        return False
    origin = headers.get("Origin")
    if origin and origin.lower() != "http://" + (headers.get("Host") or "").lower():
        return False
    site = headers.get("Sec-Fetch-Site")
    if site and site not in ("same-origin", "none"):
        return False
    return True


class RateLimit:
    """A token bucket per peer: `burst` actions at once, then `rate` per second."""

    def __init__(self, rate=1.0, burst=20, clock=time.monotonic):
        self.rate, self.burst, self.clock = rate, burst, clock
        self._b = {}
        self._lock = threading.Lock()

    def allow(self, peer):
        with self._lock:
            now = self.clock()
            tokens, at = self._b.get(peer, (self.burst, now))
            tokens = min(self.burst, tokens + (now - at) * self.rate)
            if tokens < 1:
                self._b[peer] = (tokens, now)
                return False
            self._b[peer] = (tokens - 1, now)
            return True


class BadRequest(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


def last_night_start(now):
    """The most recent start of the power schedule's dark hours at or before `now`."""
    local = now.astimezone(power.TZ)
    b = local.replace(hour=power.OFF_FROM, minute=0, second=0, microsecond=0)
    return b if b <= local else b - timedelta(days=1)


def _clock(ts):
    return datetime.fromtimestamp(ts, power.TZ).strftime("%-I:%M %p")


def _str(v, n):
    return re.sub(r"\s+", " ", str(v or "")).strip()[:n]


class Remote:
    def __init__(self, clock=time.time, apply_power=None, override_path=None, log=None):
        self.clock = clock
        self.override_path = override_path
        self.apply_power = apply_power or (lambda: power.main([], self.override_path))
        self.log = log or (lambda line: print(line, flush=True))     # journal: action names and counts only
        self.lock = threading.RLock()
        self.counts = Counter()
        self.panel, self.panel_at = "auto", clock()
        self._out = (0.0, None)

    # ---------- the clock-driven part ----------
    def _tick(self):
        now = self.clock()
        if self.panel != "auto" and POWER:
            ns = last_night_start(datetime.fromtimestamp(now, power.TZ)).timestamp()
            if self.panel_at < ns <= now:
                self._normal()

    def _set_panel(self, p):
        self.panel, self.panel_at = p, self.clock()

    def _normal(self):
        self._set_panel("auto")

    # ---------- what the kiosk reads (every second) ----------
    def mode(self):
        with self.lock:
            self._tick()
            return {"panel": self.panel}

    def _forced(self, panel):
        with self.lock:
            self._tick()
            return self.panel == panel

    def tasks_forced(self):
        return self._forced("tasks")

    def inflight_forced(self):
        return self._forced("inflight")

    # ---------- what the remote reads ----------
    def _output(self):
        at, val = self._out
        if time.monotonic() - at > 5:
            outs = power.outputs()
            val = "unknown" if not outs else ("on" if any(outs.values()) else "off")
            self._out = (time.monotonic(), val)
        return val

    def screen_state(self):
        if not POWER:
            return None
        now = datetime.fromtimestamp(self.clock(), power.TZ)
        want, source = power.effective(now, self.override_path)
        nb = power.next_boundary(now)
        after = power.wanted(nb)
        if source == "override":
            nxt = f"back to the schedule at {_clock(nb.timestamp())} ({after})"
        else:
            nxt = f"{'dark' if after == 'off' else 'on'} at {_clock(nb.timestamp())}"
        return {"want": want, "source": source, "output": self._output(), "next": nxt}

    def state(self):
        with self.lock:
            self._tick()
            st = {"panel": self.panel}
            if self.panel == "auto":      # the same rule the server uses for /api/inflight
                st["showing"] = ("Work tasks and In flight"
                                 if tasks.shown(datetime.fromtimestamp(self.clock(), power.TZ)) else "Off hours")
            elif self.panel == "inflight":
                st["showing"] = "In flight (full size)"
            else:
                st["showing"] = "Work tasks (full size)"
        st["screen"] = self.screen_state()
        return st

    # ---------- actions ----------
    def act(self, action, args, peer=""):
        if not isinstance(action, str) or not re.fullmatch(r"[a-z]+\.[a-z]+", action):
            raise BadRequest(400, "bad action")
        fn = getattr(self, "_a_" + action.replace(".", "_"), None)
        if not fn:
            raise BadRequest(400, "unknown action")
        if not isinstance(args, dict):
            raise BadRequest(400, "bad args")
        result = fn(args) or {}
        self.counts[action] += 1
        self.log(f"remote: {action} (n={self.counts[action]})")
        return result

    # panels
    def _a_panel_set(self, a):
        p = a.get("panel")
        if p not in PANELS:
            raise BadRequest(400, "bad panel")
        with self.lock:
            if p == "auto":
                self._normal()
            else:
                self._set_panel(p)

    def _a_panel_normal(self, a):
        with self.lock:
            self._normal()

    # screen
    def _need_power(self):
        if not POWER:
            raise BadRequest(409, "screen power is not set up (settings: power.enabled)")

    def _apply(self):
        self._out = (0.0, None)
        threading.Thread(target=self.apply_power, daemon=True).start()

    def _a_screen_on(self, a):
        self._need_power()
        power.set_override("on", datetime.fromtimestamp(self.clock(), power.TZ), self.override_path)
        self._apply()

    def _a_screen_off(self, a):
        self._need_power()
        power.set_override("off", datetime.fromtimestamp(self.clock(), power.TZ), self.override_path)
        self._apply()

    def _a_screen_auto(self, a):
        self._need_power()
        power.clear_override(self.override_path)
        self._apply()
