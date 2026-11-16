"""Tests for server.py and cli.py: the routes, the cache with ETag/304, the Host/Origin walls, the idle exit,
`open`, what /state never carries and the jump to a session (/focus).

Each test starts the real server in-process on a free port; `snapshot.snapshot` is replaced by a fake
snapshot (except one test over this machine, which reads the live sessions and checks only counts).
Programs (tern, the browser, the detached serve) are mocked; nothing is focused or opened.
"""

from __future__ import annotations

import contextlib
import http.client
import io
import json
import os
import re
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from session_map import cli, server, snapshot, tern  # noqa: E402

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_real_snapshot = snapshot.snapshot  # captured at import, before any patch
FAKE_TERN = "/fake/bin/tern"
HOME_PREFIX = "/" + "Users/"  # built from two pieces so the fixture itself is not a literal home path


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def fake_snapshot(version="abc123", title="api · review"):
    return {"version": version, "generated_at": "2030-01-01T08:00:00+00:00",
            "nodes": [{"id": "p:111", "kind": "tern", "title": title, "where": "Tern · tab 1", "state": "waiting",
                       "since": "2030-01-01T07:59:00+00:00", "pane": 147, "waiting_for": "question or form"},
                      {"id": "p:222", "kind": "orchestrated", "title": "site · plan", "where": "Fake",
                       "state": "working", "since": None},
                      {"id": "p:333", "kind": "background", "title": "nightly report", "where": "in the background",
                       "state": "done", "since": None},
                      {"id": "a:abcdef01", "kind": "helper", "title": "Reviews", "where": "helper",
                       "state": "working", "since": None}],
            "lines": [{"from": "p:222", "to": "a:abcdef01", "kind": "called"}],
            "problems_n": 0, "hidden_n": 2}


class FakeAdapter:
    name = "Fake"
    env_var = "FAKE_HANDLE"
    env_pattern = r"term_[0-9a-z-]+"

    def __init__(self, answer=True):
        self.answer = answer
        self.focused = []

    def steps(self, live_session_ids, live_handles):
        return []

    def focus(self, handle):
        self.focused.append(handle)
        return self.answer


