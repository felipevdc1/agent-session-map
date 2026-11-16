"""Tests for session_map.snapshot: the state rule, the join, the cache, merge_closed, embed and the CLI.

Every source points at a temporary folder by environment (SESSION_MAP_REGISTRY_DIR, SESSION_MAP_PROJECTS_DIR,
SESSION_MAP_PS_JSON, SESSION_MAP_TERN_JSON, SESSION_MAP_ADAPTERS) and HOME is swapped in this process.
Live sessions are real `sleep` processes, so `os.kill(pid, 0)` answers for real. Records are hand-written in
the shape Claude Code writes; no real conversation text is copied here. The real registry folder is listed
before and after the suite, and every snapshot the suite produces is swept for paths, UUIDs, KEY=value text
and keys outside the documented schema.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# ---------------------------------------------------------------- the real registry, listed before anything runs

_REAL_SESSIONS = Path(os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")) / "sessions"


def _listing(p: Path) -> set:
    try:
        return {f.name for f in p.iterdir()}
    except OSError:
        return set()


SESSIONS_BEFORE = _listing(_REAL_SESSIONS)

TMP = Path(tempfile.mkdtemp(prefix="session-map-test-"))
_ENV_BEFORE = dict(os.environ)
os.environ["HOME"] = str(TMP / "home")
(TMP / "home").mkdir()
for _k in list(os.environ):
    if _k.startswith("SESSION_MAP_") or _k in ("CLAUDE_CONFIG_DIR",):
        os.environ.pop(_k, None)

from session_map import orchestrator, snapshot, transcript  # noqa: E402
from session_map.orchestrator import Step, Tab  # noqa: E402
from session_map.snapshot import Config  # noqa: E402

CWD = str(TMP / "work")
SNAPS: list = []  # every snapshot the suite produces, for the invariant sweep
_PROCS: list = []
_WRITTEN_NAMES: set = set()  # registry file names this suite writes (never into the real folder)


def tearDownModule():
    try:
        bad = []
        for s in SNAPS:
            bad += violations(s)
        if not SNAPS:
            raise AssertionError("the invariant sweep saw no snapshot")
        if bad:
            raise AssertionError(f"invariant sweep over {len(SNAPS)} snapshots: {bad[:5]}")
        added = _listing(_REAL_SESSIONS) - SESSIONS_BEFORE
        ours = {f"{p.pid}.json" for p in _PROCS} | _WRITTEN_NAMES
        if added & ours:
            raise AssertionError(f"the suite wrote into the real registry: {sorted(added & ours)}")
    finally:
        os.environ.clear()
        os.environ.update(_ENV_BEFORE)
        shutil.rmtree(TMP, ignore_errors=True)
        for p in _PROCS:
            with contextlib.suppress(Exception):
                p.kill()
                p.wait(timeout=5)


def alive_pid() -> int:
    p = subprocess.Popen(["sleep", "600"])
    _PROCS.append(p)
    return p.pid


def dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


# ---------------------------------------------------------------- the invariant checker

R_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
R_ASSIGN = re.compile(r"\b[A-Z][A-Z0-9_]*=")
HOME_PREFIX = "/" + "Users/"  # built from two pieces so the fixture itself is not a literal home path
TOP_KEYS = {"version", "generated_at", "nodes", "lines", "problems_n", "hidden_n", "unknown_format"}
NODE_BASE = {"id", "kind", "title", "where", "state", "since"}
NODE_OPTIONAL = {"pane", "waiting_for", "guessed"}
LINE_KEYS = {"from", "to", "kind"}


def strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for k, v in x.items():
            yield k
            yield from strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from strings(v)


def violations(snap) -> list:
    bad = [s for s in strings(snap)
           if HOME_PREFIX in s or "~/" in s or R_UUID.search(s) or R_ASSIGN.search(s)]
    if isinstance(snap, dict):
        bad += [f"top key {k}" for k in set(snap) - TOP_KEYS]
        for n in snap.get("nodes") or []:
            bad += [f"node key {k}" for k in set(n) - NODE_BASE - NODE_OPTIONAL]
            bad += [f"node lacks {k}" for k in NODE_BASE - set(n)]
        for ln in snap.get("lines") or []:
            bad += [f"line key {k}" for k in set(ln) ^ LINE_KEYS]
    return bad


def keep(snap):
    SNAPS.append(snap)
    return snap


# ---------------------------------------------------------------- records in Claude Code's shape

def now_iso(ago_s=0.0) -> str:
    """A record timestamp in Claude Code's shape ("...Z"), `ago_s` seconds ago."""
    t = time.time() - ago_s
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".%03dZ" % int((t % 1) * 1000)


def _base(kind, ts=None):
    return {"parentUuid": None, "isSidechain": False, "type": kind, "uuid": str(uuid.uuid4()),
            "timestamp": ts or now_iso(), "sessionId": "s", "cwd": CWD, "version": snapshot.SEEN_VERSIONS[0]}


def aged(recs, ago_s):
    """The same records stamped `ago_s` seconds ago."""
    return [dict(r, timestamp=now_iso(ago_s)) if "timestamp" in r else r for r in recs]


def r_user(text="ok"):
    r = _base("user")
    r["message"] = {"role": "user", "content": text}
    return r


def r_asst(text=None, uses=(), ts=None):
    r = _base("assistant", ts)
    c = [{"type": "tool_use", "id": f"toolu_{i}", "name": n, "input": inp} for i, (n, inp) in enumerate(uses)]
    if text is not None:
        c.append({"type": "text", "text": text})
    r["message"] = {"role": "assistant", "content": c}
    return r


def r_result(text="done"):
    r = _base("user")
    r["message"] = {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_0", "content": text}]}
    return r


def r_end(pending=None):
    r = _base("system")
    r.update({"subtype": "turn_duration", "durationMs": 1000})
    if pending is not None:
        r["pendingBackgroundAgentCount"] = pending
    return r


def r_raw(kind, **extra):
    r = {"type": kind, "uuid": str(uuid.uuid4()), "timestamp": now_iso()}
    r.update(extra)
    return r


def r_title(kind, value):
    field = {"custom-title": "customTitle", "ai-title": "aiTitle"}[kind]
    return {"type": kind, field: value, "sessionId": "s"}


TAILS = {
    "cost-state": r_raw("cost-state", costUSD=0.1),
    "file-history-snapshot": r_raw("file-history-snapshot", snapshot={"trackedFileBackups": {}}),
    "queue-operation": r_raw("queue-operation", operation="enqueue"),
    "away_summary": r_raw("system", subtype="away_summary", isSidechain=False),
    "stop_hook_summary": r_raw("system", subtype="stop_hook_summary", isSidechain=False, hookCount=1),
    "local_command": r_raw("system", subtype="local_command", isSidechain=False, content="x"),
    "bridge_status": r_raw("system", subtype="bridge_status", isSidechain=False),
    "informational": r_raw("system", subtype="informational", isSidechain=False, content="note"),
    "attachment": r_raw("attachment", isSidechain=False, attachment={"type": "todo_reminder"}),
    "new-kind": r_raw("new-kind", isSidechain=False),
}

BG = [r_user("do it"), r_asst(uses=[("Bash", {"command": "sleep 99", "run_in_background": True})]),
      r_result("Command running in background with ID: bx12"), r_asst("Running in the background.")]
RETURNED = r_user("<task-notification>\n<task-id>bx12</task-id>\n</task-notification>")


# ---------------------------------------------------------------- a fake orchestrator adapter

class FakeAdapter:
    name = "Orch"
    env_var = "ORCH_HANDLE"
    env_pattern = r"h_[0-9a-f]+"

    def __init__(self):
        self.step_list: list = []
        self.calls: list = []
        self.focused: list = []
        self.problems: list = []
        self.raise_on_steps = False

    def steps(self, live_session_ids, live_handles):
        self.calls.append((set(live_session_ids), set(live_handles)))
        if self.raise_on_steps:
            raise RuntimeError("the orchestrator broke at " + "/some/disk/path")
        return list(self.step_list)

    def focus(self, handle):
        self.focused.append(handle)
        return True


FAKE = FakeAdapter()
_FAKE_MOD = types.ModuleType("_session_map_test_adapter")
_FAKE_MOD.adapter = FAKE
sys.modules[_FAKE_MOD.__name__] = _FAKE_MOD
FAKE_ENTRY = f"{_FAKE_MOD.__name__}:adapter"


def handle() -> str:
    return "h_" + uuid.uuid4().hex


def step(key, sids=(), label="example · plan", verdict=None, pending=False, depends_on=(), manager=None,
         handles=None):
    handles = list(handles or [None] * len(sids))
    return Step(key=key, label=label, tabs=tuple(Tab(s, h) for s, h in zip(sids, handles)), verdict=verdict,
                pending=pending, depends_on=tuple(depends_on), manager_session_id=manager)


def next_id(index, key):
    return f"n:{index}:{hashlib.sha256(key.encode()).hexdigest()[:12]}"


# ---------------------------------------------------------------- a temporary world

class World:
    def __init__(self):
        self.d = Path(tempfile.mkdtemp(dir=TMP))
        self.reg = self.d / "sessions"
        self.proj = self.d / "projects"
        self.ps_json = self.d / "ps.json"
        self.tern_json = self.d / "tern.json"
        for p in (self.reg, self.proj):
            p.mkdir(parents=True)
        self.ps_lines: list = []
        self.blocks: list = []
        self.steps = None  # a list of Step turns the fake adapter on for this world
        self.env = {"SESSION_MAP_REGISTRY_DIR": str(self.reg), "SESSION_MAP_PROJECTS_DIR": str(self.proj),
                    "SESSION_MAP_PS_JSON": str(self.ps_json), "SESSION_MAP_TERN_JSON": str(self.tern_json)}
        self.write()

    def write(self):
        self.ps_json.write_text(json.dumps(self.ps_lines), encoding="utf-8")
        self.tern_json.write_text(json.dumps({"sessions": [{"id": 1, "name": "s", "tabs": [
            {"id": 10 + i, "number": n, "blocks": [b]} for i, (n, b) in enumerate(self.blocks)]}],
            "detached": []}), encoding="utf-8")

    def registry_file(self, name: str, text: str):
        _WRITTEN_NAMES.add(name)
        (self.reg / name).write_text(text, encoding="utf-8")

    def session(self, pid=None, sid=None, cwd=None, status="idle", kind="interactive", name="a session",
                pane=None, number=None, tern_title="✳ tern title", handle=None, **extra):
        pid = pid or alive_pid()
        sid = sid or str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        data = {"pid": pid, "sessionId": sid, "cwd": cwd or CWD, "startedAt": 1700000000000,
                "procStart": "x", "version": snapshot.SEEN_VERSIONS[0], "kind": kind, "entrypoint": "cli", "name": name,
                "nameSince": 1, "nameSource": "user", "status": status, "updatedAt": now_ms,
                "statusUpdatedAt": now_ms}
        data.update(extra)
        self.registry_file(f"{pid}.json", json.dumps(data))
        line = f"{pid} /usr/local/bin/claude FAKE_SECRET=abc123"
        if pane is not None:
            line += f" TERN_PANE={pane}"
            self.blocks.append((number or 1, {"id": pane, "title": tern_title, "cwd": CWD, "program": "zsh",
                                              "live": True}))
        if handle is not None:
            line += f" ORCH_HANDLE={handle}"
        self.ps_lines.append(line + " SHELL=/bin/zsh")
        self.write()
        return pid, sid

    def conversation(self, sid, recs, cwd=None, raw: bytes | None = None):
        p = self.proj / transcript.encode_cwd(cwd or CWD) / f"{sid}.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        if raw is not None:
            p.write_bytes(raw)
        else:
            p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in recs), encoding="utf-8")
        return p

    def helper(self, sid, aid, meta, recs=(), age_s=0.0, cwd=None):
        d = self.proj / transcript.encode_cwd(cwd or CWD) / sid / "subagents"
        d.mkdir(parents=True, exist_ok=True)
        m = d / f"agent-{aid}.meta.json"
        m.write_text(json.dumps(meta), encoding="utf-8")
        j = d / f"agent-{aid}.jsonl"
        j.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
        t = time.time() - age_s
        os.utime(m, (t, t))
        os.utime(j, (t, t))
        return j

    @contextlib.contextmanager
    def active(self):
        env = dict(self.env)
        if self.steps is not None:
            env["SESSION_MAP_ADAPTERS"] = FAKE_ENTRY
            FAKE.step_list = list(self.steps)
        before = {k: os.environ.get(k) for k in list(env) + ["SESSION_MAP_ADAPTERS"]}
        os.environ.pop("SESSION_MAP_ADAPTERS", None)
        os.environ.update(env)
        try:
            yield
        finally:
            for k, v in before.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def snap(self, now=None):
        with self.active():
            r, problems = snapshot._join(now)
        keep(r)
        return r, problems


