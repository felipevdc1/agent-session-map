"""The local server of the session map, and `open`.

`serve` listens on 127.0.0.1 only (port SESSION_MAP_PORT, default 9131) and answers:
- `GET /` -> the page (`page.html`, or the file SESSION_MAP_PAGE names);
- `GET /state` -> the snapshot (`snapshot.snapshot()`, imported, not a subprocess), kept for 1 s, with
  `ETag: "<version>"`; an equal `If-None-Match` -> 304. Between two snapshots `snapshot.merge_closed` keeps
  a vanished session on the map as `closed` for a while;
- `POST /focus` with `{"id": "<node id>"}` -> brings that session's tab to the front (Tern: `tern focus`;
  an orchestrated session: its adapter's `focus`);
- any other path -> 404; a `Host` other than `127.0.0.1:<port>` / `localhost:<port>` -> 403 (DNS rebinding).
It exits by itself once nobody asked for `/state` for SESSION_MAP_IDLE_S seconds (default 600).

`/focus` never trusts the client beyond the id: the node is looked up in the server's own latest snapshot,
the Tern pane comes from that snapshot, and an orchestrated session's handle from `snapshot.focus_target`.

`open` reuses a live server (it is ours when its `Server:` header says so) or starts one detached, then opens
the map: beside this pane inside Tern (`TERN_PANE` set), else in the default browser.

Nothing is logged: the detached server writes stdout and stderr to the null device.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from . import orchestrator, snapshot, tern

NAME = "claude-session-map"
DEFAULT_PORT = 9131
DEFAULT_IDLE_S = 600.0
CACHE_S = 1.0
BODY_MAX = 1024
CMD_TIMEOUT_S = 5
ENV_PORT = "SESSION_MAP_PORT"
ENV_IDLE = "SESSION_MAP_IDLE_S"
ENV_PAGE = "SESSION_MAP_PAGE"
_ID_RE = re.compile(r"^[a-z]:[A-Za-z0-9:_.-]{1,80}$")
_PANE_RE = re.compile(r"^\d{1,20}$")  # Tern block ids can be 13 digits or more
_PACKAGE_PARENT = str(Path(__file__).resolve().parent.parent)

NO_TARGET = {
    "background": "This session runs in the background, with no window to open.",
    "other": "This session is in another terminal, outside Tern and any orchestrator; the map cannot take you there.",
    "helper": "This is a helper: it runs inside the session that called it, with no tab of its own.",
    "next": "This step has not opened yet; it has no tab.",
    "tern": "It is in Tern, but the map does not know which tab.",
}


def port() -> int:
    raw = os.environ.get(ENV_PORT) or ""
    if not raw:
        return DEFAULT_PORT
    if not raw.isdigit() or not 0 < int(raw) < 65536:
        raise ValueError(f"{ENV_PORT} must be a port number")
    return int(raw)


def idle_s() -> float:
    raw = os.environ.get(ENV_IDLE) or ""
    if not raw:
        return DEFAULT_IDLE_S
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(f"{ENV_IDLE} must be a number of seconds") from None
    if value <= 0:
        raise ValueError(f"{ENV_IDLE} must be a number of seconds")
    return value


def page_path() -> Path:
    return Path(os.environ.get(ENV_PAGE) or snapshot.PAGE)


def _run(args: list) -> bool:
    """One command with list args; True when it exits 0. Its output is never shown nor kept."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=CMD_TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


# ---------------------------------------------------------------- the snapshot, cached

class State:
    """The latest snapshot, recomputed at most once per CACHE_S, one computation at a time."""

    def __init__(self):
        self.lock = threading.Lock()
        self.snap: Optional[dict] = None
        self.at = 0.0
        self.last_request = time.monotonic()

    def current(self) -> dict:
        with self.lock:
            now = time.monotonic()
            if self.snap is None or now - self.at >= CACHE_S:
                new = snapshot.snapshot()
                if self.snap is not None:
                    new = snapshot.merge_closed(self.snap, new, time.time())
                self.snap, self.at = new, now
            return self.snap

    def latest(self) -> Optional[dict]:
        with self.lock:
            return self.snap

    def node(self, nid: str) -> Optional[dict]:
        snap = self.latest() or self.current()
        for n in snap.get("nodes") or []:
            if isinstance(n, dict) and n.get("id") == nid:
                return n
        return None


