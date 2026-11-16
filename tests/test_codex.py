"""The Codex source (session_map/sources/codex.py) against the plan's Codex state rule.

Every scenario builds a temporary Codex home (`SESSION_MAP_CODEX_DIR`) whose rollouts are written at run time
from the shape templates in tests/fixtures/codex/records.json (record and payload types, key names and value
kinds copied from real rollouts; every id, path, name and text invented), plus a fake process listing
(`SESSION_MAP_CODEX_PS_JSON`) and a fake Tern listing (`SESSION_MAP_TERN_JSON`). The Claude side points at an
empty registry, so the snapshots hold Codex nodes only. Templates are filled in code rather than shipped as
whole rollouts because every clock in the rule (process start, permission wait, stuck, the 8 h cut, helper
silence) is relative to now.

Every snapshot this module makes goes through the invariant sweep: tests/test_snapshot.violations, with the
allowed node keys extended here (only here) by `agent` and `inferred`, and a scan of the whole JSON for every
thread id, cwd and message text the scenario wrote.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import test_snapshot as ts
from session_map import snapshot
from session_map.sources import codex

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "codex"
TEMPLATES = json.loads((FIXTURES / "records.json").read_text(encoding="utf-8"))

CWD = "/tmp/proj-a"
TERMINAL_ORIGIN = "codex_cli_rs"  # any originator outside DESKTOP_ORIGINATORS is a terminal one
CODEX_BIN = "/usr/local/bin/codex"
BUNDLED_CODEX = "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"  # the TUI (Checkpoint 0)
DAEMON_CODEX = "/tmp/invented home/app-server-daemon/CodexCLI.app/Contents/MacOS/codex"  # a path with a space
QUESTION_TEXT = "Which of the two names do you prefer, Alder or Birch?"
NODE_EXTRA = {"agent", "inferred"}  # the Codex node keys this module allows on top of test_snapshot's

_TMP = None
_N = [0]


def setUpModule():
    global _TMP
    _TMP = Path(tempfile.mkdtemp(prefix="session-map-codex-test-"))


def tearDownModule():
    shutil.rmtree(_TMP, ignore_errors=True)


def tid(n: int) -> str:
    """An invented thread id in the UUID shape Codex uses."""
    return f"{n:08d}-0000-4000-8000-{n:012d}"


def x_id(thread: str) -> str:
    return "x:" + hashlib.sha256(thread.encode("utf-8")).hexdigest()[:12]


def a_id(thread: str) -> str:
    return "a:" + hashlib.sha256(thread.encode("utf-8")).hexdigest()[:8]


def iso_z(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(epoch)) + ".%03dZ" % int((epoch % 1) * 1000)


def line(record) -> str:
    """One rollout line as Codex writes it: compact JSON (no spaces after `,` and `:`), then a newline."""
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"


def etime(seconds: float) -> str:
    """`ps -o etime` for an elapsed time: [[dd-]hh:]mm:ss."""
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    out = f"{h:02d}:{m:02d}:{s:02d}"
    return f"{d}-{out}" if d else out


class _Base(unittest.TestCase):
    """A temp Codex home, an empty Claude registry, fake ps and Tern listings, and record builders."""

    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for k in list(os.environ):
            if k.startswith("SESSION_MAP_") or k in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):
                os.environ.pop(k, None)
        _N[0] += 1
        self.d = _TMP / f"t{_N[0]}"
        self.home = self.d / "codex"
        (self.home / "sessions").mkdir(parents=True)
        claude = self.d / "claude"
        for sub in ("sessions", "projects"):
            (claude / sub).mkdir(parents=True)
        (self.d / "home").mkdir()
        (claude / "ps.json").write_text("[]", encoding="utf-8")
        self.ps_path = self.d / "codex-ps.json"
        self.tern_path = self.d / "tern.json"
        self.comm: list = []
        self.env_lines: list = []
        self.blocks: list = []
        self.write_ps()
        self.write_tern()
        os.environ.update({
            "HOME": str(self.d / "home"),
            "SESSION_MAP_REGISTRY_DIR": str(claude / "sessions"),
            "SESSION_MAP_PROJECTS_DIR": str(claude / "projects"),
            "SESSION_MAP_PS_JSON": str(claude / "ps.json"),
            "SESSION_MAP_TERN_JSON": str(self.tern_path),
            "SESSION_MAP_CODEX_DIR": str(self.home),
            "SESSION_MAP_CODEX_PS_JSON": str(self.ps_path),
        })
        codex.forget()
        snapshot.forget()
        self.addCleanup(codex.forget)
        self.addCleanup(snapshot.forget)
        self.now = time.time()
        self.secrets: set = set()  # thread ids, cwds and texts the scenario wrote: never in a snapshot
        self.index_lines: list = []

    # ------------------------------------------------------------ processes and Tern

    def write_ps(self):
        self.ps_path.write_text(json.dumps({"comm": self.comm, "env": self.env_lines}), encoding="utf-8")

    def write_tern(self):
        self.tern_path.write_text(json.dumps({"sessions": [{"id": 1, "name": "s", "tabs": [
            {"id": 10 + i, "number": n, "blocks": [{"id": p, "title": "zsh", "cwd": "/tmp", "program": "codex",
                                                   "live": True}]} for i, (p, n) in enumerate(self.blocks)]}],
            "detached": []}), encoding="utf-8")

    def proc(self, pid=501, cwd=CWD, age_s=3600, pane=None, ppid=400, path=CODEX_BIN, argv="codex"):
        """A terminal Codex process started `age_s` ago in `cwd` (its PWD), in a Tern pane when given."""
        self.comm.append(f"{pid:>5} {ppid:>5} {etime(age_s):>11} {path}")
        line = f"{pid:>5} {argv} PWD={cwd} TERM=xterm-256color OPENAI_API_KEY=sk-invented-secret"
        if pane is not None:
            line += f" TERN_PANE={pane}"
        self.env_lines.append(line + " SHELL=/bin/zsh")
        self.secrets.update({cwd, "sk-invented-secret"})
        self.write_ps()

    def app(self, pid=480, age_s=7200):
        """The ChatGPT desktop app's own process (Codex inside it)."""
        self.comm.append(f"{pid:>5}     1 {etime(age_s):>11} {codex.APP_PROCESS_PATH}")
        self.write_ps()

    def tab(self, pane, number):
        self.blocks.append((pane, number))
        self.write_tern()

    # ------------------------------------------------------------ records (shapes from the fixture templates)

    def rec(self, template, ago, **payload):
        r = copy.deepcopy(TEMPLATES[template])
        r["timestamp"] = iso_z(self.now - ago)
        r["payload"].update(payload)
        return r

    def meta(self, thread, ago=600, cwd=CWD, originator=TERMINAL_ORIGIN, source="cli", **extra):
        self.secrets.update({thread, cwd})
        return self.rec("session_meta", ago, id=thread, session_id=thread, cwd=cwd, originator=originator,
                        source=source, timestamp=iso_z(self.now - ago), runtime_workspace_roots=[cwd], **extra)

    def ctx(self, ago, policy="on-request", reviewer="user", turn="t1", cwd=CWD):
        return self.rec("turn_context", ago, turn_id=turn, root_turn_id=turn, approval_policy=policy,
                        approvals_reviewer=reviewer, cwd=cwd)

    def started(self, ago, turn="t1"):
        return self.rec("task_started", ago, turn_id=turn, root_turn_id=turn, started_at=int(self.now - ago))

    def complete(self, ago, text="Shipped the fix, all green.", error=None, turn="t1"):
        if text:
            self.secrets.add(text)
        return self.rec("task_complete", ago, turn_id=turn, root_turn_id=turn, last_agent_message=text,
                        error=error, completed_at=int(self.now - ago))

    def aborted(self, ago, turn="t1"):
        return self.rec("turn_aborted", ago, turn_id=turn, root_turn_id=turn, completed_at=int(self.now - ago))

    def user(self, ago, text="Please tidy the parser module."):
        self.secrets.add(text)
        r = self.rec("message", ago)
        r["payload"].update(role="user", content=[{"type": "input_text", "text": text}])
        return r

    def asst(self, ago, text="Reading the parser now."):
        self.secrets.add(text)
        r = self.rec("message", ago)
        r["payload"].update(role="assistant", content=[{"type": "output_text", "text": text}])
        return r

    def exec_call(self, ago, call_id="call_esc1", escalated=True):
        perm = ', sandbox_permissions: "require_escalated", justification: "Read a system folder."' if escalated \
            else ""
        text = f'await tools.exec_command({{cmd: "ls /etc/invented-dir"{perm}}})'
        self.secrets.add("/etc/invented-dir")
        return self.rec("custom_tool_call", ago, call_id=call_id, input=text)

    def exec_out(self, ago, call_id="call_esc1"):
        self.secrets.add("invented-output-line")
        return self.rec("custom_tool_call_output", ago, call_id=call_id, output="invented-output-line")

    def fn_escalated(self, ago, call_id="call_fn1"):
        args = json.dumps({"cmd": "ls /etc/invented-dir", "sandbox_permissions": "require_escalated",
                           "justification": "Read a system folder."})
        self.secrets.add("/etc/invented-dir")
        return self.rec("function_call", ago, name="exec_command", namespace=None, call_id=call_id, arguments=args)

    def question(self, ago, call_id="call_q1"):
        args = json.dumps({"questions": [{"id": "name", "header": "Name", "question": QUESTION_TEXT,
                                          "options": [{"label": "Alder"}, {"label": "Birch"}]}]})
        self.secrets.add(QUESTION_TEXT)
        return self.rec("function_call", ago, name="request_user_input_async", namespace=None, call_id=call_id,
                        arguments=args)

    def accepted(self, ago, call_id="call_q1"):
        return self.rec("function_call_output", ago, call_id=call_id, output=json.dumps({"accepted": True}))

    def noise(self, ago):
        return self.rec("token_count", ago)

    # ------------------------------------------------------------ files

    def rollout(self, thread, records, created_ago=600, mtime_ago=None, raw_tail=b""):
        """Write a rollout as Codex names it, mtime at its last record (or `mtime_ago`)."""
        t0 = self.now - created_ago
        day = time.localtime(t0)
        folder = self.home / "sessions" / time.strftime("%Y/%m/%d", day)
        folder.mkdir(parents=True, exist_ok=True)
        p = folder / f"rollout-{time.strftime('%Y-%m-%dT%H-%M-%S', day)}-{thread}.jsonl"
        data = "".join(line(r) for r in records).encode("utf-8") + raw_tail
        p.write_bytes(data)
        if mtime_ago is None:
            stamps = [ts.transcript.parse_iso(r["timestamp"]) for r in records if isinstance(r, dict)]
            stamps = [s.timestamp() for s in stamps if s is not None]
            mt = max(stamps) if stamps else t0
        else:
            mt = self.now - mtime_ago
        os.utime(p, (mt, mt))
        return p

    def append(self, path, records, mtime_ago=0.0):
        data = "".join(line(r) for r in records).encode("utf-8")
        with open(path, "ab") as fh:
            fh.write(data)
        mt = self.now - mtime_ago
        os.utime(path, (mt, mt))
        return data

    def name(self, thread, thread_name):
        self.index_lines.append(json.dumps({"id": thread, "thread_name": thread_name,
                                            "updated_at": iso_z(self.now)[:-1] + "000Z"}))
        (self.home / "session_index.jsonl").write_text("".join(x + "\n" for x in self.index_lines),
                                                      encoding="utf-8")

    # ------------------------------------------------------------ scenarios

    def busy(self, thread, ago=60, **meta):
        """A thread with one turn open, started `ago` seconds ago, no tool call yet."""
        return [self.meta(thread, ago=ago + 10, **meta), self.ctx(ago), self.started(ago), self.user(ago)]

    def terminal(self, thread, records, **proc):
        p = self.rollout(thread, records)
        self.proc(**proc)
        return p

    # ------------------------------------------------------------ joining and the sweep

    def join(self, now=None):
        snap, problems = snapshot._join(self.now if now is None else now)
        self.sweep(snap)
        return snap, problems

    def sweep(self, snap):
        with mock.patch.object(ts, "NODE_OPTIONAL", ts.NODE_OPTIONAL | NODE_EXTRA):
            bad = ts.violations(snap)
        self.assertEqual(bad, [], "test_snapshot.violations")
        text = json.dumps(snap, ensure_ascii=False)
        leaked = sorted(s for s in self.secrets if s and s in text)
        self.assertEqual(leaked, [], "the snapshot holds a thread id, cwd or text")

    def codex_nodes(self, snap):
        return [n for n in snap["nodes"] if n["id"].startswith(("x:", "a:"))]

    def node(self, snap, thread):
        found = [n for n in snap["nodes"] if n["id"] == x_id(thread)]
        self.assertEqual(len(found), 1, [n["id"] for n in snap["nodes"]])
        return found[0]

    def only(self, thread):
        """The one Codex node of a one-thread scenario (and there is exactly one)."""
        snap, problems = self.join()
        self.assertEqual(problems, [])
        self.assertEqual([n["id"] for n in self.codex_nodes(snap)], [x_id(thread)])
        return self.node(snap, thread)

    def state_of(self, n):
        return {k: n[k] for k in ("state", "waiting_for", "guessed", "inferred", "stuck") if k in n}