def _drop_field(m, field):
    """Drop `field` from every registry file of the world."""
    for f in m.reg.glob("*.json"):
        d = json.loads(f.read_text(encoding="utf-8"))
        d.pop(field, None)
        f.write_text(json.dumps(d), encoding="utf-8")
    return True


def by_id(snap):
    return {n["id"]: n for n in snap["nodes"]}


# ---------------------------------------------------------------- the state grid

class StateGrid(unittest.TestCase):
    """session_state, cell by cell: registry status x step verdict x tail shape x config.
    The tails are real files read by read_tail."""

    MARKERS = Config(done_lines=("All done.",), wait_marks=("[needs you]",))

    @classmethod
    def setUpClass(cls):
        snapshot.forget()
        cls.m = World()
        q = "First step done.\nShall I go on?"
        cls.tails = {
            "middle": [r_user("do it"), r_asst(uses=[("Read", {"file_path": "x"})]), r_result()],
            "end_mark": [r_user("do it"), r_asst("Plan ready.\n[needs you] Shall I go with option 1?"), r_end()],
            "end_q_last": [r_user("do it"), r_asst(q), r_end()],
            "end_q_paren": [r_user("do it"), r_asst(
                "Changed the file.\n(Want another pass? Just say so.)\nSecond line.\nThird line."), r_end()],
            "end_done_line": [r_user("do it"), r_asst("Shall I go on?\nAll done."), r_end()],
            "end_pending": [r_user("do it"), r_asst("Started the helper."), r_end(pending=1)],
            "end_plain": [r_user("do it"), r_asst("Done, all green."), r_end()],
            "user_after": [r_user("do it"), r_asst("Done."), r_end(), r_user("and now")],
            "asst_no_end": [r_user("do it"), r_asst("Starting.")],
            "no_text": [r_user("do it")],
        }
        cls.files = {}
        for name, recs in cls.tails.items():
            cls.files[name] = cls.m.conversation(str(uuid.uuid4()), recs)
        cls.files["missing"] = cls.m.proj / "does-not-exist.jsonl"

    def state(self, reg, st, tail, config=None):
        with self.m.active():
            return snapshot.session_state({}, reg, st, snapshot.read_tail(self.files[tail]), config)

    REG = {"absent": None, "dead": {"alive": False, "status": "busy"},
           "waiting": {"alive": True, "status": "waiting", "waitingFor": "input needed"},
           "busy": {"alive": True, "status": "busy"}, "idle": {"alive": True, "status": "idle"},
           "shell": {"alive": True, "status": "shell"}}
    STEPS = {"none": None, "running": step("k"), "done": step("k", verdict="done"),
             "closed": step("k", verdict="closed"), "waiting": step("k", verdict="waiting")}

    # idle (and shell) with no deciding step, default config: the conversation's tail decides.
    BY_TAIL = [
        ("middle", "stopped", False),
        ("end_mark", "waiting", True),
        ("end_q_last", "waiting", True),
        ("end_q_paren", "waiting", True),
        ("end_done_line", "waiting", True),  # the done line counts only when set
        ("end_pending", "working", False),
        ("end_plain", "done", False),
        ("user_after", "stopped", False),
        ("asst_no_end", "stopped", False),
        ("no_text", "stopped", False),
        ("missing", "stopped", False),
    ]
    BY_STEP = [("done", "done"), ("closed", "closed"), ("waiting", "waiting")]

    def test_registry_dominates(self):
        for reg, expected in (("absent", "closed"), ("dead", "closed"), ("waiting", "waiting"),
                              ("busy", "working")):
            for st in self.STEPS:
                for tail in self.files:
                    for config in (None, self.MARKERS):
                        with self.subTest(reg=reg, step=st, tail=tail, config=config):
                            self.assertEqual(self.state(self.REG[reg], self.STEPS[st], tail, config)["state"],
                                             expected)

    def test_waiting_for_comes_from_the_registry_in_words(self):
        for value, words in (("input needed", "question or form"), ("permission request", "permission request"),
                             ("worker request", "permission request"), ("dialog open", "dialog open"),
                             ("something new", "your decision")):
            r = self.state({"alive": True, "status": "waiting", "waitingFor": value}, None, "end_plain")
            self.assertEqual(r, {"state": "waiting", "waiting_for": words})

    def test_stopped_by_tail(self):
        for reg in ("idle", "shell"):
            for st in ("none", "running"):
                for tail, expected, guessed in self.BY_TAIL:
                    with self.subTest(reg=reg, step=st, tail=tail):
                        r = self.state(self.REG[reg], self.STEPS[st], tail)
                        self.assertEqual(r["state"], expected)
                        self.assertEqual(bool(r.get("guessed")), guessed)
                        self.assertNotIn("waiting_for", r)

    def test_stopped_by_step(self):
        for reg in ("idle", "shell"):
            for st, expected in self.BY_STEP:
                for tail in self.files:
                    for config in (None, self.MARKERS):
                        with self.subTest(reg=reg, step=st, tail=tail):
                            self.assertEqual(self.state(self.REG[reg], self.STEPS[st], tail, config)["state"],
                                             expected)

    def test_markers_both_ways(self):
        """The two marker cells, once with the empty default and once with markers set."""
        idle = self.REG["idle"]
        cells = [("end_done_line", None, {"state": "waiting", "guessed": True}),
                 ("end_mark", None, {"state": "waiting", "guessed": True}),
                 ("end_done_line", self.MARKERS, {"state": "done"}),
                 ("end_mark", self.MARKERS, {"state": "waiting"})]
        for tail, config, expected in cells:
            with self.subTest(tail=tail, config=config):
                self.assertEqual(self.state(idle, None, tail, config), expected)
        # a set marker that is absent from the text changes nothing
        only_done = Config(done_lines=("All done.",))
        self.assertEqual(self.state(idle, None, "end_q_last", only_done), {"state": "waiting", "guessed": True})
        only_mark = Config(wait_marks=("[needs you]",))
        self.assertEqual(self.state(idle, None, "end_plain", only_mark), {"state": "done"})

    def test_read_config_from_env(self):
        old = {k: os.environ.get(k) for k in ("SESSION_MAP_DONE_LINES", "SESSION_MAP_WAIT_MARKS")}
        try:
            os.environ["SESSION_MAP_DONE_LINES"] = "All done.| All set. |"
            os.environ["SESSION_MAP_WAIT_MARKS"] = "[needs you]"
            self.assertEqual(snapshot.read_config(),
                             Config(done_lines=("All done.", "All set."), wait_marks=("[needs you]",)))
            os.environ.pop("SESSION_MAP_DONE_LINES")
            os.environ.pop("SESSION_MAP_WAIT_MARKS")
            self.assertEqual(snapshot.read_config(), Config())
        finally:
            for k, v in old.items():
                if v is not None:
                    os.environ[k] = v

    def test_edge_cells(self):
        idle = self.REG["idle"]
        self.assertEqual(self.state(idle, self.STEPS["done"], "end_mark")["state"], "done")
        self.assertEqual(self.state(self.REG["busy"], None, "missing")["state"], "working")
        self.assertEqual(self.state(self.REG["dead"], None, "middle")["state"], "closed")
        self.assertEqual(self.state(idle, None, "end_pending")["state"], "working")
        self.assertEqual(self.state(idle, None, "asst_no_end")["state"], "stopped")
        self.assertEqual(self.state(idle, None, "end_done_line", self.MARKERS)["state"], "done")