# ---------------------------------------------------------------- bringing a session to the front (/focus)

def focus(state: State, nid: str) -> tuple:
    """(http status, {"ok", "message"}) for a jump to node `nid`; the target comes only from the server side."""
    node = state.node(nid)
    if node is None:
        return 404, {"ok": False, "message": "That session is no longer on the map."}
    kind = node.get("kind")
    if kind == "tern" and _PANE_RE.fullmatch(str(node.get("pane", ""))):
        if tern.focus(str(node["pane"])):
            return 200, {"ok": True, "message": "Done: the tab is in front, in Tern."}
        return 200, {"ok": False, "message": "Tern did not find that tab; it may be in another window."}
    if kind == "orchestrated":
        where = node.get("where") if isinstance(node.get("where"), str) and node.get("where") else "an orchestrator"
        target = snapshot.focus_target(int(nid[2:])) if nid.startswith("p:") and nid[2:].isdigit() else None
        if target is not None:
            adapter, handle = target
            if isinstance(handle, str) and orchestrator.HANDLE_RE.fullmatch(handle):
                name = getattr(adapter, "name", "") or where
                try:
                    ok = adapter.focus(handle) is True
                except Exception:  # noqa: BLE001 - an adapter failing is a plain "did not answer"
                    ok = False
                if ok:
                    return 200, {"ok": True, "message": f"Done: the tab is in front, in {name}."}
                return 200, {"ok": False, "message": f"{name} did not answer; the tab may have closed."}
        return 200, {"ok": False, "message": f"It is in {where}, but its tab was not found."}
    return 200, {"ok": False, "message": NO_TARGET.get(kind, "The map does not know how to take you to that session.")}


# ---------------------------------------------------------------- HTTP

def _unquote(etag: str) -> str:
    e = etag.strip()
    if e.startswith("W/"):
        e = e[2:]
    return e.strip('"')


class Handler(BaseHTTPRequestHandler):
    server_version = NAME
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def version_string(self) -> str:  # the `Server:` header `open` recognises us by
        return NAME

    def log_message(self, *args):  # nothing is logged
        pass

    def _send(self, status: int, body: bytes = b"", ctype: str = "application/json; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if status != 304:
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and status != 304 and self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data: dict):
        self._send(status, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _host_ok(self) -> bool:
        p = self.server.server_address[1]
        return self.headers.get("Host", "") in (f"127.0.0.1:{p}", f"localhost:{p}")

    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "host not allowed"})
        if self.path == "/":
            try:
                body = page_path().read_bytes()
            except OSError:
                return self._json(500, {"error": "the page could not be read"})
            return self._send(200, body, "text/html; charset=utf-8")
        if self.path == "/state":
            state = self.server.state
            state.last_request = time.monotonic()
            try:
                snap = state.current()
            except Exception as exc:  # noqa: BLE001 - the error type only, never its text
                return self._json(500, {"error": f"the snapshot failed ({type(exc).__name__})"})
            version = str(snap.get("version") or "")
            etag = f'"{version}"'
            asked = self.headers.get("If-None-Match")
            if version and asked is not None and version in {_unquote(x) for x in asked.split(",")}:
                return self._send(304, extra={"ETag": etag})
            return self._send(200, json.dumps(snap, ensure_ascii=False).encode("utf-8"), extra={"ETag": etag})
        return self._json(404, {"error": "not found"})

    def do_HEAD(self):
        return self._json(405, {"error": "method not allowed"})

    def do_POST(self):
        if not self._host_ok():
            return self._json(403, {"error": "host not allowed"})
        if self.path != "/focus":
            return self._json(404, {"error": "not found"})
        p = self.server.server_address[1]
        if self.headers.get("Origin") not in (f"http://127.0.0.1:{p}", f"http://localhost:{p}"):
            return self._json(403, {"error": "origin not allowed"})
        if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            return self._json(415, {"error": "JSON only"})
        try:
            size = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            size = -1
        if size <= 0 or size > BODY_MAX:
            return self._json(413 if size > BODY_MAX else 400, {"error": "bad request"})
        try:
            data = json.loads(self.rfile.read(size).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"error": "bad request"})
        nid = data.get("id") if isinstance(data, dict) else None
        if not isinstance(nid, str) or not _ID_RE.fullmatch(nid):
            return self._json(400, {"error": "bad request"})
        status, reply = focus(self.server.state, nid)
        return self._json(status, reply)

    def do_PUT(self):
        return self._json(405, {"error": "method not allowed"})

    do_DELETE = do_PATCH = do_OPTIONS = do_PUT


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # SO_REUSEADDR: rebinding over TIME_WAIT leftovers of a server that just exited; on macOS and Linux it does
    # not let a second live listener share the port (that would be SO_REUSEPORT)
    allow_reuse_address = True

    def __init__(self, port_: int, idle: float):
        super().__init__(("127.0.0.1", port_), Handler)
        self.state = State()
        self.idle = idle
        self._watch = threading.Thread(target=self._watch_idle, daemon=True)
        self._watch.start()

    def handle_error(self, request, client_address):
        """One short line on stderr: the error type only (no traceback, no path, no request content)."""
        exc = sys.exc_info()[1]
        kind = type(exc).__name__ if exc is not None else "error"
        print(f"{NAME}: a connection failed ({kind})", file=sys.stderr)

    def _watch_idle(self):
        step = max(0.05, min(5.0, self.idle / 4))
        while True:
            time.sleep(step)
            if time.monotonic() - self.state.last_request >= self.idle:
                self.shutdown()
                return


