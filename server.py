#!/usr/bin/python3 -I
"""The easel server: the page, its JSON, and the remote. Standard library only.

Two listeners on the same port (settings `server.port`):
  - `server.host` (127.0.0.1): everything below (the easel browser on this box);
  - with settings `remote.enabled`: this box's Tailscale address (`tailscale ip -4`, when `remote.tailscale`) or
    `remote.listen`, serving ONLY the remote (/remote, its CSS/JS, /api/remote/state, /api/control). If Tailscale is
    not up yet it retries every 30 s. Every request also passes remote.gate() (peer and Host).

  /                 web/index.html
  /static/<file>    web/<file>
  /api/status       the status strip and the scene (status.py, cached 10 s)
  /api/tasks        the work task list, work hours only (tasks.py, cached 10 s)
  /api/inflight     the In flight panel: waiting on you, running, just finished, open PRs (inflight.py, cached 10 s)
  /api/done         the Done this week box, work hours only (done.py, cached 10 s)
  /api/mode         what the left panel shows (remote.py), polled every second by the easel
  /remote, /api/remote/state, POST /api/control   the remote (local and remote listener)
Everything else is 404. Nothing is ever written to Linear or GitHub.
"""
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config   # noqa: E402
import agents    # noqa: E402
import derive   # noqa: E402
import diskcache  # noqa: E402
import done     # noqa: E402
import inflight # noqa: E402
import remote   # noqa: E402
import status   # noqa: E402
import tasks    # noqa: E402

_C = config.get()
WEB = os.path.join(HERE, "web")
HOST = _C["server"]["host"]
PORT = int(os.environ.get("EASEL_PORT") or _C["server"]["port"])
CACHE_S = 10
# a page build id: the page reloads itself when the server's own files change
BUILD = str(int(max(os.stat(os.path.join(d, f)).st_mtime
                    for d in (HERE, WEB) for f in os.listdir(d) if f.endswith((".py", ".html", ".css", ".js")))))
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
         ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml"}

CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; object-src 'none'; "
       "frame-ancestors 'none'; form-action 'none'; base-uri 'self'")

GITHUB = bool(_C["github"]["enabled"] and _C["github"]["orgs"])

_lock = threading.Lock()          # the status cache
_cache = {"at": 0.0, "body": b""}
_tasks_lock = threading.Lock()
_tasks_cache = {"at": 0.0, "body": b"", "force": None}
_inflight_lock = threading.Lock()
_inflight_cache = {"at": 0.0, "body": b"", "force": None}
# the last good Linear and GitHub answers are kept on disk (diskcache.py) and served, with their age, from the first
# request after a start until a fresh answer arrives (a restart never shows a short list)
_linear = tasks.Linear(cache_file=diskcache.path(tasks.LINEAR_CACHE))
# the owner's recent PRs with the ticket ids they name, plus the state of the PRs their In Review issues link in
# Linear (one TicketPRs for both panels: the task list's state lines and the automatic waiting line)
_ticket_prs = derive.TicketPRs(cache_file=diskcache.path("github-ticket-prs-last-good.json"),
                               wanted=lambda: inflight.wanted_refs(_linear.snapshot())) if GITHUB else None
# the notes file's IN FLIGHT block (W lines, hand R/F) + the hook's record of agents (agents.py) + the open PRs;
# work ticket prefixes = the Linear team keys; + the automatic "Waiting on you" line (inflight.auto_waiting)
_inflight_src = inflight.Source(
    github=inflight.GitHub(cache_file=diskcache.path("github-prs-last-good.json")) if GITHUB else None,
    agents=agents.Reader() if _C["agents"]["enabled"] else None, prefixes=_linear.team_keys,
    linear=_linear, ticket_prs=_ticket_prs)
# Linear + notes overrides; state lines derived from Linear, PRs and finished lines (derive.py); the waiting/running
# flags from the merged In flight block (tasks.apply_flight)
_tasks_src = tasks.Source(linear=_linear, flight=lambda: _inflight_src.flight(), prs=_ticket_prs)
# the Done this week box (done.py): merged PRs (its own search), Linear's completed issues (the same fetch), the notes
# file's Done lines; "can close" from the automatic waiting line's computation
_merged_prs = done.MergedPRs(cache_file=diskcache.path("github-merged-last-good.json")) if GITHUB else None
_done_src = done.Source(linear=_linear, merged=_merged_prs, ticket_prs=_ticket_prs)
_done_lock = threading.Lock()
_done_cache = {"at": 0.0, "body": b""}
REMOTE = remote.Remote()
_limit = remote.RateLimit()
_refused = {"n": 0}