class StuckSession(unittest.TestCase):
    """busy with the conversation untouched for more than 15 min is stopped; a working card's `since` is the
    conversation's last change (statusUpdatedAt does not move during a busy run)."""

    NOW = 1_800_000_000.0

    def r(self, status, changed, st=None):
        return snapshot.session_state({"now": self.NOW}, {"alive": True, "status": status}, st,
                                      {"exists": changed is not None, "last": None, "text": None,
                                       "changed": changed})

    def test_grid(self):
        cases = [
            ("busy", self.NOW - 16 * 60, {"state": "stopped", "stuck": True}),
            ("busy", self.NOW - 14 * 60, {"state": "working", "since": self.NOW - 14 * 60}),
            ("busy", None, {"state": "working"}),  # a new session, no conversation yet: still turning
            ("idle", self.NOW - 16 * 60, {"state": "stopped"}),
            ("waiting", self.NOW - 3600, {"state": "waiting", "waiting_for": "your decision"}),
        ]
        for status, changed, expected in cases:
            with self.subTest(status=status, changed=changed):
                self.assertEqual(self.r(status, changed), expected)

    def test_since_of_a_working_card_is_the_conversation(self):
        m = World()
        pid, sid = m.session(status="busy", statusUpdatedAt=1)
        recs = aged([r_user("do it"), r_asst("Starting.")], 120)
        m.conversation(sid, recs + [r_title("ai-title", "a title")])  # untimestamped metadata after: not a change
        t = transcript.parse_iso(recs[-1]["timestamp"]).timestamp()
        node = by_id(m.snap()[0])[f"p:{pid}"]
        self.assertEqual((node["state"], node["since"]), ("working", snapshot._iso(t)))

    def test_long_helper_keeps_the_mother_turning(self):
        m = World()
        pid, sid = m.session(status="busy")
        m.conversation(sid, aged([r_user("do it"), r_asst("Started.")], 40 * 60))
        m.helper(sid, "a5555555555555555", {"agentType": "t", "description": "long"},
                 aged([r_asst("working")], 30), age_s=30)
        self.assertEqual(by_id(m.snap()[0])[f"p:{pid}"]["state"], "working")
        m2 = World()  # the same helper silent for 20 min: the mother is stuck, grey
        pid2, sid2 = m2.session(status="busy")
        m2.conversation(sid2, aged([r_user("do it"), r_asst("Started.")], 40 * 60))
        m2.helper(sid2, "a5555555555555556", {"agentType": "t", "description": "long"},
                  aged([r_asst("working")], 20 * 60), age_s=30)
        self.assertEqual(by_id(m2.snap()[0])[f"p:{pid2}"]["state"], "stopped")

    def test_mtime_without_timestamp_is_not_a_change(self):
        """Metadata appended without a timestamp moves the mtime, not the last change."""
        m = World()
        pid, sid = m.session(status="busy")
        path = m.conversation(sid, aged([r_user("do it"), r_asst("Running.")], 40 * 60))
        with open(path, "a", encoding="utf-8") as fh:
            for kind in ("last-prompt", "mode", "session-owner", "tools-used"):
                fh.write(json.dumps({"type": kind, "sessionId": sid}) + "\n")
            fh.write(json.dumps(r_title("ai-title", "new title")) + "\n")
        self.assertEqual(by_id(m.snap()[0])[f"p:{pid}"]["state"], "stopped")

    def test_background_command_stops_turning(self):
        """shell/idle with a background command that never returned, conversation untouched for more than
        15 min: stopped, never working forever. Counted for every session, whatever its cwd."""
        bg = [r_user("do it"), r_asst(uses=[("Bash", {"command": "npm run dev", "run_in_background": True})]),
              r_result("Command running in background with ID: bx99"), r_asst("Server is up."), r_end()]
        for status in ("shell", "idle"):
            for ago, expected in ((10 * 3600, "stopped"), (16 * 60, "stopped"), (60, "working")):
                for cwd in (None, str(TMP / "elsewhere")):
                    with self.subTest(status=status, ago=ago, cwd=cwd):
                        m = World()
                        pid, sid = m.session(status=status, cwd=cwd)
                        m.conversation(sid, aged(bg, ago), cwd=cwd)
                        self.assertEqual(by_id(m.snap()[0])[f"p:{pid}"]["state"], expected)
        r = snapshot.session_state({"now": self.NOW}, {"alive": True, "status": "idle"}, None,
                                   {"last": r_end(pending=1), "text": "x", "changed": self.NOW - 3600})
        self.assertEqual(r, {"state": "stopped", "stuck": True})


class AgeCut(unittest.TestCase):
    """done/stopped with no change for more than 8 h is left out (with its helpers) and counted."""

    def test_cut(self):
        m = World()
        old_ms = int((time.time() - 9 * 3600) * 1000)
        old = old_ms / 1000
        nine = 9 * 3600
        finished, sf = m.session(statusUpdatedAt=old_ms)
        os.utime(m.conversation(sf, aged([r_user("x"), r_asst("Done."), r_end()], nine)), (old, old))
        # a helper that would show on its own (finished, meta just rewritten), its transcript as old as the mother's
        jl = m.helper(sf, "a4444444444444444", {"agentType": "t", "description": "help", "finished": True},
                      aged([r_asst("ok")], nine))
        os.utime(jl, (old, old))
        stopped, _ = m.session(statusUpdatedAt=old_ms)
        waiting, se = m.session(statusUpdatedAt=old_ms)
        m.conversation(se, aged([r_user("x"), r_asst("Go on?"), r_end()], nine))
        stuck, sp = m.session(status="busy", statusUpdatedAt=old_ms)
        m.conversation(sp, aged([r_user("x"), r_asst("Running.")], nine))
        # untimestamped metadata just appended (fresh mtime) does not save a 9 h old card
        background, sb = m.session(kind="bg", statusUpdatedAt=old_ms)
        m.conversation(sb, aged([r_user("x"), r_asst("Done."), r_end()], nine)
                       + [{"type": "last-prompt", "sessionId": sb}, r_title("ai-title", "box")])
        new, _ = m.session(status="busy", statusUpdatedAt=old_ms)
        mgr, sg = m.session(pane=1)
        orch, so = m.session(statusUpdatedAt=old_ms)
        m.steps = [step("old", [so], verdict="done", manager=sg)]
        recent, sr = m.session()
        m.conversation(sr, [r_user("x"), r_asst("Done."), r_end()])
        r, _ = m.snap()
        nodes = by_id(r)
        self.assertEqual(r["hidden_n"], 5)
        for pid in (finished, stopped, stuck, orch, background):
            self.assertNotIn(f"p:{pid}", nodes)
        self.assertFalse([n for n in r["nodes"] if n["kind"] == "helper"])
        self.assertEqual(nodes[f"p:{waiting}"]["state"], "waiting")
        self.assertEqual(nodes[f"p:{new}"]["state"], "working")
        self.assertEqual(nodes[f"p:{recent}"]["state"], "done")
        self.assertFalse([x for x in r["lines"] if f"p:{orch}" in (x["from"], x["to"])])
        self.assertEqual(violations(r), [])


class AcceptList(unittest.TestCase):
    """Whatever raw record trails the turn end, the last useful record is the turn end."""

    def test_every_raw_tail_finds_the_end(self):
        m = World()
        for name, rec in TAILS.items():
            with self.subTest(tail=name):
                path = m.conversation(str(uuid.uuid4()), [r_user("do it"), r_asst("Done."), r_end(), rec, dict(rec)])
                with m.active():
                    c = snapshot.read_tail(path)
                    self.assertEqual((c["last"]["type"], c["last"].get("subtype")), ("system", "turn_duration"))
                    r = snapshot.session_state({}, {"alive": True, "status": "idle"}, None, c)
                self.assertEqual(r["state"], "done")