# ================================================================= rules 2-6 on a live terminal thread

class RuleCells(_Base):
    T = tid(1)

    def test_working_since_turn_start(self):
        self.terminal(self.T, self.busy(self.T, ago=60) + [self.asst(30)])
        n = self.only(self.T)
        self.assertEqual(n["state"], "working")
        self.assertEqual(n["since"], snapshot._iso(int(self.now - 60)))
        self.assertEqual(n["agent"], "codex")
        self.assertNotIn("inferred", n)

    def test_open_turn_untouched_is_stuck(self):
        old = snapshot.STUCK_S + 60
        self.terminal(self.T, self.busy(self.T, ago=old), age_s=old + 3600)
        self.assertEqual(self.only(self.T)["state"], "stopped")  # `stuck` is the rule's, not a node key
        fresh = tid(30)  # ruler: the same open turn touched just now is working
        self.terminal(fresh, self.busy(fresh, ago=old, cwd="/tmp/proj-fresh") + [self.asst(5)], pid=502, cwd="/tmp/proj-fresh")
        snap, _ = self.join()
        self.assertEqual(self.node(snap, fresh)["state"], "working")

    def test_permission_inferred(self):
        wait = codex.PERMISSION_WAIT_S + 10
        self.terminal(self.T, self.busy(self.T, ago=60) + [self.exec_call(wait)])
        n = self.only(self.T)
        self.assertEqual(self.state_of(n), {"state": "waiting", "waiting_for": "permission request",
                                            "inferred": True})

    def test_permission_inferred_from_a_function_call(self):
        wait = codex.PERMISSION_WAIT_S + 10
        self.terminal(self.T, self.busy(self.T, ago=60) + [self.fn_escalated(wait)])
        self.assertEqual(self.only(self.T)["waiting_for"], "permission request")

    def test_permission_under_untrusted_policy(self):
        wait = codex.PERMISSION_WAIT_S + 10
        recs = self.busy(self.T, ago=60)
        recs[1] = self.ctx(60, policy="untrusted")
        self.terminal(self.T, recs + [self.exec_call(wait)])
        self.assertEqual(self.only(self.T)["state"], "waiting")

    def test_no_permission_wait_when_nobody_is_asked(self):
        wait = codex.PERMISSION_WAIT_S + 10
        for policy, reviewer in (("never", "user"), ("on-request", "auto_review")):
            with self.subTest(policy=policy, reviewer=reviewer):
                codex.forget()
                t = tid(10 + len(policy) + len(reviewer))
                recs = self.busy(t, ago=60)
                recs[1] = self.ctx(60, policy=policy, reviewer=reviewer)
                shutil.rmtree(self.home / "sessions")
                (self.home / "sessions").mkdir()
                self.comm.clear()
                self.env_lines.clear()
                self.terminal(t, recs + [self.exec_call(wait)])
                n = self.only(t)
                self.assertEqual(self.state_of(n), {"state": "working"})

    def test_pending_under_the_threshold_is_working(self):
        self.terminal(self.T, self.busy(self.T, ago=60) + [self.exec_call(codex.PERMISSION_WAIT_S * 0.3)])
        self.assertEqual(self.state_of(self.only(self.T)), {"state": "working"})

    def test_escalated_call_with_its_output_is_working(self):
        wait = codex.PERMISSION_WAIT_S + 10
        self.terminal(self.T, self.busy(self.T, ago=60) + [self.exec_call(wait), self.exec_out(wait - 1)])
        self.assertEqual(self.state_of(self.only(self.T)), {"state": "working"})

    def test_plain_call_is_not_a_permission_wait(self):
        wait = codex.PERMISSION_WAIT_S + 10
        self.terminal(self.T, self.busy(self.T, ago=60) + [self.exec_call(wait, escalated=False)])
        self.assertEqual(self.state_of(self.only(self.T)), {"state": "working"})

    def test_question_pending(self):
        recs = self.busy(self.T, ago=60) + [self.question(20), self.accepted(19.7)]  # the ack does not clear it
        self.terminal(self.T, recs)
        n = self.only(self.T)
        self.assertEqual(n["state"], "waiting")
        self.assertEqual(n["waiting_for"], "question or form")
        if codex.QUESTION_WRITTEN:
            self.assertNotIn("inferred", n)
        else:
            self.assertIs(n["inferred"], True)

    def test_question_cleared_by_a_later_user_message(self):
        recs = self.busy(self.T, ago=60) + [self.question(20), self.accepted(19.7),
                                            self.user(10, "Birch, please.")]
        self.terminal(self.T, recs)
        self.assertEqual(self.state_of(self.only(self.T)), {"state": "working"})

    def test_question_then_task_complete_goes_by_the_text(self):
        """A grid surprise: the turn ending answers the question; the state is the closing text's."""
        for text, want in (("Named it Birch, all set.", {"state": "done"}),
                           ("Named it Birch. Want me to push it?", {"state": "waiting", "guessed": True})):
            with self.subTest(text=text):
                codex.forget()
                shutil.rmtree(self.home / "sessions")
                (self.home / "sessions").mkdir()
                self.comm.clear()
                self.env_lines.clear()
                recs = self.busy(self.T, ago=60) + [self.question(40), self.accepted(39.7), self.complete(5, text)]
                self.terminal(self.T, recs)
                self.assertEqual(self.state_of(self.only(self.T)), want)

    def test_question_in_an_aborted_turn_is_cleared(self):
        recs = self.busy(self.T, ago=60) + [self.question(40), self.accepted(39.7), self.aborted(5)]
        self.terminal(self.T, recs)
        self.assertEqual(self.state_of(self.only(self.T)), {"state": "stopped"})

    def test_escalated_call_cleared_at_turn_end(self):
        wait = codex.PERMISSION_WAIT_S + 30
        recs = self.busy(self.T, ago=60) + [self.exec_call(wait), self.complete(5)]
        self.terminal(self.T, recs)
        self.assertEqual(self.state_of(self.only(self.T)), {"state": "done"})

    def test_complete_plain_text_is_done(self):
        self.terminal(self.T, self.busy(self.T, ago=60) + [self.complete(5)])
        self.assertEqual(self.state_of(self.only(self.T)), {"state": "done"})

    def test_complete_with_a_question_is_guessed_waiting(self):
        self.terminal(self.T, self.busy(self.T, ago=60) + [self.complete(5, "Plan ready. Want me to start?")])
        self.assertEqual(self.state_of(self.only(self.T)), {"state": "waiting", "guessed": True})

    def test_error_decides_before_the_text(self):
        for text in ("Shipped the fix, all green.", "Plan ready. Want me to start?"):
            with self.subTest(text=text):
                codex.forget()
                shutil.rmtree(self.home / "sessions")
                (self.home / "sessions").mkdir()
                self.comm.clear()
                self.env_lines.clear()
                err = {"message": "Invented failure.", "codex_error_info": "other"}
                self.secrets.add("Invented failure.")
                self.terminal(self.T, self.busy(self.T, ago=60) + [self.complete(5, text, error=err)])
                self.assertEqual(self.state_of(self.only(self.T)), {"state": "stopped"})

    def test_complete_without_a_message_is_stopped(self):
        self.terminal(self.T, self.busy(self.T, ago=60) + [self.complete(5, None)])
        self.assertEqual(self.state_of(self.only(self.T)), {"state": "stopped"})

    def test_aborted_is_stopped(self):
        self.terminal(self.T, self.busy(self.T, ago=60) + [self.aborted(5)])
        self.assertEqual(self.state_of(self.only(self.T)), {"state": "stopped"})

    def test_header_only_is_stopped(self):
        self.terminal(self.T, [self.meta(self.T, ago=30)])
        self.assertEqual(self.state_of(self.only(self.T)), {"state": "stopped"})

    def test_turn_markers_reach_the_codex_rule(self):
        """Rule 4 is turn_end_state itself: SESSION_MAP_DONE_LINES / WAIT_MARKS apply to Codex texts too."""
        os.environ[snapshot.ENV_DONE_LINES] = "Over to you?"
        self.terminal(self.T, self.busy(self.T, ago=60) + [self.complete(5, "Fixed it.\nOver to you?")])
        self.assertEqual(self.state_of(self.only(self.T)), {"state": "done"})

    def test_a_second_turn_counts_not_the_first(self):
        recs = self.busy(self.T, ago=300) + [self.complete(250, "Plan ready. Want me to start?"),
                                             self.ctx(100, turn="t2"), self.started(100, turn="t2"),
                                             self.user(100, "Yes, start.")]
        self.terminal(self.T, recs)
        n = self.only(self.T)
        self.assertEqual(self.state_of(n), {"state": "working"})
        self.assertEqual(n["since"], snapshot._iso(int(self.now - 100)))