def tasks_body():
    """The work task list (tasks.py): empty outside work hours unless the remote chose the Tasks panel (decided here
    from the server's own state, never from the request), cached 10 s."""
    force = REMOTE.tasks_forced()
    with _tasks_lock:
        if (time.monotonic() - _tasks_cache["at"] > CACHE_S or not _tasks_cache["body"]
                or _tasks_cache["force"] != force):
            try:
                data = tasks.payload(_tasks_src, force=force)
            except Exception:          # fail closed: nothing shown
                data = dict(tasks.EMPTY)
            _tasks_cache["body"] = json.dumps(data).encode()
            _tasks_cache["at"] = time.monotonic()
            _tasks_cache["force"] = force
        return _tasks_cache["body"]


def inflight_body():
    """The In flight panel (inflight.py): the same work hours as the task list unless the remote chose the In flight
    panel (decided here from the server's own state, never from the request), cached 10 s. Never raises: hidden at
    worst (fail closed)."""
    force = REMOTE.inflight_forced()
    with _inflight_lock:
        if (time.monotonic() - _inflight_cache["at"] > CACHE_S or not _inflight_cache["body"]
                or _inflight_cache["force"] != force):
            try:
                data = inflight.payload(_inflight_src, force=force)
            except Exception:
                data = dict(inflight.HIDDEN, now=int(time.time()))
            _inflight_cache["body"] = json.dumps(data).encode()
            _inflight_cache["at"] = time.monotonic()
            _inflight_cache["force"] = force
        return _inflight_cache["body"]


def done_body():
    """The Done this week box (done.py): work hours only, cached 10 s. Never raises: hidden at worst."""
    with _done_lock:
        if time.monotonic() - _done_cache["at"] > CACHE_S or not _done_cache["body"]:
            try:
                data = done.payload(_done_src)
            except Exception:
                data = dict(done.HIDDEN, now=int(time.time()))
            _done_cache["body"] = json.dumps(data).encode()
            _done_cache["at"] = time.monotonic()
        return _done_cache["body"]


def feeds():
    """The health tiles' facts (status.check_linear / check_github): the last good fetch of each slow source and
    whether its last attempt failed, from the objects in this process; None outside work hours (no tiles then)."""
    if not tasks.shown():
        return None
    out = {}
    try:
        snap = _linear.snapshot()
        out["linear"] = {"at": snap.get("at"), "missing": sorted(snap.get("missing") or ())}
    except Exception:
        out["linear"] = {"at": None, "missing": ["?"]}
    gh = []
    for src in (_inflight_src.github, _ticket_prs, _merged_prs):
        if src is None:
            continue
        try:
            sn = src.snapshot()
            # "failed": the last attempt of THIS run failed (a copy loaded from disk is "stale" until the first fetch,
            # which is not a failure)
            gh.append({"at": sn.get("at"), "status": sn.get("status"), "failed": getattr(src, "_ok", None) is False})
        except Exception:
            gh.append({"at": None, "status": "off"})
    out["github"] = gh
    return out


def status_body():
    with _lock:
        if time.monotonic() - _cache["at"] > CACHE_S or not _cache["body"]:
            try:
                data = status.collect(feeds=feeds())
            except Exception as e:     # never crash the page over one bad read
                data = {"overall": "warn", "problems": ["status: " + type(e).__name__], "checks": []}
            data["build"] = BUILD
            _cache["body"] = json.dumps(data).encode()
            _cache["at"] = time.monotonic()
        return _cache["body"]