class TurnSettled(unittest.TestCase):
    """A conversation that ends in a turn_duration: settled (the turn is over and nothing it started still runs)
    exactly when the state rule (no markers, no `?`) does not say working."""

    CASES = {  # name: (records, settled)
        "no_pending": ([r_user("do it"), r_asst("Done."), r_end()], True),
        "end_with_zero_field": ([r_user("do it"), r_asst("Done."), r_end(pending=0)], True),
        "helper_pending": ([r_user("do it"), r_asst("Started."), r_end(pending=2)], False),
        "background_command": (BG + [r_end()], False),
        "background_returned": (BG + [RETURNED, r_asst("It returned."), r_end()], True),
    }

    def test_settled_is_not_working(self):
        m = World()
        seen = set()
        for name, (recs, settled) in self.CASES.items():
            with self.subTest(case=name):
                path = m.conversation(str(uuid.uuid4()), recs)
                with m.active():
                    r = snapshot.session_state({}, {"alive": True, "status": "idle"}, None, snapshot.read_tail(path))
                self.assertIn(r["state"], ("done", "working"))
                self.assertEqual(settled, r["state"] != "working")
                seen.add(settled)
        self.assertEqual(seen, {True, False})  # the table holds both answers


# ---------------------------------------------------------------- the snapshot: shape, who becomes a card, lines

class Shape(unittest.TestCase):

    def setUp(self):
        snapshot.forget()

    def test_first_time_empty(self):
        m = World()
        m.tern_json.unlink()
        r, problems = m.snap()  # an empty registry folder: a valid, quiet, empty snapshot
        self.assertEqual((r["nodes"], r["lines"], r["problems_n"], problems), ([], [], 0, []))
        self.assertRegex(r["version"], r"^[0-9a-f]{12}$")
        self.assertEqual(set(r), {"version", "generated_at", "nodes", "lines", "problems_n", "hidden_n"})
        self.assertEqual(r["hidden_n"], 0)

    def test_unknown_format(self):
        """Only when true: a missing folder, a file with no readable pid, or a missing field in an unseen
        version raise it; a missing field in a seen version or a half-written file do not."""
        cases = {
            "folder_gone": (lambda m: shutil.rmtree(m.reg), True),
            "no_pid": (lambda m: m.registry_file("abc.json", json.dumps({"status": "idle"})), True),
            "not_an_object": (lambda m: m.registry_file("12.json", "[1]"), True),
            "field_missing_new_version": (lambda m: m.session(version="9.9.9", kind=None) and
                                          _drop_field(m, "kind"), True),
            "field_missing_seen_version": (lambda m: m.session() and _drop_field(m, "kind"), False),
            "half_written": (lambda m: m.registry_file("13.json", '{"pid": 1'), False),
            "all_fine": (lambda m: m.session(), False),
        }
        for name, (prepare, expected) in cases.items():
            with self.subTest(case=name):
                m = World()
                prepare(m)
                r, _ = m.snap()
                self.assertEqual(r.get("unknown_format", "absent"), True if expected else "absent")
                self.assertGreaterEqual(r["problems_n"], 1 if expected and name != "folder_gone" else 0)

    def test_shapes_and_lines(self):
        m = World()
        mgr_pid, mgr = m.session(pane=7, number=2, name="api · review", status="waiting", waitingFor="input needed")
        tab_pid, tab = m.session(name="registry name", status="busy")
        bg_pid, bg = m.session(kind="bg", name="nightly report")
        out_pid, _ = m.session(cwd=str(TMP / "other-tool"), name="other tool", status="busy")
        new_pid, _ = m.session(name="new session")
        tmux_pid, _ = m.session(name="in tmux", tmux="main")
        m.conversation(bg, [r_user("x"), r_asst("Done.\nWant more?"), r_end()])
        m.steps = [step("s1", [tab], label="example · plan", manager=mgr),
                   step("s2", label="example · build", pending=True, depends_on=["s1"], manager=mgr),
                   step("s3", label="example · wrap-up", pending=True, manager=mgr)]
        m.helper(tab, "a1111111111111111", {"agentType": "general-purpose", "description": "Reviews the plan"})
        m.helper(tab, "a2222222222222222", {"agentType": "Explore", "description": "Reads the code",
                                            "parentAgentId": "a1111111111111111", "finished": True})
        m.helper(tab, "a3333333333333333", {"agentType": "x", "description": "Teammate", "teamName": "crew",
                                            "name": "teammate"})
        r, problems = m.snap()
        self.assertEqual(problems, [])
        nodes = by_id(r)
        for n in r["nodes"]:
            self.assertTrue(NODE_BASE <= set(n) <= NODE_BASE | NODE_OPTIONAL, n)
            self.assertRegex(n["id"], r"^(p:\d+|a:[0-9a-f]{8}|n:\d+:[0-9a-f]{12})$")
            if "pane" in n:
                self.assertEqual(n["kind"], "tern")
        g = nodes[f"p:{mgr_pid}"]
        self.assertEqual((g["kind"], g["where"], g["state"], g["pane"], g["waiting_for"], g["title"]),
                         ("tern", "Tern · tab 2", "waiting", 7, "question or form", "api · review"))
        a = nodes[f"p:{tab_pid}"]
        self.assertEqual((a["kind"], a["title"], a["where"], a["state"]),
                         ("orchestrated", "example · plan", "Orch", "working"))
        b = nodes[f"p:{bg_pid}"]
        self.assertEqual((b["kind"], b["where"], b["state"], b.get("guessed")),
                         ("background", "in the background", "waiting", True))
        self.assertEqual(nodes[f"p:{tmux_pid}"]["kind"], "background")
        self.assertEqual((nodes[f"p:{out_pid}"]["kind"], nodes[f"p:{out_pid}"]["where"]), ("other", "other terminal"))
        self.assertEqual((nodes[f"p:{new_pid}"]["kind"], nodes[f"p:{new_pid}"]["state"]), ("other", "stopped"))
        n2, n3 = next_id(0, "s2"), next_id(0, "s3")
        self.assertEqual(nodes[n2], {"id": n2, "kind": "next", "title": "example · build", "where": "Orch · next",
                                     "state": "next", "since": None})
        h = {n["title"]: n for n in r["nodes"] if n["kind"] == "helper"}
        self.assertEqual((h["Reviews the plan"]["state"], h["Reviews the plan"]["where"]), ("working", "general-purpose"))
        self.assertEqual(h["Reads the code"]["state"], "done")
        self.assertEqual(h["Teammate"]["where"], "team crew")
        lines = {(x["from"], x["to"], x["kind"]) for x in r["lines"]}
        self.assertIn((f"p:{mgr_pid}", f"p:{tab_pid}", "opened"), lines)
        self.assertIn((f"p:{tab_pid}", n2, "follows"), lines)
        self.assertIn((f"p:{mgr_pid}", n3, "follows"), lines)  # no dependency: from the manager
        self.assertIn((f"p:{tab_pid}", h["Reviews the plan"]["id"], "called"), lines)
        self.assertIn((h["Reviews the plan"]["id"], h["Reads the code"]["id"], "called"), lines)  # parentAgentId
        self.assertEqual(violations(r), [])

    def test_old_steps_do_not_become_cards(self):
        m = World()
        pid, _ = m.session()
        m.steps = [step("s1", [str(uuid.uuid4())], verdict="done", manager=str(uuid.uuid4())),
                   step("s2", pending=True, depends_on=["s1"], manager=str(uuid.uuid4()))]
        r, _ = m.snap()
        self.assertEqual([n["id"] for n in r["nodes"]], [f"p:{pid}"])
        self.assertEqual(r["lines"], [])

    def test_dead_session_not_in_a_cold_snapshot(self):
        m = World()
        m.session(pid=dead_pid(), status="busy")
        r, _ = m.snap()
        self.assertEqual(r["nodes"], [])

    def test_manager_linked_through_manager_session_id(self):
        """The tab is found only by its handle (its session id is unknown to the step); the manager by the
        step's manager_session_id."""
        m = World()
        mgr_pid, mgr = m.session(pane=4)
        h = handle()
        tab_pid, _ = m.session(status="busy", handle=h)
        m.steps = [step("s1", [str(uuid.uuid4())], handles=[h], manager=mgr)]
        r, _ = m.snap()
        self.assertIn({"from": f"p:{mgr_pid}", "to": f"p:{tab_pid}", "kind": "opened"}, r["lines"])
        self.assertEqual(by_id(r)[f"p:{tab_pid}"]["kind"], "orchestrated")

    def test_older_tab_of_a_continuation_does_not_inherit(self):
        m = World()
        _, mgr = m.session(pane=1)
        old_pid, old = m.session()
        new_pid, new = m.session()
        m.conversation(old, [r_user("x"), r_asst("Shall I go on?"), r_end()])
        m.conversation(new, [r_user("x"), r_asst("Shall I go on?"), r_end()])
        m.steps = [step("s1", [old, new], verdict="done", manager=mgr)]
        r, _ = m.snap()
        nodes = by_id(r)
        self.assertEqual(nodes[f"p:{new_pid}"]["state"], "done")
        self.assertEqual((nodes[f"p:{old_pid}"]["state"], nodes[f"p:{old_pid}"].get("guessed")), ("waiting", True))
        self.assertEqual(nodes[f"p:{old_pid}"]["kind"], "orchestrated")  # kind and label, no verdict

    def test_helpers_dead_rule_and_vanishing(self):
        m = World()
        _, busy = m.session(status="busy")
        _, idle = m.session(status="idle")
        for sid in (busy, idle):
            m.helper(sid, "a0000000000000001", {"agentType": "t", "description": "silent 6 min"}, age_s=360)
            m.helper(sid, "a0000000000000002", {"agentType": "t", "description": "silent 30 min"}, age_s=1800)
            m.helper(sid, "a0000000000000003", {"agentType": "t", "description": "finished 20 min ago",
                                                "finished": True}, age_s=1200)
        r, _ = m.snap()
        states = sorted((n["title"], n["state"]) for n in r["nodes"] if n["kind"] == "helper")
        self.assertEqual(states, [("silent 6 min", "closed"), ("silent 6 min", "working")])

    def test_tern_title_without_the_claude_sign(self):
        m = World()
        pid, _ = m.session(pane=3, name="", tern_title="⠂ Fix the page")
        r, _ = m.snap()
        self.assertEqual(by_id(r)[f"p:{pid}"]["title"], "Fix the page")