# ================================================================= rule 1 and rule 0: liveness and the grid

class Liveness(_Base):
    T = tid(2)

    def test_terminal_other_and_where(self):
        self.terminal(self.T, self.busy(self.T))
        n = self.only(self.T)
        self.assertEqual((n["kind"], n["where"]), ("other", "Codex · other terminal"))
        self.assertNotIn("pane", n)

    def test_no_process_no_node(self):
        self.rollout(self.T, self.busy(self.T) + [self.complete(5)])
        snap, problems = self.join()
        self.assertEqual((self.codex_nodes(snap), problems), ([], []))

    def test_other_cwd_no_node(self):
        self.terminal(self.T, self.busy(self.T), cwd="/tmp/proj-elsewhere")
        snap, _ = self.join()
        self.assertEqual(self.codex_nodes(snap), [])

    def test_process_started_after_the_last_record_is_not_its_process(self):
        self.terminal(self.T, self.busy(self.T, ago=600) + [self.complete(500)], age_s=60)
        snap, _ = self.join()
        self.assertEqual(self.codex_nodes(snap), [])

    def test_two_threads_one_process_newest_only(self):
        old, new = tid(3), tid(4)
        self.rollout(old, self.busy(old, ago=400) + [self.complete(300)], created_ago=410)
        self.rollout(new, self.busy(new, ago=100) + [self.complete(20)], created_ago=110)
        self.proc()
        snap, _ = self.join()
        self.assertEqual([n["id"] for n in self.codex_nodes(snap)], [x_id(new)])

    def test_pwd_with_a_space(self):
        cwd = "/tmp/proj b two"
        self.terminal(self.T, self.busy(self.T, cwd=cwd), cwd=cwd)
        self.assertEqual(self.only(self.T)["state"], "working")

    def test_last_pwd_and_pane_win(self):
        """argv comes before the environment: a prompt that types PWD= or TERN_PANE= never wins."""
        self.tab(7, 3)
        self.tab(99, 8)
        self.terminal(self.T, self.busy(self.T), pane=7,
                      argv="codex PWD=/tmp/proj-decoy TERN_PANE=99 fix it")
        n = self.only(self.T)
        self.assertEqual((n["kind"], n.get("pane"), n["where"]), ("tern", 7, "Codex · Tern · tab 3"))

    def test_decoy_pwd_in_argv_alone_does_not_match(self):
        self.terminal(self.T, self.busy(self.T, cwd="/tmp/proj-decoy"), cwd=CWD,
                      argv="codex PWD=/tmp/proj-decoy")
        snap, _ = self.join()
        self.assertEqual(self.codex_nodes(snap), [])

    def test_tern_pane(self):
        self.tab(7, 3)
        self.terminal(self.T, self.busy(self.T), pane=7)
        n = self.only(self.T)
        self.assertEqual((n["kind"], n["pane"], n["where"]), ("tern", 7, "Codex · Tern · tab 3"))

    def test_the_apps_own_codex_is_never_a_terminal(self):
        """A codex whose parent (or grandparent) is the app is the app's own, wherever its binary lives."""
        for label, kw in (("child of the app", {"ppid": 480}),
                          ("grandchild of the app", {"ppid": 470, "path": BUNDLED_CODEX})):
            with self.subTest(label):
                codex.forget()
                self.comm.clear()
                self.env_lines.clear()
                self.app()
                self.comm.append("  470   480    01:00:00 /bin/zsh")
                self.rollout(self.T, self.busy(self.T))
                self.proc(**kw)
                snap, _ = self.join()
                self.assertEqual(self.codex_nodes(snap), [])

    def test_the_bundled_binary_run_from_tern_is_a_terminal(self):
        """Checkpoint 0: the terminal TUI IS the binary inside the app bundle; its parent is tern, not the app."""
        self.app()
        self.comm.append("  400     1    02:00:00 /usr/local/bin/tern")
        self.tab(7, 3)
        self.terminal(self.T, self.busy(self.T), pane=7, ppid=400, path=BUNDLED_CODEX, argv=BUNDLED_CODEX)
        n = self.only(self.T)
        self.assertEqual((n["kind"], n["where"]), ("tern", "Codex · Tern · tab 3"))

    def test_an_orphaned_app_server_daemon_is_no_session(self):
        """The TUI's `codex app-server` child outlives /quit, reparented to launchd, with the TUI's PWD and pane."""
        self.tab(7, 3)
        self.terminal(self.T, self.busy(self.T), pane=7, ppid=1, path=DAEMON_CODEX,
                      argv=DAEMON_CODEX + " app-server --listen stdio")
        snap, _ = self.join()
        self.assertEqual(self.codex_nodes(snap), [])

    def test_a_live_tuis_codex_child_adds_no_second_card(self):
        """The TUI's children (its `app-server`, or any other codex under it) are never a second session:
        the plain child is held out by the codex-ancestor rule alone, the daemon by both rules."""
        for label, child_argv in (("app-server child", DAEMON_CODEX + " app-server"),
                                  ("plain codex child", DAEMON_CODEX)):
            with self.subTest(label):
                codex.forget()
                self.comm.clear()
                self.env_lines.clear()
                self.tab(7, 3)
                self.terminal(self.T, self.busy(self.T), pid=501, pane=7, ppid=400, path=BUNDLED_CODEX,
                              argv=BUNDLED_CODEX)
                self.proc(pid=502, pane=7, ppid=501, path=DAEMON_CODEX, argv=child_argv)
                other = tid(41)  # a second thread in the same folder a child could otherwise claim
                self.rollout(other, self.busy(other, ago=120))
                snap, problems = self.join()
                self.assertEqual(problems, [])
                self.assertEqual([n["id"] for n in self.codex_nodes(snap)], [x_id(self.T)])

    def test_codex_resume_is_a_terminal(self):
        self.terminal(self.T, self.busy(self.T), ppid=400, argv="codex resume --last")
        self.assertEqual(self.only(self.T)["kind"], "other")

    def test_same_process_outside_the_app_is_a_terminal(self):
        """Ruler for the test above: the same process with ppid 1, outside the bundle, IS a terminal."""
        self.app()
        self.terminal(self.T, self.busy(self.T), ppid=1)
        self.assertEqual(self.only(self.T)["kind"], "other")

    def test_desktop_thread_live_while_the_app_runs(self):
        origin = codex.DESKTOP_ORIGINATORS[0]
        self.rollout(self.T, self.busy(self.T, originator=origin, source="vscode") + [self.complete(5)])
        snap, _ = self.join()
        self.assertEqual(self.codex_nodes(snap), [])  # app not running
        self.app()
        n = self.only(self.T)
        self.assertEqual((n["kind"], n["where"], n["state"]), ("app", "Codex app", "done"))
        self.assertNotIn("pane", n)

    def test_an_app_helper_process_is_not_the_app(self):
        origin = codex.DESKTOP_ORIGINATORS[0]
        self.rollout(self.T, self.busy(self.T, originator=origin, source="vscode"))
        self.comm.append("  481   480    01:00:00 /Applications/ChatGPT.app/Contents/Frameworks/ChatGPT Helper (Renderer)")
        self.write_ps()
        snap, _ = self.join()
        self.assertEqual(self.codex_nodes(snap), [])

    def test_terminal_thread_is_not_live_by_the_app(self):
        self.app()
        self.rollout(self.T, self.busy(self.T))
        snap, _ = self.join()
        self.assertEqual(self.codex_nodes(snap), [])

    def test_old_desktop_thread_is_not_considered(self):
        origin = codex.DESKTOP_ORIGINATORS[0]
        old = snapshot.MAX_AGE_S + 600
        self.app(age_s=old + 3600)
        self.rollout(self.T, self.busy(self.T, ago=old + 60, originator=origin, source="vscode")
                     + [self.complete(old)], created_ago=old + 100)
        snap, _ = self.join()
        self.assertEqual(self.codex_nodes(snap), [])

    def test_grid(self):
        """{turn open, complete, aborted, question pending, escalated pending} x {terminal live, terminal process
        gone, app alive, app closed}. Gone or closed -> no node (rule 1 wins, even over a pending permission)."""
        wait = codex.PERMISSION_WAIT_S + 10
        cells = {
            "open": (lambda t, **m: self.busy(t, **m), {"state": "working"}),
            "complete": (lambda t, **m: self.busy(t, **m) + [self.complete(5)], {"state": "done"}),
            "aborted": (lambda t, **m: self.busy(t, **m) + [self.aborted(5)], {"state": "stopped"}),
            "question": (lambda t, **m: self.busy(t, **m) + [self.question(20), self.accepted(19.7)],
                         {"state": "waiting", "waiting_for": "question or form"}),
            "escalated": (lambda t, **m: self.busy(t, **m) + [self.exec_call(wait)],
                          {"state": "waiting", "waiting_for": "permission request", "inferred": True}),
        }
        desktop = {"originator": codex.DESKTOP_ORIGINATORS[0], "source": "vscode"}
        for cell, (recs, want) in cells.items():
            for where in ("terminal live", "terminal gone", "app alive", "app closed"):
                with self.subTest(cell=cell, where=where):
                    codex.forget()
                    shutil.rmtree(self.home / "sessions")
                    (self.home / "sessions").mkdir()
                    self.comm.clear()
                    self.env_lines.clear()
                    self.write_ps()
                    meta = desktop if where.startswith("app") else {}
                    self.rollout(self.T, recs(self.T, **meta))
                    if where == "terminal live":
                        self.proc()
                    elif where == "app alive":
                        self.app()
                    snap, problems = self.join()
                    self.assertEqual(problems, [])
                    if where in ("terminal gone", "app closed"):
                        self.assertEqual(self.codex_nodes(snap), [])
                        continue
                    got = self.state_of(self.node(snap, self.T))
                    got.pop("inferred", None) if cell == "question" else None
                    self.assertEqual(got, want)

    def test_ps_failure_is_one_line_without_a_path(self):
        self.ps_path.write_text("{not json", encoding="utf-8")
        self.rollout(self.T, self.busy(self.T))
        snap, problems = self.join()
        self.assertEqual(self.codex_nodes(snap), [])
        self.assertLessEqual(len(problems), 1, problems)
        self.assertFalse(any(str(self.d) in p for p in problems), problems)