def serve(port_: Optional[int] = None) -> int:
    port_ = port() if port_ is None else port_
    try:
        srv = Server(port_, idle_s())
    except OSError:
        print(f"Port {port_} is already taken by another program; the map does not start.", file=sys.stderr)
        return 1
    try:
        srv.serve_forever(poll_interval=0.2)
    finally:
        srv.server_close()
    return 0


# ---------------------------------------------------------------- open

def probe(port_: int, timeout: float = 2.0) -> str:
    """'ours' (our server answers), 'other' (something else holds the port) or 'free'."""
    try:
        s = socket.create_connection(("127.0.0.1", port_), timeout=timeout)
    except OSError:
        return "free"
    head = b""
    try:
        s.settimeout(timeout)
        s.sendall(f"GET /state HTTP/1.0\r\nHost: 127.0.0.1:{port_}\r\nConnection: close\r\n\r\n".encode("ascii"))
        while b"\r\n\r\n" not in head and len(head) < 8192:
            chunk = s.recv(4096)
            if not chunk:
                break
            head += chunk
    except OSError:
        return "other"
    finally:
        s.close()
    lines = head.split(b"\r\n\r\n")[0].decode("latin-1").split("\r\n")
    ours = any(ln.lower().startswith("server:") and ln.split(":", 1)[1].strip().split(" ")[0] == NAME
               for ln in lines[1:])
    return "ours" if ours else "other"


def _start_detached() -> None:
    """`serve` in its own session, its output to the null device; it imports this same copy of the package."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (_PACKAGE_PARENT, env.get("PYTHONPATH", "")) if p)
    with open(os.devnull, "r+b") as null:
        subprocess.Popen([sys.executable, "-m", "session_map", "serve"], stdin=null, stdout=null, stderr=null,
                         env=env, start_new_session=True, close_fds=True)


def browser_command(url: str) -> list:
    return ["open", url] if sys.platform == "darwin" else ["xdg-open", url]


def open_map(port_: Optional[int] = None, wait_s: float = 10.0) -> int:
    port_ = port() if port_ is None else port_
    url = f"http://127.0.0.1:{port_}/"
    found = probe(port_)
    if found == "other":
        print(f"Port {port_} is taken by another program; the map was not opened.", file=sys.stderr)
        return 1
    if found == "free":
        _start_detached()
        limit = time.monotonic() + wait_s
        while found != "ours" and time.monotonic() < limit:
            time.sleep(0.2)
            found = probe(port_)
        if found != "ours":
            print("The map did not start in time; try again.", file=sys.stderr)
            return 1
    pane = os.environ.get("TERN_PANE", "")
    if _PANE_RE.fullmatch(pane):
        if tern.open_beside(pane, url):
            print("Map opened beside this pane, in Tern.")
            return 0
        print("Tern did not open the browser block; opening the default browser.", file=sys.stderr)
    if _run(browser_command(url)):
        print(f"Map opened in the default browser: {url}")
        return 0
    print(f"Could not open a browser; the map is at {url}", file=sys.stderr)
    return 1