class Handler(BaseHTTPRequestHandler):
    server_version = "easel"
    sys_version = ""

    def log_message(self, fmt, *args):   # quiet: the page polls every 15 s
        return

    def _gate(self, path):
        """True when the request may go on; otherwise a 403 has been sent."""
        srv = self.server
        code = remote.gate(getattr(srv, "kind", "local"), self.client_address[0], path, self.headers.get("Host"),
                           srv.server_address[1], getattr(srv, "names", ()))
        if code:
            _refused["n"] += 1
            if _refused["n"] in (1, 10, 100) or _refused["n"] % 1000 == 0:
                print(f"remote: refused (n={_refused['n']})", flush=True)
            self._send(code, b"not for you", "text/plain")
            return False
        return True

    def _json(self, code, data):
        self._send(code, json.dumps(data).encode(), "application/json")

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            n = -1
        if n < 0 or n > remote.MAX_BODY:
            return None
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def do_POST(self):
        path = unquote(urlsplit(self.path).path)
        if not self._gate(path):
            return
        if path == "/api/control":
            if not remote.csrf_ok(self.headers, "POST"):
                return self._send(403, b"missing remote header", "text/plain")
            if not _limit.allow(self.client_address[0]):
                return self._json(429, {"error": "slow down"})
            body = self._body()
            if body is None:
                return self._json(400, {"error": "bad body"})
            try:
                res = REMOTE.act(body.get("action"), body.get("args") or {})
            except remote.BadRequest as e:
                return self._json(e.code, {"error": e.msg})
            except Exception as e:     # never echo details (they could carry anything)
                print(f"remote: action failed ({type(e).__name__})", flush=True)
                return self._json(500, {"error": "failed"})
            return self._json(200, {"ok": True, **res, "state": REMOTE.state()})
        return self._send(405 if path == "/api/control" else 404, b"not here", "text/plain")

    def do_PUT(self):
        self._send(405, b"no", "text/plain")

    do_DELETE = do_PATCH = do_OPTIONS = do_PUT

    def _send(self, code, body, ctype, cache="no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        # the page may only load from this server (no IP-literal or any other off-box request)
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _file(self, path, cache="no-cache"):
        ext = os.path.splitext(path)[1].lower()
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            return self._send(404, b"not here", "text/plain")
        self._send(200, body, TYPES.get(ext, "application/octet-stream"), cache)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = unquote(urlsplit(self.path).path)
        if not self._gate(path):
            return
        if path == "/remote":
            return self._file(os.path.join(WEB, "remote.html"))
        if path == "/api/remote/state":
            return self._json(200, REMOTE.state())
        if path == "/api/mode":
            return self._json(200, REMOTE.mode())
        if path == "/" or path == "/index.html":
            return self._file(os.path.join(WEB, "index.html"))
        if path == "/api/status":
            return self._send(200, status_body(), "application/json")
        if path == "/api/tasks":
            return self._send(200, tasks_body(), "application/json")
        if path == "/api/inflight":
            return self._send(200, inflight_body(), "application/json")
        if path == "/api/done":
            return self._send(200, done_body(), "application/json")
        if path.startswith("/static/"):
            name = path[len("/static/"):]
            if "/" in name or name.startswith(".") or os.path.splitext(name)[1] not in (".css", ".js", ".svg"):
                return self._send(404, b"not here", "text/plain")
            return self._file(os.path.join(WEB, name))
        return self._send(404, b"not here", "text/plain")


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # a browser that drops a connection half way is normal; anything else keeps the traceback
        if isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, TimeoutError)):
            return
        super().handle_error(request, client_address)


def tailnet_identity():
    """(100.x address, [names]) of this box on the tailnet, or (None, []) when Tailscale is not up."""
    try:
        r = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=10)
        ip = r.stdout.split()[0] if r.returncode == 0 and r.stdout.split() else None
        s = subprocess.run(["tailscale", "status", "--self", "--json"], capture_output=True, text=True, timeout=10)
        me = json.loads(s.stdout).get("Self", {}) if s.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None, []
    import ipaddress
    try:
        if not ip or ipaddress.ip_address(ip) not in remote.TAILNET:
            return None, []
    except ValueError:
        return None, []
    names = [ip]
    dns = str(me.get("DNSName") or "").rstrip(".")
    if dns:
        names += [dns, dns.split(".")[0]]
    return ip, names


def remote_identity():
    """(address, [Host names]) the remote listener binds and answers to, or (None, [])."""
    rc = _C["remote"]
    if rc.get("listen"):
        ip, names = rc["listen"], [rc["listen"]]
    elif rc.get("tailscale", True):
        ip, names = tailnet_identity()
    else:
        return None, []
    return ip, (names + [str(h) for h in rc.get("hostnames") or ()]) if ip else []


def serve_remote():
    """Bind the remote on its address; wait for it (Tailscale not up yet) and retry every 30 s."""
    while True:
        ip, names = remote_identity()
        if ip:
            try:
                srv = Server((ip, PORT), Handler)
            except OSError as e:
                print(f"easel: remote bind failed ({type(e).__name__}); retrying", flush=True)
            else:
                srv.daemon_threads = True
                srv.kind, srv.names = "remote", names
                print(f"easel: remote on {ip}:{PORT}", flush=True)
                srv.serve_forever()
        time.sleep(30)


def main():
    srv = Server((HOST, PORT), Handler)
    srv.daemon_threads = True
    srv.kind, srv.names = "local", ()
    print(f"easel: serving on {HOST}:{PORT}", flush=True)
    if _C["remote"]["enabled"]:
        threading.Thread(target=serve_remote, daemon=True).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