# ================================================================= helpers, guardian, titles, fork

class HelpersAndTitles(_Base):
    P = tid(5)

    def child(self, thread, parent, records, nickname="Scout", created_ago=50, mtime_ago=None):
        src = {"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1, "agent_path": "/root/scout",
                                             "agent_nickname": nickname, "agent_role": None}}}
        extra = {"parent_thread_id": parent, "thread_source": "subagent", "agent_path": "/root/scout"}
        if nickname:
            extra["agent_nickname"] = nickname
        recs = [self.meta(thread, ago=created_ago, source=src, **extra)] + records
        return self.rollout(thread, recs, created_ago=created_ago, mtime_ago=mtime_ago)

    def parent(self, records=None):
        self.terminal(self.P, records or self.busy(self.P, ago=120))

    def test_helper_working_with_called_line(self):
        c = tid(6)
        self.parent()
        self.child(c, self.P, [self.ctx(40), self.started(40), self.user(40, "Map the parser files.")])
        snap, problems = self.join()
        self.assertEqual(problems, [])
        self.assertEqual(self.node(snap, self.P)["state"], "working")  # the newer child never steals the process
        h = [n for n in snap["nodes"] if n["id"] == a_id(c)]
        self.assertEqual(len(h), 1, [n["id"] for n in snap["nodes"]])
        self.assertEqual((h[0]["kind"], h[0]["title"], h[0]["where"], h[0]["state"]),
                         ("helper", "Scout", "Codex helper", "working"))
        self.assertIn({"from": x_id(self.P), "to": a_id(c), "kind": "called"}, snap["lines"])
        self.assertNotIn(x_id(c), [n["id"] for n in snap["nodes"]])

    def test_helper_done_and_untitled(self):
        c = tid(7)
        self.parent()
        self.child(c, self.P, [self.ctx(40), self.started(40), self.complete(10, "Found three parser files.")],
                   nickname=None)
        snap, _ = self.join()
        h = [n for n in snap["nodes"] if n["id"] == a_id(c)][0]
        self.assertEqual((h["title"], h["state"]), ("helper", "done"))

    def test_done_helper_leaves_after_gone_s(self):
        c = tid(8)
        old = snapshot.GONE_S + 60
        self.parent(self.busy(self.P, ago=old + 300))
        self.child(c, self.P, [self.ctx(old + 100), self.started(old + 100), self.complete(old)],
                   created_ago=old + 120)
        snap, _ = self.join()
        self.assertNotIn(a_id(c), [n["id"] for n in snap["nodes"]])

    def test_silent_helper_is_closed_when_its_mother_is_not_busy(self):
        c = tid(9)
        silent = snapshot.DEAD_S + 60
        self.parent(self.busy(self.P, ago=silent + 300) + [self.complete(20)])
        self.child(c, self.P, [self.ctx(silent), self.started(silent)], created_ago=silent + 10)
        snap, _ = self.join()
        h = [n for n in snap["nodes"] if n["id"] == a_id(c)]
        self.assertEqual([n["state"] for n in h], ["closed"])

    def test_silent_helper_works_on_while_its_mother_is_busy(self):
        c = tid(10)
        silent = snapshot.DEAD_S + 60  # under SILENT_MAX_S
        self.parent(self.busy(self.P, ago=silent + 300) + [self.asst(5)])
        self.child(c, self.P, [self.ctx(silent), self.started(silent)], created_ago=silent + 10)
        snap, _ = self.join()
        self.assertEqual([n["state"] for n in snap["nodes"] if n["id"] == a_id(c)], ["working"])

    def test_helper_of_a_parent_off_the_map_is_not_shown(self):
        c = tid(11)
        self.rollout(self.P, self.busy(self.P, ago=120))  # no process: the parent is not a card
        self.child(c, self.P, [self.ctx(40), self.started(40)])
        snap, _ = self.join()
        self.assertEqual(self.codex_nodes(snap), [])

    def test_guardian_is_never_a_node(self):
        g = tid(12)
        self.parent()
        src = {"subagent": {"other": "guardian"}}
        self.rollout(g, [self.meta(g, ago=30, source=src, parent_thread_id=self.P, thread_source="guardian_review",
                                   agent_nickname="Guardian"),
                         self.ctx(25), self.started(25)], created_ago=30)
        snap, _ = self.join()
        self.assertEqual([n["id"] for n in self.codex_nodes(snap)], [x_id(self.P)])
        self.assertEqual(snap["lines"], [])

    def test_title_last_one_wins_cleaned(self):
        other = tid(13)
        self.parent()
        self.name(self.P, "First name")
        self.name(other, "Another thread's name")
        dirty = "✳ Fix the parser at " + "/" + "Users/someone/proj and " + tid(99) + " TOKEN=abc"
        self.name(self.P, dirty)
        self.secrets.update({tid(99), "TOKEN=abc", "someone"})
        n = self.only(self.P)
        self.assertEqual(n["title"], snapshot._clean(dirty))
        self.assertTrue(n["title"].startswith("Fix the parser at"))

    def test_no_name_is_unnamed_session(self):
        self.parent()
        self.assertEqual(self.only(self.P)["title"], "unnamed session")

    def test_fork_counts_only_the_first_header(self):
        src_thread = tid(14)
        recs = self.busy(self.P, ago=120)
        fork = self.meta(src_thread, ago=119, cwd="/tmp/proj-fork-source", forked_from_id=tid(15))
        self.secrets.add(tid(15))
        recs.insert(1, fork)
        self.terminal(self.P, recs + [self.complete(5)])
        self.name(self.P, "The fork itself")
        self.name(src_thread, "The source thread")
        snap, problems = self.join()
        self.assertEqual(problems, [])
        self.assertEqual([n["id"] for n in self.codex_nodes(snap)], [x_id(self.P)])
        self.assertEqual((self.node(snap, self.P)["title"], self.node(snap, self.P)["state"]),
                         ("The fork itself", "done"))