class Base(unittest.TestCase):
    idle = 60.0

    def setUp(self):
        self.calls = 0
        self.snap = fake_snapshot()

        def fake():
            self.calls += 1
            return json.loads(json.dumps(self.snap))

        p = mock.patch.object(snapshot, "snapshot", fake)
        p.start()
        self.addCleanup(p.stop)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.page = Path(folder.name) / "page.html"
        self.page.write_text("<!doctype html><title>planted page</title>", encoding="utf-8")
        p = mock.patch.dict(os.environ, {"SESSION_MAP_PAGE": str(self.page)})
        p.start()
        self.addCleanup(p.stop)
        self.port = free_port()
        self.srv = server.Server(self.port, self.idle)
        self.thread = threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self.srv.shutdown()
        self.srv.server_close()

    def ask(self, method, path, body=None, host=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        c.putheader("Host", host if host is not None else f"127.0.0.1:{self.port}")
        for k, v in (headers or {}).items():
            c.putheader(k, v)
        if body is not None:
            c.putheader("Content-Length", str(len(body)))
        c.endheaders(body)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, dict(r.getheaders()), data

    def focus(self, body, origin="OURS", ctype="application/json", host=None):
        headers = {"Content-Type": ctype}
        if origin == "OURS":
            origin = f"http://127.0.0.1:{self.port}"
        if origin is not None:
            headers["Origin"] = origin
        return self.ask("POST", "/focus", body if isinstance(body, bytes) else body.encode("utf-8"), host, headers)


class Routes(Base):
    def test_state_200_with_its_shape(self):
        st, headers, body = self.ask("GET", "/state")
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertEqual(d, self.snap)
        for k in ("version", "generated_at", "nodes", "lines", "problems_n"):
            self.assertIn(k, d)
        self.assertEqual(headers["ETag"], '"abc123"')
        self.assertTrue(headers["Content-Type"].startswith("application/json"))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertTrue(headers["Server"].startswith("claude-session-map"))

    def test_304_with_equal_if_none_match(self):
        self.assertEqual(self.ask("GET", "/state", headers={"If-None-Match": '"abc123"'})[0], 304)
        self.assertEqual(self.ask("GET", "/state", headers={"If-None-Match": "abc123"})[0], 304)
        st, _h, body = self.ask("GET", "/state", headers={"If-None-Match": '"other"'})
        self.assertEqual(st, 200)
        self.assertTrue(body)

    def test_304_has_no_body(self):
        st, _h, body = self.ask("GET", "/state", headers={"If-None-Match": '"abc123"'})
        self.assertEqual((st, body), (304, b""))

    def test_root_serves_the_page(self):
        st, headers, body = self.ask("GET", "/")
        self.assertEqual(st, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertEqual(body, self.page.read_bytes())

    def test_odd_paths_never_serve_a_file(self):
        source = (ROOT / "session_map" / "snapshot.py").read_bytes()
        for path in ("/../", "/state?x", "/page.html/../snapshot.py", "/page.html", "/snapshot.py",
                     "/..%2fsnapshot.py", "/%2e%2e/snapshot.py", "//etc/passwd", "/state/", "/server.py"):
            st, _h, body = self.ask("GET", path)
            self.assertEqual(st, 404, path)  # exact routes only
            self.assertNotIn(source[:200], body, path)
            self.assertNotIn(b"import ", body, path)

    def test_wrong_host_403(self):
        for host in ("example.com", "example.com:%d" % self.port, "127.0.0.1", "127.0.0.1:1", ""):
            for path in ("/", "/state"):
                self.assertEqual(self.ask("GET", path, host=host)[0], 403, (host, path))

    def test_localhost_accepted(self):
        self.assertEqual(self.ask("GET", "/state", host=f"localhost:{self.port}")[0], 200)

    def test_cache_of_1_s(self):
        self.ask("GET", "/state")
        self.ask("GET", "/state")
        self.assertEqual(self.calls, 1)
        time.sleep(server.CACHE_S + 0.1)
        self.ask("GET", "/state")
        self.assertEqual(self.calls, 2)

    def test_broken_snapshot_gives_500_without_its_text(self):
        def broken():
            raise RuntimeError(HOME_PREFIX + "someone secret")
        with mock.patch.object(snapshot, "snapshot", broken):
            st, _h, body = self.ask("GET", "/state")
        self.assertEqual(st, 500)
        self.assertNotIn(HOME_PREFIX.encode(), body)
        self.assertNotIn(b"secret", body)
        self.assertIn(b"RuntimeError", body)

    def test_listens_on_loopback_only(self):
        self.assertEqual(self.srv.server_address[0], "127.0.0.1")
        ip = None
        try:
            u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            u.connect(("10.255.255.255", 1))  # no packet is sent; it only picks the outward interface
            ip = u.getsockname()[0]
            u.close()
        except OSError:
            pass
        if not ip or ip.startswith("127."):
            self.skipTest("no network address outside the loopback")
        with self.assertRaises(OSError):
            socket.create_connection((ip, self.port), timeout=1).close()

    def test_nothing_logged(self):
        with mock.patch.object(sys, "stderr") as err:
            self.ask("GET", "/state")
            self.ask("GET", "/nothing")
        self.assertFalse(err.write.called)


class MergeClosed(Base):
    def test_merge_closed_between_snapshots(self):
        seen = []

        def merge(previous, current, now):
            seen.append((previous["version"], current["version"]))
            return dict(current, version="merged")
        with mock.patch.object(snapshot, "merge_closed", merge):
            self.ask("GET", "/state")
            time.sleep(server.CACHE_S + 0.1)
            self.snap = fake_snapshot(version="new")
            _st, headers, _ = self.ask("GET", "/state")
        self.assertEqual(seen, [("abc123", "new")])
        self.assertEqual(headers["ETag"], '"merged"')


class IdleExit(Base):
    idle = 0.4

    def test_exits_after_idle(self):
        self.ask("GET", "/state")
        self.thread.join(timeout=3)
        self.assertFalse(self.thread.is_alive())

    def test_requests_delay_the_exit(self):
        end = time.monotonic() + 0.9
        while time.monotonic() < end:
            self.ask("GET", "/state")
            time.sleep(0.1)
        self.assertTrue(self.thread.is_alive())


class Open(Base):
    def test_with_one_live_no_second_starts(self):
        url = f"http://127.0.0.1:{self.port}/"
        with mock.patch.object(server.subprocess, "Popen") as popen, \
                mock.patch.object(server, "_run", return_value=True) as run, \
                mock.patch.object(server.tern, "open_beside") as beside, \
                mock.patch.dict(os.environ, {}, clear=False), contextlib.redirect_stdout(io.StringIO()):
            os.environ.pop("TERN_PANE", None)
            self.assertEqual(server.open_map(self.port), 0)
        popen.assert_not_called()
        beside.assert_not_called()
        expected = ["open", url] if sys.platform == "darwin" else ["xdg-open", url]
        self.assertEqual(run.call_args[0][0], expected)

    def test_inside_tern_opens_beside(self):
        with mock.patch.object(server, "_run", return_value=True) as run, \
                mock.patch.object(server.tern, "open_beside", return_value=True) as beside, \
                mock.patch.dict(os.environ, {"TERN_PANE": "147"}), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(server.open_map(self.port), 0)
        beside.assert_called_once_with("147", f"http://127.0.0.1:{self.port}/")
        run.assert_not_called()

    def test_real_13_digit_pane_opens_beside(self):
        """Real Tern block ids are long; the request carries the whole number."""
        with mock.patch.object(tern, "_run", return_value=True) as run, \
                mock.patch.object(tern, "exe", return_value=FAKE_TERN), \
                mock.patch.object(server, "_run", return_value=True) as browser, \
                mock.patch.dict(os.environ, {"TERN_PANE": "1234567890123"}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(server.open_map(self.port), 0)
        browser.assert_not_called()
        self.assertEqual(len(run.call_args_list), 1)
        args = run.call_args[0][0]
        self.assertEqual(args[:2], [FAKE_TERN, "browser"])
        self.assertEqual(json.loads(args[2]), {"op": "open", "owner": 1234567890123,
                                               "url": f"http://127.0.0.1:{self.port}/", "how": "beside"})

    def test_probe_recognises_ours(self):
        self.assertEqual(server.probe(self.port), "ours")


class OpenWithoutServer(unittest.TestCase):
    def test_port_of_another_program_exits_1(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        p = s.getsockname()[1]

        def answer():
            c, _ = s.accept()
            c.recv(1024)
            c.sendall(b"HTTP/1.0 200 OK\r\nServer: something-else\r\n\r\nhello")
            c.close()
        t = threading.Thread(target=answer, daemon=True)
        t.start()
        try:
            with mock.patch.object(server.subprocess, "Popen") as popen, \
                    mock.patch.object(server, "_run") as run, \
                    mock.patch.object(server.tern, "open_beside") as beside, \
                    mock.patch.object(sys, "stderr") as err:
                self.assertEqual(server.open_map(p), 1)
            popen.assert_not_called()
            run.assert_not_called()
            beside.assert_not_called()
            written = "".join(c[0][0] for c in err.write.call_args_list)
            self.assertIn("taken by another program", written)
        finally:
            s.close()

    def test_free_port_starts_detached(self):
        p = free_port()
        with mock.patch.object(server.subprocess, "Popen") as popen, \
                mock.patch.object(server, "probe", side_effect=["free", "free", "ours"]), \
                mock.patch.object(server, "_run", return_value=True), \
                mock.patch.object(server.time, "sleep"), \
                mock.patch.dict(os.environ, {}, clear=False), contextlib.redirect_stdout(io.StringIO()):
            os.environ.pop("TERN_PANE", None)
            self.assertEqual(server.open_map(p), 0)
        args, kw = popen.call_args
        self.assertEqual(args[0], [sys.executable, "-m", "session_map", "serve"])
        self.assertTrue(kw["start_new_session"])
        self.assertIsNot(kw.get("stdout"), None)
        self.assertEqual(kw["env"]["PYTHONPATH"].split(os.pathsep)[0], str(ROOT))

    def test_serve_on_a_taken_port_exits_1(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        try:
            with mock.patch.object(sys, "stderr"):
                self.assertEqual(server.serve(s.getsockname()[1]), 1)
        finally:
            s.close()


class ErrorWithoutTraceback(unittest.TestCase):
    """A connection error in `serve` is one short line: no traceback, no paths."""

    def test_handle_error_one_line(self):
        srv = server.Server(free_port(), 60.0)
        try:
            with mock.patch.object(sys, "stderr") as err:
                try:
                    raise BrokenPipeError(HOME_PREFIX + "someone/secret GET /state?x=conversation")
                except BrokenPipeError:
                    srv.handle_error(None, ("127.0.0.1", 50000))
            written = "".join(c[0][0] for c in err.write.call_args_list)
        finally:
            srv.server_close()
        self.assertEqual(written.count("\n"), 1)
        self.assertIn("BrokenPipeError", written)
        for forbidden in ("Traceback", HOME_PREFIX, "secret", "conversation", "50000", "File "):
            self.assertNotIn(forbidden, written)

    def test_cut_connection_prints_no_traceback(self):
        p = free_port()
        srv = server.Server(p, 60.0)
        thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            with mock.patch.object(sys, "stderr") as err, \
                    mock.patch.object(server.Handler, "do_GET", side_effect=ConnectionResetError(HOME_PREFIX + "x")):
                c = socket.create_connection(("127.0.0.1", p), timeout=5)
                c.sendall(f"GET /state HTTP/1.0\r\nHost: 127.0.0.1:{p}\r\n\r\n".encode("ascii"))
                c.recv(4096)
                c.close()
                time.sleep(0.3)
            written = "".join(c[0][0] for c in err.write.call_args_list)
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertNotIn("Traceback", written)
        self.assertNotIn(HOME_PREFIX, written)
        self.assertIn("ConnectionResetError", written)


class Rebind(unittest.TestCase):
    """The port after a server exits: TIME_WAIT must not block a new one; a live one must still block."""

    def _up(self, p):
        srv = server.Server(p, 60.0)
        thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        return srv

    def _down(self, srv):
        srv.shutdown()
        srv.server_close()

    def test_rebinds_over_time_wait(self):
        p = free_port()
        with mock.patch.object(snapshot, "snapshot", lambda: fake_snapshot()):
            srv = self._up(p)
            for _ in range(3):  # HTTP/1.0: the server closes first, so its side lands in TIME_WAIT
                c = socket.create_connection(("127.0.0.1", p), timeout=5)
                c.sendall(f"GET /state HTTP/1.0\r\nHost: 127.0.0.1:{p}\r\n\r\n".encode("ascii"))
                while c.recv(4096):
                    pass
                c.close()
            self._down(srv)
            self.assertEqual(server.probe(p), "free")  # TIME_WAIT is not a listener
            again = self._up(p)  # must bind at once
            self._down(again)

    def test_second_live_on_the_same_port_fails(self):
        p = free_port()
        srv = self._up(p)
        try:
            with self.assertRaises(OSError):
                server.Server(p, 60.0)
        finally:
            self._down(srv)


class Focus(Base):
    def setUp(self):
        super().setUp()
        self.adapter = FakeAdapter()
        p = mock.patch.object(snapshot, "focus_target", return_value=None)  # no real ps in these tests
        self.target = p.start()
        self.addCleanup(p.stop)

    def test_tern_resolves_from_the_snapshot(self):
        self.ask("GET", "/state")
        with mock.patch.object(tern, "_run", return_value=True) as run, \
                mock.patch.object(tern, "exe", return_value=FAKE_TERN):
            st, _h, body = self.focus('{"id": "p:111"}')
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(run.call_args_list[0][0][0], [FAKE_TERN, "focus", "147"])
        for c in run.call_args_list:
            self.assertIn(Path(c[0][0][0]).name, ("tern", "open"))

    def test_tern_real_13_digit_pane(self):
        self.snap["nodes"][0]["pane"] = 1234567890123
        with mock.patch.object(tern, "_run", return_value=True) as run, \
                mock.patch.object(tern, "exe", return_value=FAKE_TERN):
            _st, _h, body = self.focus('{"id": "p:111"}')
        self.assertTrue(json.loads(body)["ok"])
        self.assertEqual(run.call_args_list[0][0][0], [FAKE_TERN, "focus", "1234567890123"])

    def test_client_pane_is_ignored(self):
        with mock.patch.object(tern, "_run", return_value=True) as run, \
                mock.patch.object(tern, "exe", return_value=FAKE_TERN):
            self.focus('{"id": "p:111", "pane": 999, "kind": "tern"}')
        self.assertEqual(run.call_args_list[0][0][0][2], "147")

    def test_orchestrated_resolves_through_focus_target(self):
        self.target.return_value = (self.adapter, "term_right")
        with mock.patch.object(tern, "_run") as run:
            st, _h, body = self.focus('{"id": "p:222"}')
        self.assertEqual(st, 200)
        d = json.loads(body)
        self.assertTrue(d["ok"])
        self.assertIn("Fake", d["message"])
        self.target.assert_called_once_with(222)
        self.assertEqual(self.adapter.focused, ["term_right"])
        run.assert_not_called()

    def test_orchestrated_without_a_tab_says_where(self):
        with mock.patch.object(tern, "_run") as run:
            st, _h, body = self.focus('{"id": "p:222"}')
        run.assert_not_called()
        self.assertEqual(self.adapter.focused, [])
        d = json.loads(body)
        self.assertEqual((st, d["ok"]), (200, False))
        self.assertIn("Fake", d["message"])

    def test_no_target_says_where(self):
        with mock.patch.object(tern, "_run") as run:
            for nid, word in (("p:333", "background"), ("a:abcdef01", "helper")):
                st, _h, body = self.focus(json.dumps({"id": nid}))
                d = json.loads(body)
                self.assertEqual((st, d["ok"]), (200, False), nid)
                self.assertIn(word, d["message"].lower(), nid)
        run.assert_not_called()
        self.target.assert_not_called()

    def test_refusals(self):
        with mock.patch.object(tern, "_run") as run:
            cases = [
                ("unknown id", self.focus('{"id": "p:999"}'), 404),
                ("malformed id", self.focus('{"id": "p:1; rm -rf /"}'), 400),
                ("id not text", self.focus('{"id": 111}'), 400),
                ("not JSON", self.focus("id=p:111"), 400),
                ("wrong type", self.focus('{"id": "p:111"}', ctype="text/plain"), 415),
                ("form", self.focus('{"id": "p:111"}', ctype="application/x-www-form-urlencoded"), 415),
                ("no origin", self.focus('{"id": "p:111"}', origin=None), 403),
                ("other origin", self.focus('{"id": "p:111"}', origin="http://example.com"), 403),
                ("other port", self.focus('{"id": "p:111"}', origin="http://127.0.0.1:1"), 403),
                ("wrong host", self.focus('{"id": "p:111"}', host="example.com"), 403),
                ("too large", self.focus('{"id": "p:111", "x": "' + "a" * 2000 + '"}'), 413),
            ]
            for name, (st, _h, body), expected in cases:
                self.assertEqual(st, expected, name)
                d = json.loads(body)
                self.assertTrue(d.get("message") or d.get("error"), name)  # the page shows one of these keys
        run.assert_not_called()
        self.assertEqual(self.adapter.focused, [])

    def test_get_on_focus_and_other_methods(self):
        self.assertEqual(self.ask("GET", "/focus")[0], 404)
        self.assertEqual(self.ask("OPTIONS", "/focus")[0], 405)
        self.assertEqual(self.ask("PUT", "/state", b"")[0], 405)


class FocusAfterClear(Base):
    """After a `/clear` the registry carries a new sessionId the orchestrator never saw; the click still reaches
    the tab through the adapter's handle in the process environment (a fake `ps`), via snapshot.focus_target."""

    def setUp(self):
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.ps = Path(folder.name) / "ps.json"
        p = mock.patch.dict(os.environ, {"SESSION_MAP_PS_JSON": str(self.ps)})
        p.start()
        self.addCleanup(p.stop)
        self.adapter = FakeAdapter()
        saved = (list(snapshot._ENV_RES), snapshot._ENV_KEY, list(snapshot._IN_USE), dict(snapshot._MATCHED),
                 dict(snapshot._PANES))

        def restore():
            snapshot._ENV_RES, snapshot._ENV_KEY, snapshot._IN_USE = saved[0], saved[1], saved[2]
            snapshot._MATCHED.clear()
            snapshot._MATCHED.update(saved[3])
            snapshot._PANES.clear()
            snapshot._PANES.update(saved[4])
        self.addCleanup(restore)
        snapshot._use_adapters([self.adapter])
        snapshot._IN_USE = [self.adapter]
        snapshot._PANES.pop(222, None)  # the cache never answers for the fake pid
        self.match(None)

    def match(self, step_handle):
        snapshot._MATCHED.clear()
        snapshot._MATCHED[222] = (0, step_handle)

    def _ps(self, lines):
        self.ps.write_text(json.dumps(lines), encoding="utf-8")

    def _click(self):
        with mock.patch.object(tern, "_run", return_value=True) as run:
            st, _h, body = self.focus('{"id": "p:222"}')
        return st, json.loads(body), [c[0][0] for c in run.call_args_list]

    def test_checker_without_a_handle_in_ps_finds_nothing(self):
        """The checker shown blind first: no handle in the fake `ps` and none on the step means no jump."""
        self._ps(["  222 claude TERN_X=1"])
        st, d, calls = self._click()
        self.assertEqual((st, d["ok"], calls, self.adapter.focused), (200, False, [], []))

    def test_click_after_clear_uses_the_environment_handle(self):
        h = "term_0a1b2c3d-4e5f-6789-abcd-ef0123456789"
        self._ps([f"  222 claude --resume FAKE_HANDLE={h} HOME=/x"])
        st, d, calls = self._click()
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        self.assertEqual(self.adapter.focused, [h])
        self.assertEqual(calls, [])

    def test_handle_outside_the_rule_is_refused(self):
        for bad in ("term_x; rm -rf /", "term x", "", "a" * 81, "term_$(id)"):
            self._ps([f"  222 claude FAKE_HANDLE={bad}"])
            self.match(bad)
            st, d, calls = self._click()
            self.assertEqual((st, d["ok"], calls), (200, False, []), bad)
            # the server's own check, even when focus_target hands it a bad handle
            with mock.patch.object(snapshot, "focus_target", return_value=(self.adapter, bad)):
                st, d, calls = self._click()
            self.assertEqual((st, d["ok"], calls), (200, False, []), bad)
        self.assertEqual(self.adapter.focused, [])


class NothingPersonal(Base):
    def _scan(self, body):
        """Counts of forbidden shapes in the /state body (never the matching text)."""
        text = body.decode("utf-8")
        return {"users": text.count(HOME_PREFIX), "uuid": len(_UUID_RE.findall(text)),
                "env": text.count("TERN_PANE=")}

    def test_checker_flags_a_bad_case(self):
        bad = json.dumps({"title": HOME_PREFIX + "a01234567-89ab-cdef-0123-456789abcdef TERN_PANE=3"}).encode("utf-8")
        self.assertTrue(all(v > 0 for v in self._scan(bad).values()))

    def test_fake_state_clean(self):
        st, _h, body = self.ask("GET", "/state")
        self.assertEqual(st, 200)
        self.assertEqual(self._scan(body), {"users": 0, "uuid": 0, "env": 0})

    def test_real_state_clean(self):
        """This machine's live sessions through the real snapshot (Tern listing replaced by an empty one, no
        adapters): only the counts can fail the test."""
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        listing = Path(folder.name) / "tern.json"
        listing.write_text('{"sessions": [], "detached": []}', encoding="utf-8")
        snapshot.forget()
        self.addCleanup(snapshot.forget)
        with mock.patch.object(snapshot, "snapshot", _real_snapshot), \
                mock.patch.dict(os.environ, {"SESSION_MAP_TERN_JSON": str(listing)}):
            os.environ.pop("SESSION_MAP_ADAPTERS", None)
            st, _h, body = self.ask("GET", "/state")
        self.assertEqual(st, 200)
        self.assertEqual(self._scan(body), {"users": 0, "uuid": 0, "env": 0})


class Cli(unittest.TestCase):
    def test_bad_arguments_exit_2(self):
        for argv in (["--bogus"], [], ["nothing"], ["snapshot", "--bogus"]):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(argv), 2, argv)

    def test_snapshot_and_prototype_go_through_snapshot_main(self):
        with mock.patch.object(snapshot, "main", return_value=0) as main:
            self.assertEqual(cli.main(["snapshot", "--problems"]), 0)
            self.assertEqual(cli.main(["prototype"]), 0)
            self.assertEqual(cli.main(["prototype", "--out", "x.html"]), 0)
        self.assertEqual([c[0][0] for c in main.call_args_list],
                         [["snapshot", "--problems"], ["prototype", "--out", "prototype.html"],
                          ["prototype", "--out", "x.html"]])

    def test_serve_and_open_errors_are_one_line(self):
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"SESSION_MAP_PORT": "not-a-port"}), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["serve"]), 1)
            self.assertEqual(cli.main(["open"]), 1)
        self.assertEqual(err.getvalue().count("\n"), 2)
        self.assertIn("SESSION_MAP_PORT", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())
        with mock.patch.object(server, "serve", side_effect=KeyboardInterrupt):
            self.assertEqual(cli.main(["serve"]), 0)


if __name__ == "__main__":
    unittest.main()