class TitleOrder(unittest.TestCase):
    """user name -> last customTitle -> last aiTitle -> Tern title (tern only) -> derived name -> "unnamed
    session"; each step wins over every one after it."""

    def title(self, source="derived", custom=(), ai=(), pane=5, name="derived-2c", tern="✳ tern title"):
        m = World()
        pid, sid = m.session(pane=pane, name=name, nameSource=source, tern_title=tern)
        recs = [r_user("x")] + [r_title("custom-title", c) for c in custom] + [
            r_title("ai-title", a) for a in ai] + [r_asst("Done."), r_end()]
        m.conversation(sid, recs)
        r, _ = m.snap()
        self.assertEqual(violations(r), [])
        return by_id(r)[f"p:{pid}"]["title"]

    def test_each_step_wins_over_the_next(self):
        full = {"custom": ["custom old", "custom new"], "ai": ["ai old", "ai new"]}
        self.assertEqual(self.title("user", name="given name", **full), "given name")
        self.assertEqual(self.title(**full), "custom new")
        self.assertEqual(self.title(ai=["ai old", "ai new"]), "ai new")
        self.assertEqual(self.title(), "tern title")
        self.assertEqual(self.title(tern=""), "derived-2c")
        self.assertEqual(self.title(tern="", name=""), "unnamed session")

    def test_tern_title_only_for_a_tern_tab(self):
        self.assertEqual(self.title(pane=None), "derived-2c")

    def test_title_with_path_uuid_or_env_comes_out_clean(self):
        m = World()
        pid, _ = m.session(name=f"look {HOME_PREFIX}someone/x and {uuid.uuid4()} and KEY=1 and ~/notes")
        r, _ = m.snap()
        self.assertEqual(violations(r), [])
        self.assertTrue(by_id(r)[f"p:{pid}"]["title"].startswith("look"))


class BrokenSource(unittest.TestCase):

    def setUp(self):
        snapshot.forget()

    def test_one_broken_source_does_not_stop_the_others(self):
        m = World()
        pid, sid = m.session(pane=2)
        m.ps_json.write_text("{not a list", encoding="utf-8")
        m.registry_file("999999999.json", "{broken")
        m.steps = []
        FAKE.raise_on_steps = True
        try:
            r, problems = m.snap()
        finally:
            FAKE.raise_on_steps = False
        self.assertEqual([n["id"] for n in r["nodes"]], [f"p:{pid}"])
        self.assertEqual(r["problems_n"], len(problems))
        self.assertIn("ps did not answer", problems)
        self.assertTrue(any(p.startswith("session registry") for p in problems))
        self.assertIn("adapter Orch: failed (RuntimeError)", problems)

    def test_tern_broken(self):
        m = World()
        pid, _ = m.session(pane=2)
        m.tern_json.write_text("x", encoding="utf-8")
        r, problems = m.snap()
        self.assertEqual((by_id(r)[f"p:{pid}"]["kind"], by_id(r)[f"p:{pid}"]["where"]), ("tern", "Tern"))
        self.assertGreaterEqual(r["problems_n"], 1)


# ---------------------------------------------------------------- the process environment never leaves _read_ps

class Environment(unittest.TestCase):

    def setUp(self):
        snapshot.forget()

    def _clean(self, r, problems):
        everything = json.dumps(r, ensure_ascii=False) + "\n".join(problems)
        for forbidden in ("abc123", "SECRET", "SHELL", "/bin/zsh", "/usr/local/bin/claude", "TERN_PANE"):
            self.assertNotIn(forbidden, everything)

    def test_planted_environment(self):
        m = World()
        pid, _ = m.session()
        with_pane, _ = m.session()
        m.ps_lines = [f"{pid} claude FAKE_SECRET=abc123 TERN_PANE_X=9",
                      f"{with_pane} claude FAKE_SECRET=abc123 TERN_PANE=5 OTHER=1"]
        m.write()
        r, problems = m.snap()
        nodes = by_id(r)
        self.assertEqual(nodes[f"p:{pid}"]["kind"], "other")  # TERN_PANE_X never matches
        self.assertNotIn("pane", nodes[f"p:{pid}"])
        self.assertEqual(nodes[f"p:{with_pane}"].get("pane"), 5)
        self._clean(r, problems)

    def test_spoiled_line_does_not_reach_the_problems(self):
        m = World()
        m.session()
        m.ps_lines = ["junk FAKE_SECRET=abc123 TERN_PANE=", 12345, None, "   FAKE_SECRET=abc123"]
        m.write()
        r, problems = m.snap()
        self._clean(r, problems)
        with m.active(), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(snapshot.main(["snapshot", "--problems"]), 0)
        self.assertNotIn("abc123", out.getvalue())

    def test_anchored_regex(self):
        rx = snapshot._PANE_RE
        self.assertEqual(rx.search("1 a TERN_PANE=12 b").group(1), "12")
        self.assertEqual(rx.search("1 a TERN_PANE=12").group(1), "12")
        for bad in ("1 a TERN_PANE_X=9", "1 a XTERN_PANE=9", "1 a TERN_PANE=9x", "1 a TERN_PANE="):
            self.assertIsNone(rx.search(bad), bad)


# ---------------------------------------------------------------- the cache

def _read():
    return dict(snapshot.READ)


def _delta(before):
    return {k: snapshot.READ[k] - before[k] for k in before}


def _line(rec) -> bytes:
    return (json.dumps(rec, ensure_ascii=False) + "\n").encode("utf-8")


def _fill(n=120):
    """About 100 KB of plain records, so a conversation is bigger than the tail window."""
    return [r_result("x" * 800) for _ in range(n)]


class Cache(unittest.TestCase):
    """Same key -> 0 bytes; one new line -> only it (plus at most the tail window); shrunk or another inode ->
    from the start; a half-written last line is read again whole."""

    def setUp(self):
        snapshot.forget()

    def title_of(self, m, pid):
        return by_id(m.snap()[0])[f"p:{pid}"]["title"]

    def test_second_snapshot_without_change_reads_zero_bytes(self):
        m = World()
        _, sid = m.session(pane=1)
        m.conversation(sid, [r_user("do it"), r_asst("Done."), r_end()] + _fill())
        m.helper(sid, "a1212121212121212", {"agentType": "t", "description": "help"}, [r_asst("reading")])
        _, bg = m.session(kind="bg")
        m.conversation(bg, BG + [r_end()])  # idle at a turn end with a background command
        first = m.snap()[0]
        self.assertGreater(snapshot.READ["scan"], 0)
        before = _read()
        second = m.snap()[0]
        self.assertEqual(_delta(before), {"scan": 0, "tail": 0})
        self.assertEqual(second["version"], first["version"])

    def test_one_new_line_reads_only_it(self):
        m = World()
        pid, sid = m.session(pane=1, nameSource="derived")
        path = m.conversation(sid, [r_user("do it"), r_asst("Done."), r_end()] + _fill())
        m.helper(sid, "a1313131313131313", {"agentType": "t", "description": "help"}, [r_asst("ok")])
        self.assertEqual(self.title_of(m, pid), "tern title")
        new = _line(r_title("custom-title", "renamed"))
        with open(path, "ab") as fh:
            fh.write(new)
        before = _read()
        title = self.title_of(m, pid)
        d = _delta(before)
        self.assertEqual(d["scan"], len(new))  # the helper transcript was not read again either
        self.assertTrue(0 < d["tail"] <= snapshot.WINDOW, d)
        self.assertEqual(title, "renamed")  # the new bytes were judged

    def test_half_line_is_read_again_whole(self):
        m = World()
        pid, sid = m.session(pane=1, nameSource="derived")
        path = m.conversation(sid, [r_user("do it"), r_asst("Done."), r_end()])
        m.snap()
        new = _line(r_title("custom-title", "renamed"))
        half, rest = new[:20], new[20:]
        with open(path, "ab") as fh:
            fh.write(half)
        before = _read()
        self.assertEqual(self.title_of(m, pid), "tern title")  # does not parse yet: not judged
        self.assertEqual(_delta(before)["scan"], len(half))
        with open(path, "ab") as fh:
            fh.write(rest)
        before = _read()
        self.assertEqual(self.title_of(m, pid), "renamed")
        self.assertEqual(_delta(before)["scan"], len(new))  # from the start of the unfinished line

    def test_shrunk_reads_from_the_start(self):
        m = World()
        pid, sid = m.session(pane=1, nameSource="derived")
        recs = [r_user("do it"), r_asst("Done."), r_end()]
        path = m.conversation(sid, recs + [r_title("custom-title", "renamed")])
        ino = path.stat().st_ino
        self.assertEqual(self.title_of(m, pid), "renamed")
        m.conversation(sid, recs)  # rewritten shorter, same inode
        self.assertEqual(path.stat().st_ino, ino)
        before = _read()
        self.assertEqual(self.title_of(m, pid), "tern title")
        self.assertEqual(_delta(before)["scan"], path.stat().st_size)

    def test_another_inode_reads_from_the_start(self):
        m = World()
        pid, sid = m.session(pane=1, nameSource="derived")
        path = m.conversation(sid, [r_user("do it"), r_title("custom-title", "renamed"), r_end()])
        self.assertEqual(self.title_of(m, pid), "renamed")
        other = path.with_suffix(".new")
        other.write_bytes(b"".join(_line(r) for r in [r_user("do it"), r_asst("Done."), r_end()] + _fill(5)))
        os.replace(other, path)  # bigger than before, another inode
        before = _read()
        self.assertEqual(self.title_of(m, pid), "tern title")
        self.assertEqual(_delta(before)["scan"], path.stat().st_size)

    def test_big_tail_text_past_the_window(self):
        m = World()
        _, sid = m.session()
        middle = m.conversation(sid, [r_user("do it"), r_asst("First text.")] + _fill())  # mid-turn: no text needed
        with m.active():
            before = _read()
            c = snapshot.read_tail(middle)
            self.assertLessEqual(_delta(before)["tail"], snapshot.WINDOW)
            self.assertEqual(c["last"]["type"], "user")
            end = m.conversation(str(uuid.uuid4()), [r_user("do it"), r_asst("Last words?"), r_end()]
                                 + [r_raw("cost-state", costUSD=0.1, pad="y" * 800) for _ in range(120)])
            c = snapshot.read_tail(end)  # the turn end and its text past the window: the wider read
            self.assertEqual((c["last"].get("subtype"), c["text"]), ("turn_duration", "Last words?"))