# ================================================================= the cache: incremental reads and cold twins

class Cache(_Base):
    T = tid(16)

    def cached_names(self) -> set:
        """Every key of codex.py's module-level dicts, as text (whatever shape the cache keys take)."""
        out = set()
        for v in vars(codex).values():
            if isinstance(v, dict):
                out.update(str(k) for k in v)
        return out

    def test_deleted_rollout_leaves_the_cache(self):
        p = self.terminal(self.T, self.busy(self.T))
        other = tid(17)
        self.rollout(other, self.busy(other, ago=30, cwd="/tmp/proj-other"))
        self.join()
        self.assertTrue(any(p.name in k for k in self.cached_names()), "ruler: the cache held the rollout")
        p.unlink()
        snap, problems = self.join()
        self.assertEqual(problems, [])
        self.assertFalse(any(p.name in k for k in self.cached_names()))
        self.assertEqual(self.codex_nodes(snap), [])

    def test_unchanged_second_read_is_zero_bytes(self):
        self.terminal(self.T, self.busy(self.T) + [self.question(20), self.accepted(19.7)])
        self.name(self.T, "A named thread")
        before = codex.READ["codex"]
        first, _ = self.join()
        self.assertGreater(codex.READ["codex"], before)
        mid = codex.READ["codex"]
        second, _ = self.join()
        self.assertEqual(codex.READ["codex"], mid)
        self.assertEqual(first["nodes"], second["nodes"])

    def test_grown_file_reads_only_new_bytes(self):
        p = self.terminal(self.T, self.busy(self.T, ago=60))
        self.join()
        mid = codex.READ["codex"]
        added = self.append(p, [self.asst(3, "Halfway through the parser."), self.noise(2)])
        self.join()
        self.assertEqual(codex.READ["codex"] - mid, len(added))

    def test_cold_start_twin(self):
        """Incremental reads through every stage give the same node as a cold read of the final file."""
        wait = codex.PERMISSION_WAIT_S + 20
        p = self.terminal(self.T, self.busy(self.T, ago=90) + [self.exec_call(wait)])
        self.name(self.T, "Twin thread")
        stages = [
            ([self.exec_out(wait - 2), self.asst(15, "Listed it.")], {"state": "working"}),
            ([self.question(12), self.accepted(11.7)], {"state": "waiting", "waiting_for": "question or form"}),
            ([self.user(9, "Alder."), self.complete(4, "Named it Alder. Shall I commit?")],
             {"state": "waiting", "guessed": True}),
        ]
        warm, _ = self.join()
        self.assertEqual(self.node(warm, self.T)["waiting_for"], "permission request")
        for recs, want in stages:
            self.append(p, recs, mtime_ago=1)
            warm, _ = self.join()
            got = self.state_of(self.node(warm, self.T))
            got.pop("inferred", None) if "question" in str(want) else None
            self.assertEqual(got, want)
            codex.forget()
            snapshot.forget()
            cold, _ = self.join()
            self.assertEqual(self.node(cold, self.T), self.node(warm, self.T))

    def test_partial_last_line_then_completed(self):
        p = self.terminal(self.T, self.busy(self.T, ago=60))
        self.join()
        raw = line(self.complete(2, "All wrapped up here.")).encode("utf-8")
        with open(p, "ab") as fh:
            fh.write(raw[:40])
        half, problems = self.join()  # half a record: no crash, no problem line, the turn still open
        self.assertEqual((self.node(half, self.T)["state"], problems), ("working", []))
        with open(p, "ab") as fh:
            fh.write(raw[40:])
        warm, _ = self.join()
        codex.forget()
        cold, _ = self.join()
        self.assertEqual(self.node(warm, self.T), self.node(cold, self.T))
        self.assertEqual(self.node(warm, self.T)["state"], "done")


# ================================================================= the sandbox rule, the home, merge_closed

class SandboxAndHome(_Base):
    T = tid(18)

    def test_sandboxed_run_reads_no_codex(self):
        self.app()
        self.terminal(self.T, self.busy(self.T))
        d = tid(19)
        self.rollout(d, self.busy(d, originator=codex.DESKTOP_ORIGINATORS[0], source="vscode"))
        os.environ.pop("SESSION_MAP_CODEX_DIR")
        os.environ["CODEX_HOME"] = str(self.home)
        self.assertTrue(codex.sandboxed())
        snap, problems = self.join()
        self.assertEqual((self.codex_nodes(snap), problems), ([], []))
        os.environ["SESSION_MAP_CODEX_DIR"] = str(self.home)  # ruler: pointing Codex at it turns it on
        self.assertFalse(codex.sandboxed())
        snap, _ = self.join()
        self.assertEqual(len(self.codex_nodes(snap)), 2)

    def test_each_fixture_variable_sandboxes(self):
        os.environ.pop("SESSION_MAP_CODEX_DIR")
        keep = {k: os.environ.pop(k) for k in ("SESSION_MAP_REGISTRY_DIR", "SESSION_MAP_PROJECTS_DIR",
                                               "SESSION_MAP_PS_JSON")}
        self.assertFalse(codex.sandboxed())
        for k, v in keep.items():
            with self.subTest(k), mock.patch.dict(os.environ, {k: v}):
                self.assertTrue(codex.sandboxed())

    def test_codex_dir_order(self):
        os.environ["CODEX_HOME"] = str(self.d / "from-codex-home")
        self.assertEqual(Path(codex.codex_dir()), self.home)
        os.environ.pop("SESSION_MAP_CODEX_DIR")
        self.assertEqual(Path(codex.codex_dir()), self.d / "from-codex-home")
        os.environ.pop("CODEX_HOME")
        self.assertEqual(Path(codex.codex_dir()), Path(os.environ["HOME"]) / ".codex")

    def test_no_sessions_folder_is_quiet(self):
        shutil.rmtree(self.home / "sessions")
        snap, problems = self.join()
        self.assertEqual((self.codex_nodes(snap), problems), ([], []))

    def test_merge_closed_keeps_a_vanished_codex_card(self):
        wait = codex.PERMISSION_WAIT_S + 10
        self.terminal(self.T, self.busy(self.T) + [self.exec_call(wait)])
        prev, _ = self.join()
        n = self.node(prev, self.T)
        self.assertEqual((n["state"], n["inferred"]), ("waiting", True))
        self.comm.clear()
        self.env_lines.clear()
        self.write_ps()
        cur, _ = self.join()
        self.assertEqual(self.codex_nodes(cur), [])  # process gone: no node from the snapshot itself
        memory: dict = {}
        merged = snapshot.merge_closed(prev, cur, self.now, memory=memory, hidden=set())
        self.sweep(merged)
        kept = self.node(merged, self.T)
        self.assertEqual(kept["state"], "closed")
        self.assertEqual(kept["agent"], "codex")
        for k in ("inferred", "waiting_for", "pane", "guessed"):
            self.assertNotIn(k, kept)
        later = snapshot.merge_closed(merged, cur, self.now + snapshot.GONE_S, memory=memory, hidden=set())
        self.assertEqual(self.codex_nodes(later), [])