class ColdStart(unittest.TestCase):
    """The first-ever snapshot with an empty cache, and the same snapshot taken twice."""

    def test_first_snapshot_with_empty_cache_then_twice(self):
        snapshot.forget()
        self.assertEqual((snapshot._CACHE, snapshot._PANES), ({}, {}))
        m = World()
        pid, sid = m.session(pane=1)
        m.conversation(sid, [r_user("do it"), r_asst("Done."), r_end()])
        before = _read()
        first, problems = m.snap()
        self.assertEqual(problems, [])
        self.assertGreater(_delta(before)["scan"], 0)
        self.assertEqual(by_id(first)[f"p:{pid}"]["state"], "done")
        before = _read()
        second, _ = m.snap()
        self.assertEqual(_delta(before), {"scan": 0, "tail": 0})
        self.assertEqual({k: v for k, v in second.items() if k != "generated_at"},
                         {k: v for k, v in first.items() if k != "generated_at"})

    def test_empty_registry_folder_through_the_public_call(self):
        snapshot.forget()
        m = World()
        problems: list = []
        with m.active():
            s = keep(snapshot.snapshot(problems))
        self.assertEqual((s["nodes"], s["problems_n"], problems), ([], 0, []))


class PsAndLimits(unittest.TestCase):
    """ps only for new pids, problems without disk paths, the tail never reads more than 2 MiB."""

    def setUp(self):
        snapshot.forget()

    def test_ps_only_for_a_new_pid(self):
        m = World()
        one, _ = m.session(pane=1)
        calls = []
        original = snapshot._read_ps

        def count(pids, problems):
            calls.append(sorted(pids))
            return original(pids, problems)

        snapshot._read_ps = count
        try:
            self.assertEqual(by_id(m.snap()[0])[f"p:{one}"]["pane"], 1)
            self.assertEqual(by_id(m.snap()[0])[f"p:{one}"]["pane"], 1)  # from the cache
            two, _ = m.session(pane=2)
            self.assertEqual(by_id(m.snap()[0])[f"p:{two}"]["pane"], 2)
            self.assertEqual(calls, [[one], [two]])
            (m.reg / f"{one}.json").unlink()
            m.snap()
            self.assertNotIn(one, snapshot._PANES)  # a pid no longer asked about leaves the cache
        finally:
            snapshot._read_ps = original

    def test_problems_without_disk_paths(self):
        m = World()
        m.session(pane=1)
        m.tern_json.unlink()
        m.registry_file("999999998.json", "{broken")
        with m.active(), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(snapshot.main(["snapshot", "--problems"]), 0)
        text = out.getvalue()
        self.assertNotIn(str(TMP), text)
        self.assertIsNone(re.search(r"(?:^|[\s'\"=(\[])(?:~|\$\{?HOME\}?)?/[^\s'\"]*/", text), text)
        self.assertNotIn("no problems", text)  # the checker had something to look at
        self.assertEqual(snapshot._DISK_RE.sub("…", "error: [Errno 2] No such file: '/a/b/c.json' and ~/x/y"),
                         "error: [Errno 2] No such file: '…' and …")

    def test_tail_never_reads_more_than_2_mib(self):
        m = World()
        _, sid = m.session()
        path = m.conversation(sid, [r_user("do it"), r_asst("Done."), r_end()]
                              + [{"type": "cost-state", "pad": "z" * 1000} for _ in range(2300)])  # ~2.3 MB
        with m.active():
            before = _read()
            c = snapshot.read_tail(path)
        self.assertLessEqual(_delta(before)["tail"], snapshot.WINDOW + snapshot._CEILING)
        self.assertGreater(path.stat().st_size, snapshot._CEILING)
        self.assertEqual((c["last"], c["changed"]), (None, None))


# ---------------------------------------------------------------- the incremental reads against cold ones

_COLD_TAIL = 256 * 1024


def cold_tail(path: Path, accept):
    """The last record `accept` accepts, read from the file's end (the last 256 KiB, then the whole file), a
    trailing idle-compaction block seen through. The cold reference for read_tail."""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            start = max(0, fh.tell() - _COLD_TAIL)
            while True:
                fh.seek(start)
                lines = fh.read().decode("utf-8", "replace").splitlines()
                if start > 0:
                    lines = lines[1:]
                crossing = False
                for line in reversed(lines):
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(rec, dict):
                        continue
                    if not rec.get("isSidechain"):
                        if not crossing and transcript.is_idle_compaction_mark(rec):
                            crossing = True
                            continue
                        if crossing and transcript.is_compaction_block(rec):
                            continue
                    if accept(rec):
                        return rec
                if start == 0:
                    return None
                start = 0
    except OSError:
        return None


def cold_titles(path: Path) -> dict:
    """The last custom-title and ai-title over the whole file, line by line."""
    out = {"customTitle": None, "aiTitle": None}
    try:
        data = path.read_bytes()
    except OSError:
        return out
    for raw in data.split(b"\n"):
        try:
            rec = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(rec, dict):
            continue
        field = {"custom-title": "customTitle", "ai-title": "aiTitle"}.get(rec.get("type"))
        if field and isinstance(rec.get(field), str) and rec[field].strip():
            out[field] = rec[field]
    return out


class IncrementalEqualsCold(unittest.TestCase):
    """The incremental reads against cold ones, the file growing a few bytes at a time (cuts inside lines and
    inside UTF-8 characters included): the tail = cold_tail, the titles = cold_titles, the background ids =
    transcript.background_ids."""

    def fixtures(self):
        base = [r_user("do it"), r_asst("Action finished ✓ — naïve café 日本")]
        fx = {"plain": base + [r_end()]}
        fx["background"] = BG + [r_end()]
        fx["background_returned"] = BG + [RETURNED, r_asst("It returned."), r_end()]
        fx["background_queued"] = BG + [r_raw("attachment", attachment={
            "type": "queued_command", "prompt": "<task-notification><task-id>bx12</task-id>"}), r_end()]
        fx["titles"] = base + [r_title("custom-title", "one"), r_title("ai-title", "two"),
                               r_title("custom-title", "three ✓"), r_end()]
        fx["compacted"] = base + [r_end(), r_raw("system", subtype="compact_boundary", isSidechain=False),
                                  dict(r_user("summary"), isCompactSummary=True),
                                  r_raw("system", subtype="informational", isSidechain=False,
                                        content="Compacted while idle")]
        for name, rec in TAILS.items():
            fx[f"tail_{name}"] = base + [r_end(), rec]
        fx["long_tail"] = base + [r_end()] + [r_raw("cost-state", pad="y" * 800) for _ in range(120)]
        fx = {k: b"".join(_line(r) for r in v) for k, v in fx.items()}
        fx["undecodable"] = _line(r_user("x")) + b"\xff\xfe does not decode\n" + _line(r_end())
        return fx

    def test_incremental_equals_cold(self):
        m = World()
        snapshot.forget()
        for name, data in self.fixtures().items():
            with self.subTest(fixture=name):
                path = m.proj / f"{uuid.uuid4()}.jsonl"
                path.write_bytes(b"")
                ends = [i for i, c in enumerate(data) if c == 10]  # before and after each newline
                every = 23 if len(data) < 20000 else 4099
                cuts = sorted(set(range(0, len(data), every)) | set(ends) | {i + 1 for i in ends} | {len(data)})
                done = 0
                for cut in cuts:
                    with open(path, "ab") as fh:
                        fh.write(data[done:cut])
                    done = cut
                    c = snapshot.read_tail(path)
                    self.assertEqual(c["background"], transcript.background_ids(path), cut)
                    self.assertEqual(c["last"], cold_tail(path, transcript.accepted), cut)
                    said = cold_tail(path, transcript.has_text)
                    self.assertEqual(c["text"], transcript.text(said) if said else None, cut)
                    self.assertEqual(snapshot.read_titles(path), cold_titles(path), cut)

    def test_the_checker_tells_running_from_returned(self):
        """The cold references themselves, both ways: a running background id from a returned one, and the
        title reader from an untitled file."""
        fx = self.fixtures()
        d = Path(tempfile.mkdtemp(dir=TMP))
        path = d / "c.jsonl"
        for name, expected in (("background", {"bx12"}), ("background_returned", set()),
                               ("background_queued", set()), ("undecodable", set())):
            path.write_bytes(fx[name])
            self.assertEqual(transcript.background_ids(path), expected, name)
        path.write_bytes(fx["titles"])
        self.assertEqual(cold_titles(path), {"customTitle": "three ✓", "aiTitle": "two"})
        path.write_bytes(fx["plain"])
        self.assertEqual(cold_titles(path), {"customTitle": None, "aiTitle": None})