# ================================================================= malformed input

class Malformed(_Base):

    def test_bad_lines_and_a_missing_header(self):
        good, bad, headless = tid(20), tid(21), tid(22)
        self.rollout(good, self.busy(good, cwd="/tmp/proj-good") + [self.complete(5)])
        self.proc(pid=601, cwd="/tmp/proj-good")
        recs = self.busy(bad, cwd="/tmp/proj-bad")
        self.rollout(bad, recs, raw_tail=b'{not json\n[1, 2]\n"a string"\nnull\n{"type": 7, "payload": "x"}\n'
                                         b'{"type": "event_msg", "payload": {"type": "task_complete"}}\n')
        self.proc(pid=602, cwd="/tmp/proj-bad")
        self.rollout(headless, [self.ctx(30, cwd="/tmp/proj-headless"), self.started(30), self.user(30)])
        self.proc(pid=603, cwd="/tmp/proj-headless")
        self.secrets.add(headless)
        (self.home / "session_index.jsonl").write_text('{broken\n["x"]\n' + json.dumps(
            {"id": good, "thread_name": "The good one", "updated_at": "2026-01-01T00:00:00.000000Z"}) + "\n",
            encoding="utf-8")
        snap, problems = self.join()
        self.assertLessEqual(len(problems), 1, problems)
        self.assertFalse(any(str(self.d) in p or "/tmp/" in p for p in problems), problems)
        n = self.node(snap, good)
        self.assertEqual((n["state"], n["title"]), ("done", "The good one"))
        self.assertNotIn(x_id(headless), [m["id"] for m in snap["nodes"]])

    def test_empty_and_binary_files(self):
        good = tid(23)
        self.terminal(good, self.busy(good))
        folder = next((self.home / "sessions").rglob("rollout-*.jsonl")).parent
        (folder / f"rollout-2026-01-01T00-00-00-{tid(24)}.jsonl").write_bytes(b"")
        (folder / f"rollout-2026-01-01T00-00-01-{tid(25)}.jsonl").write_bytes(b"\x00\xff\xfe garbage \x80\n")
        snap, problems = self.join()
        self.assertLessEqual(len(problems), 1, problems)
        self.assertEqual([n["id"] for n in self.codex_nodes(snap)], [x_id(good)])


if __name__ == "__main__":
    unittest.main()