# ---------------------------------------------------------------- merge_closed

class MergeClosed(unittest.TestCase):
    """Rule 1 in the server: a vanished session or helper stays `closed` for 10 min, then goes."""

    T = 1_800_000_000.0

    def snapshots(self):
        nodes = [
            {"id": "p:1", "kind": "tern", "title": "Window", "where": "Tern · tab 1", "state": "waiting",
             "since": "x", "pane": 3, "waiting_for": "permission request"},
            {"id": "p:2", "kind": "orchestrated", "title": "site · plan", "where": "Orch", "state": "working",
             "since": "x", "guessed": True},
            {"id": "a:aaaaaaaa", "kind": "helper", "title": "Reviews", "where": "t", "state": "working", "since": "x"},
            {"id": "a:bbbbbbbb", "kind": "helper", "title": "Finished", "where": "t", "state": "done", "since": "x"},
            {"id": "n:0:0123456789ab", "kind": "next", "title": "x · build", "where": "Orch · next",
             "state": "next", "since": None},
            {"id": "p:3", "kind": "tern", "title": "Cancelled", "where": "Tern", "state": "closed", "since": "x"},
            {"id": "p:4", "kind": "tern", "title": "Stays", "where": "Tern", "state": "stopped", "since": "x"},
        ]
        lines = [{"from": "p:4", "to": "p:1", "kind": "opened"}, {"from": "p:1", "to": "a:aaaaaaaa", "kind": "called"},
                 {"from": "p:4", "to": "n:0:0123456789ab", "kind": "follows"},
                 {"from": "p:4", "to": "p:2", "kind": "opened"}]
        previous = snapshot._with_version("g1", nodes, lines, 0, 0)
        current = snapshot._with_version("g2", [nodes[6]], [], 0, 0)
        return previous, current

    def test_vanished_stays_closed_ten_minutes(self):
        previous, current = self.snapshots()
        mem = {}
        r = keep(snapshot.merge_closed(previous, current, self.T, memory=mem, hidden=()))
        nodes = by_id(r)
        self.assertEqual(set(nodes), {"p:4", "p:1", "p:2", "a:aaaaaaaa"})
        self.assertEqual(nodes["p:1"], {"id": "p:1", "kind": "tern", "title": "Window", "where": "Tern · tab 1",
                                        "state": "closed", "since": snapshot._iso(self.T)})
        self.assertEqual((nodes["p:2"]["state"], "guessed" in nodes["p:2"]), ("closed", False))
        self.assertEqual(nodes["a:aaaaaaaa"]["state"], "closed")
        self.assertEqual({(x["from"], x["to"]) for x in r["lines"]},
                         {("p:4", "p:1"), ("p:1", "a:aaaaaaaa"), ("p:4", "p:2")})
        self.assertEqual(r["version"], snapshot._with_version("g2", r["nodes"], r["lines"], 0, 0)["version"])
        self.assertNotEqual(r["version"], current["version"])
        self.assertEqual(violations(r), [])
        almost = snapshot.merge_closed(r, current, self.T + 599, memory=mem, hidden=())
        self.assertEqual(by_id(almost)["p:1"]["since"], snapshot._iso(self.T))  # the clock is the vanishing
        self.assertEqual(set(by_id(almost)), set(nodes))
        end = snapshot.merge_closed(almost, current, self.T + 600, memory=mem, hidden=())
        self.assertIs(end, current)
        self.assertEqual(mem, {})

    def test_comes_back_alive(self):
        previous, current = self.snapshots()
        mem = {}
        r = snapshot.merge_closed(previous, current, self.T, memory=mem, hidden=())
        back = snapshot._with_version("g3", current["nodes"] + [previous["nodes"][0]], [], 0, 0)
        r2 = snapshot.merge_closed(r, back, self.T + 30, memory=mem, hidden=())
        self.assertEqual(by_id(r2)["p:1"]["state"], "waiting")
        self.assertNotIn("p:1", mem)

    def test_hidden_by_age_never_closes_and_without_previous_nothing_changes(self):
        previous, current = self.snapshots()
        r = snapshot.merge_closed(previous, current, self.T, memory={}, hidden={"p:1", "p:2", "a:aaaaaaaa"})
        self.assertIs(r, current)
        self.assertIs(snapshot.merge_closed(None, current, self.T, memory={}, hidden=()), current)

    def test_with_the_real_join(self):
        snapshot.forget()
        m = World()
        pid_dies, _ = m.session(pane=1)
        pid_ages, sid = m.session()
        path = m.conversation(sid, [r_user("x"), r_asst("Done."), r_end()])
        first = m.snap()[0]
        self.assertEqual({f"p:{pid_dies}", f"p:{pid_ages}"}, set(by_id(first)))
        for p in _PROCS:
            if p.pid == pid_dies:
                p.kill()
                p.wait(timeout=5)
        old = time.time() - 9 * 3600
        m.conversation(sid, aged([r_user("x"), r_asst("Done."), r_end()], 9 * 3600))
        os.utime(path, (old, old))
        reg = json.loads((m.reg / f"{pid_ages}.json").read_text(encoding="utf-8"))
        reg["statusUpdatedAt"] = int(old * 1000)
        (m.reg / f"{pid_ages}.json").write_text(json.dumps(reg), encoding="utf-8")
        second = m.snap()[0]
        self.assertEqual((second["nodes"], second["hidden_n"]), ([], 1))
        r = keep(snapshot.merge_closed(first, second, time.time(), memory={}))  # hidden: the last snapshot's
        self.assertEqual([(n["id"], n["state"]) for n in r["nodes"]], [(f"p:{pid_dies}", "closed")])


# ---------------------------------------------------------------- adapters: handles, focus, failures

class AdapterHandles(unittest.TestCase):

    def setUp(self):
        snapshot.forget()

    def _clean(self, text, handles):
        for forbidden in ("SECRET", "=abc", "ORCH_HANDLE", "h_", "SHELL", "/bin/zsh") + tuple(handles):
            self.assertNotIn(forbidden, text)

    def test_anchored_regex(self):
        rx = orchestrator.env_regex(FAKE)
        h = handle()
        self.assertEqual(rx.search(f"1 a ORCH_HANDLE={h} b").group(1), h)
        self.assertEqual(rx.search(f"1 a ORCH_HANDLE={h}").group(1), h)
        for bad in ("1 a ORCH_HANDLE_X=h_ab", "1 a XORCH_HANDLE=h_ab", "1 a ORCH_HANDLE=h_abz",
                    "1 a ORCH_HANDLE=", "1 a ORCH_HANDLE=abc"):
            self.assertIsNone(rx.search(bad), bad)

    def test_same_ps_for_panes_and_handles_and_only_the_handle_leaves(self):
        m = World()
        with_h, _ = m.session()
        without, _ = m.session()
        tern_pid, _ = m.session()
        h = handle()
        m.ps_lines = [f"{with_h} /usr/local/bin/claude FAKE_SECRET=abc ORCH_HANDLE={h} SHELL=/bin/zsh",
                      f"{without} /usr/local/bin/claude FAKE_SECRET=abc ORCH_HANDLE_X=h_00",
                      f"{tern_pid} /usr/local/bin/claude FAKE_SECRET=abc TERN_PANE=5"]
        m.write()
        calls = []
        original = snapshot._read_ps

        def count(pids, problems):
            calls.append(sorted(pids))
            return original(pids, problems)

        snapshot._read_ps = count
        try:
            with m.active():
                snapshot._use_adapters([FAKE])
                problems: list = []
                ident = {with_h: 1, without: 1, tern_pid: 1}
                self.assertEqual(snapshot.read_panes(ident, problems), {tern_pid: 5})
                self.assertEqual(snapshot.read_handles(ident, problems), {with_h: {0: h}})
                self.assertEqual(len(calls), 1)  # one ps for both: the second came from the cache
                self.assertEqual(problems, [])
                self.assertEqual(snapshot._PANES[with_h], (1, None, {0: h}))  # the fixed cache shape
        finally:
            snapshot._read_ps = original

    def test_focus_target(self):
        m = World()
        h, step_h = handle(), handle()
        by_env, sid1 = m.session(handle=h)
        by_step, sid2 = m.session()
        plain, _ = m.session()
        m.steps = [step("s1", [sid1]), step("s2", [sid2], handles=[step_h])]
        m.snap()
        calls = []
        original = snapshot._read_ps

        def count(pids, problems):
            calls.append(sorted(pids))
            return original(pids, problems)

        snapshot._read_ps = count
        try:
            with m.active():
                self.assertEqual(snapshot.focus_target(by_env), (FAKE, h))  # the environment, from the cache
                self.assertEqual(snapshot.focus_target(by_step), (FAKE, step_h))  # else the step's current tab
                self.assertIsNone(snapshot.focus_target(plain))  # never matched to a step
                self.assertIsNone(snapshot.focus_target(True))
                self.assertEqual(calls, [])
                snapshot._PANES.clear()
                self.assertEqual(snapshot.focus_target(by_env), (FAKE, h))  # one ps for that pid alone
                self.assertEqual(calls, [[by_env]])
                self.assertNotIn(by_env, snapshot._PANES)  # not kept
        finally:
            snapshot._read_ps = original

    def test_handle_never_in_the_snapshot_nor_the_problems(self):
        m = World()
        _, mgr = m.session(pane=1)
        h = handle()
        pid, old = m.session(status="busy", handle=h)
        m.steps = [step("s1", [old], handles=[h], manager=mgr)]
        m.tern_json.unlink()  # one failing source, so --problems has something to say
        r, problems = m.snap()
        self.assertEqual(by_id(r)[f"p:{pid}"]["kind"], "orchestrated")
        self.assertTrue(problems)
        self.assertEqual(violations(r), [])
        with m.active(), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(snapshot.main(["snapshot", "--problems"]), 0)
        self._clean(json.dumps(r, ensure_ascii=False) + "\n".join(problems) + out.getvalue(), [h])

    def test_clear_keeps_the_card_through_the_handle(self):
        """Before and after a /clear: the same process (same pid, same environment), the registry's sessionId
        swapped, the step still holding the old session id and the same handle."""
        m = World()
        mgr_pid, mgr = m.session(pane=1)
        pid = alive_pid()
        old, new = str(uuid.uuid4()), str(uuid.uuid4())
        h = handle()
        m.session(pid=pid, sid=old, status="busy", handle=h)
        m.steps = [step("s1", [old], handles=[h], label="example · plan", manager=mgr)]
        opened = {"from": f"p:{mgr_pid}", "to": f"p:{pid}", "kind": "opened"}

        def check():
            r, _ = m.snap()
            node = by_id(r)[f"p:{pid}"]
            self.assertEqual((node["kind"], node["title"], node["where"]), ("orchestrated", "example · plan", "Orch"))
            self.assertIn(opened, r["lines"])
            self.assertEqual(violations(r), [])

        check()
        m.session(pid=pid, sid=new, status="busy")  # the /clear: a new sessionId in the same process
        m.ps_lines = m.ps_lines[:-1]  # the environment did not change: still the one line with the handle
        m.write()
        check()
        snapshot.forget()
        check()  # cold, too
        self.assertEqual(FAKE.calls[-1][1], {h})  # the adapter was told which of its handles are live

    def test_steps_that_raise_leave_the_rest_intact(self):
        m = World()
        tern_pid, _ = m.session(pane=3)
        h = handle()
        orch_pid, sid = m.session(handle=h)
        m.steps = [step("s1", [sid])]
        FAKE.raise_on_steps = True
        try:
            r, problems = m.snap()
        finally:
            FAKE.raise_on_steps = False
        self.assertEqual(problems, ["adapter Orch: failed (RuntimeError)"])
        self.assertEqual(r["problems_n"], 1)
        nodes = by_id(r)
        self.assertEqual((nodes[f"p:{tern_pid}"]["kind"], nodes[f"p:{orch_pid}"]["kind"]), ("tern", "other"))
        self.assertIsNone(snapshot.focus_target(orch_pid))

    def test_adapter_problems_attribute_joins_the_snapshot(self):
        m = World()
        _, sid = m.session()
        m.steps = [step("s1", [sid])]
        FAKE.problems = ["the step file does not parse: " + "/some/where/steps.json", 7]
        try:
            r, problems = m.snap()
        finally:
            FAKE.problems = []
        self.assertEqual(problems, ["adapter Orch: the step file does not parse: …"])
        self.assertEqual(r["problems_n"], 1)
        self.assertEqual([n["kind"] for n in r["nodes"]], ["orchestrated"])  # its steps still count

    def test_bad_and_duplicate_steps_are_dropped(self):
        m = World()
        _, sid = m.session()
        m.steps = [step("bad key!", [sid]), step("s1", [sid], verdict="maybe"), "not a step",
                   step("ok", [sid], label="first"), step("ok", [sid], label="second")]
        r, problems = m.snap()
        self.assertEqual(len(problems), 4, problems)
        self.assertEqual([n["title"] for n in r["nodes"]], ["first"])

    def test_pending_step_with_follows_lines(self):
        m = World()
        mgr_pid, mgr = m.session(pane=1)
        a_pid, a = m.session()
        m.steps = [step("a", [a], label="site · plan"),
                   step("b", label="site · build", pending=True, depends_on=["a"]),
                   step("c", label="site · check", pending=True, depends_on=["b", "gone"]),
                   step("d", label="site · ship", pending=True, depends_on=["nowhere"], manager=mgr),
                   step("e", label="lost", pending=True, depends_on=["nowhere"])]
        r, problems = m.snap()
        self.assertEqual(problems, [])
        nb, nc, nd = next_id(0, "b"), next_id(0, "c"), next_id(0, "d")
        self.assertEqual(sorted(n["id"] for n in r["nodes"] if n["kind"] == "next"), sorted([nb, nc, nd]))
        lines = {(x["from"], x["to"], x["kind"]) for x in r["lines"]}
        self.assertEqual(lines, {(f"p:{a_pid}", nb, "follows"), (nb, nc, "follows"),
                                 (f"p:{mgr_pid}", nd, "follows")})

    def test_no_adapter_no_call(self):
        m = World()
        m.session()
        FAKE.calls.clear()
        m.snap()
        self.assertEqual(FAKE.calls, [])


# ---------------------------------------------------------------- the prototype (on a temporary copy only)

class Prototype(unittest.TestCase):

    def test_embed_with_escape(self):
        d = Path(tempfile.mkdtemp(dir=TMP))
        page = d / "page.html"
        shutil.copyfile(REPO / "session_map" / "page.html", page)
        out = d / "prototype.html"
        snap = {"version": "0" * 12, "generated_at": "x", "lines": [], "problems_n": 0, "hidden_n": 0,
                "nodes": [{"id": "p:1", "kind": "tern", "title": "</script><b>hi\u2028\u2029",
                           "where": "Tern", "state": "stopped", "since": None}]}
        self.assertEqual(snapshot.prototype(out, snap, page), out)
        html = out.read_text(encoding="utf-8")
        self.assertNotIn("</script><b>", html)
        self.assertNotIn("\u2028", html)
        m = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S)
        self.assertEqual(json.loads(m.group(1)), snap)
        self.assertEqual(html.count('<script id="data"'), 1)
        self.assertEqual(snapshot.DATA_TAG, '<script id="data" type="application/json">null</script>')
        self.assertEqual(page.read_text(encoding="utf-8").count(snapshot.DATA_TAG), 1)

    def test_page_without_the_tag_is_refused(self):
        with self.assertRaises(ValueError):
            snapshot.embed("<html></html>", {})
        with self.assertRaises(ValueError):
            snapshot.embed(snapshot.DATA_TAG * 2, {})


class Cli(unittest.TestCase):

    def test_snapshot_prints_valid_json(self):
        m = World()
        m.session(pane=1)
        with m.active(), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(snapshot.main(["snapshot"]), 0)
        r = keep(json.loads(out.getvalue()))
        self.assertEqual(len(r["nodes"]), 1)
        self.assertEqual(violations(r), [])

    def test_prototype_writes_where_asked(self):
        m = World()
        out_path = m.d / "p.html"
        with m.active(), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(snapshot.main(["prototype", "--out", str(out_path)]), 0)
        self.assertIn('<script id="data" type="application/json">{', out_path.read_text(encoding="utf-8"))

    def test_bad_argument_exits_2(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(snapshot.main(["nothing"]), 2)


class SweepChecker(unittest.TestCase):
    """The invariant checker itself, both ways."""

    def test_both_ways(self):
        clean = {"version": "x", "generated_at": "x", "problems_n": 0, "hidden_n": 0, "lines": [],
                 "nodes": [{"id": "p:1", "kind": "tern", "title": "/plan fix", "where": "Tern · tab 1",
                            "state": "done", "since": None},
                           {"id": "p:2", "kind": "other", "title": "a = b", "where": "x", "state": "done",
                            "since": None}]}
        self.assertEqual(violations(clean), [])
        for bad in (str(uuid.uuid4()), f"{HOME_PREFIX}x/y", "~/notes", "TERN_PANE=4"):
            with self.subTest(bad=bad):
                s = json.loads(json.dumps(clean))
                s["nodes"][0]["title"] = bad
                self.assertEqual(violations(s), [bad])
        s = json.loads(json.dumps(clean))
        s["nodes"][0]["undocumented"] = True
        self.assertEqual(violations(s), ["node key undocumented"])
        s = json.loads(json.dumps(clean))
        s["extra"] = 1
        self.assertEqual(violations(s), ["top key extra"])


if __name__ == "__main__":
    unittest.main()
