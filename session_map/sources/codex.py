"""The Codex source: OpenAI Codex threads (terminal `codex` and the desktop app) as cards on the map.

What it reads, each through a cache that reads only what is new:
- the rollouts, `<codex dir>/sessions/YYYY/MM/DD/rollout-*.jsonl` (one file per thread). Only whole lines
  holding a needle (a header, a turn's start or end, a turn's settings, a tool call or its output, a user
  message) are JSON-parsed; an output line gives only its call id, by regex, never parsed. Only the FIRST
  line's `session_meta` is the thread's header (a forked rollout carries a second one, ignored);
- the thread names, `<codex dir>/session_index.jsonl` (`{id, thread_name}`, the last one wins);
- the live Codex processes: one `ps -axo pid=,ppid=,etime=,comm=` (the program path only), then one
  `ps eww` over the Codex pids with no Codex or app ancestor, from which only the LAST `PWD=` and
  `TERN_PANE=` leave `_read_env` (argv comes before the environment, so a prompt that types `PWD=` never
  wins), plus whether argv's first token is `app-server` (the TUI's daemon child, which outlives /quit and is
  never a session). Every line is dropped where it is read; any failure is just "codex: ps did not answer".
The codex dir is `SESSION_MAP_CODEX_DIR`, else `$CODEX_HOME`, else `~/.codex`. A run that points the Claude
reader at fixtures (`SESSION_MAP_REGISTRY_DIR`, `SESSION_MAP_PROJECTS_DIR` or `SESSION_MAP_PS_JSON` set) and
not this one reads no Codex at all: a run that points one source at fixtures reads only fixtures.

The state rule (`codex_state`), top to bottom, the first signal that exists decides:
1. not live -> no node (a terminal thread is live when a live terminal Codex process has the thread's cwd as
   its launch PWD and started before the thread's last record, the newest such thread per process; a desktop
   thread while the app process is alive). 2. a question call still unanswered in the open turn -> waiting,
   "question or form" (inferred unless QUESTION_WRITTEN). 3. a turn open: an escalated command with no output
   for more than PERMISSION_WAIT_S under a policy that asks the user -> waiting, "permission request",
   inferred; else untouched for more than STUCK_S -> stopped, stuck; else working since the turn started.
4. the turn completed: an error -> stopped; else the last agent message through snapshot.turn_end_state.
5. aborted -> stopped. 6. anything else -> stopped.
No thread id, path, cwd or message text ever reaches a node: ids are hashes, titles go through snapshot._clean.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from .. import snapshot, transcript
from . import SourceResult

# Checkpoint 0 measured 16/11 (.planning/codex-measure.md); from disk, the desktop app was not run live.
# A terminal thread's originator is "codex-tui" (`codex exec`: "codex_exec"): anything outside this tuple.
DESKTOP_ORIGINATORS = ("Codex Desktop",)  # a thread whose originator is one of these is a desktop (app) thread
# Checkpoint 0 measured 16/11 (.planning/codex-measure.md)
APP_PROCESS_PATH = "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT"  # the desktop app is alive when a comm is this
# Checkpoint 0 measured 16/11 (.planning/codex-measure.md); the terminal TUI is the binary bundled in the app,
# so a program path under the app is no reason to drop a process: its ancestors and argv decide.
TERMINAL_BASENAMES = ("codex",)  # program basenames of a terminal Codex
DAEMON_TOKEN = "app-server"  # argv's first token of the TUI's `codex app-server` child, which outlives /quit
# Checkpoint 0 measured 16/11 (.planning/codex-measure.md)
PERMISSION_WAIT_S = 3  # an escalated command with no output for longer is waiting on the person's approval
# Checkpoint 0 measured 16/11 (.planning/codex-measure.md): the answer arrives as a role-user message
QUESTION_WRITTEN = False  # False: Codex writes no pending-question record, so the question wait is inferred

ASKING_POLICIES = ("on-request", "untrusted")  # approval policies that ask the person
QUESTION_CALL = "request_user_input_async"  # the function call that asks the person a question
PARENT_LEVELS = 4  # how far up the parent chain a terminal Codex is checked for being the app's child
START_SLACK_S = 2  # `etime` has one-second steps: a start computed from it moves by this much between reads

ENV_CODEX_DIR = "SESSION_MAP_CODEX_DIR"
ENV_CODEX_PS_JSON = "SESSION_MAP_CODEX_PS_JSON"  # {"comm": [ps -axo lines], "env": [ps eww lines]}, for tests
SANDBOX_ENVS = ("SESSION_MAP_REGISTRY_DIR", "SESSION_MAP_PROJECTS_DIR", "SESSION_MAP_PS_JSON")

_BLOCK = 4 * 1024 * 1024  # new bytes are scanned in blocks this size
READ = {"codex": 0}  # rollout and thread-name bytes read by this process (tests)
_CACHE: dict = {}  # str(path) -> the running state of one rollout (see _refresh)
_INDEX: dict = {}  # the running state of session_index.jsonl (see _names)
_ENVS: dict = {}  # pid -> (start, PWD or None, pane or None, is the app-server daemon): `ps eww` only for a new pid

# Codex writes compact JSON; the patterns below allow spaces after `:` and `,` too. A quoted needle never
# matches inside a string value, where every quote is escaped.
_NEEDLES = (b'"session_meta"', b'"turn_context"', b'"task_started"', b'"task_complete"', b'"turn_aborted"',
            b'"call_id"')
_USER_RE = re.compile(rb'"role"\s*:\s*"user"')  # a user message: it clears a pending question
_WANTED_CALL = (b"require_escalated", b'"' + QUESTION_CALL.encode() + b'"')  # a call line worth parsing
_OUTPUT_RE = re.compile(rb'"type"\s*:\s*"(?:custom_tool_call|function_call)_output"')
_CALL_ID_RE = re.compile(rb'"call_id"\s*:\s*"([^"\\]*)"')
_STAMP_RE = re.compile(rb'^\{\s*"timestamp"\s*:\s*"([^"\\]+)"')
_COMM_RE = re.compile(r"^\s*(\d+)\s+(\d+)\s+(\S+)\s(.*)$")
_ETIME_RE = re.compile(r"^(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)$")
_PWD_RE = re.compile(r"(?:^|(?<=\s))PWD=(.*?)(?= [A-Z_][A-Z0-9_]*=|$)")
_TPANE_RE = re.compile(r"(?:^|(?<=\s))TERN_PANE=(\d+)(?=\s|$)")
_TOKEN_RE = re.compile(r"^\s+([a-z][a-z0-9-]*)(?=\s|$)")  # argv's first token after the program, a plain word


# ---------------------------------------------------------------- where

def codex_dir() -> Path:
    for v in (os.environ.get(ENV_CODEX_DIR), os.environ.get("CODEX_HOME")):
        if v:
            return Path(v)
    return Path.home() / ".codex"


def sandboxed() -> bool:
    """True when the run points the Claude reader at fixtures and this source at nothing (Decision 6)."""
    return not os.environ.get(ENV_CODEX_DIR) and any(os.environ.get(v) for v in SANDBOX_ENVS)


def forget() -> None:
    """Empty this source's caches (the next read is cold)."""
    _CACHE.clear()
    _INDEX.clear()
    _ENVS.clear()


def _epoch(s) -> Optional[float]:
    t = transcript.parse_iso(s)
    return t.timestamp() if t is not None else None


# ---------------------------------------------------------------- one rollout

def _fresh() -> dict:
    return {"started": False, "header": None, "bad": False, "policy": None, "reviewer": None,
            "turn": None,  # {"id", "start"} while a turn is open
            "end": None,  # how the last turn ended: "complete" or "aborted"
            "text": None, "error": None,  # the last task_complete's message and error
            "esc": {},  # {call id: epoch}: escalated commands of the open turn with no output yet
            "ask": None,  # epoch of the open turn's last question call with nothing after it
            "last": None}  # epoch of the last record


def _header(pl: dict, ts) -> dict:
    src = pl.get("source")
    sub = src.get("subagent") if isinstance(src, dict) else None
    spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
    parent = spawn.get("parent_thread_id") if isinstance(spawn, dict) else None
    if isinstance(sub, dict) and not isinstance(parent, str):
        parent = pl.get("parent_thread_id")

    def s(k):
        return pl.get(k) if isinstance(pl.get(k), str) else None
    return {"id": s("id"), "cwd": s("cwd"), "originator": s("originator"),
            "start": _epoch(pl.get("timestamp")) or ts,
            "guardian": isinstance(sub, dict) and sub.get("other") == "guardian",
            "helper": isinstance(sub, dict), "parent": parent if isinstance(parent, str) else None,
            "nickname": s("agent_nickname")}


def _end_turn(st: dict, how: str) -> None:
    st["turn"], st["end"], st["ask"] = None, how, None
    st["esc"] = {}


def _apply(st: dict, raw: bytes, first: bool) -> None:
    """One whole line (no newline) into the running state. The first line is the header, nothing else."""
    if not first and b'"call_id"' in raw:
        if _OUTPUT_RE.search(raw):  # an output: only its call id matters, and only when that call is pending
            m = _CALL_ID_RE.search(raw)
            if m and st["esc"]:
                st["esc"].pop(m.group(1).decode("utf-8", "replace"), None)
            return
        if st["turn"] is None or not any(n in raw for n in _WANTED_CALL):
            return
    try:
        rec = json.loads(raw)
    except ValueError:
        st["bad"] = True
        return
    if not isinstance(rec, dict) or not isinstance(rec.get("payload"), dict):
        st["bad"] = True
        return
    typ, pl = rec.get("type"), rec["payload"]
    ts = _epoch(rec.get("timestamp"))
    if first:
        if typ == "session_meta":
            st["header"] = _header(pl, ts)
        else:
            st["bad"] = True
        return
    pt = pl.get("type")
    if typ == "turn_context":
        st["policy"] = pl.get("approval_policy")
        st["reviewer"] = pl.get("approvals_reviewer")
    elif typ == "event_msg" and pt == "task_started":
        st["turn"], st["end"], st["ask"], st["esc"] = {"id": pl.get("turn_id"), "start": ts}, None, None, {}
    elif typ == "event_msg" and pt in ("task_complete", "turn_aborted"):
        if st["turn"] is not None and pl.get("turn_id") not in (None, st["turn"]["id"]):
            return  # the end of another turn: the open one stays open
        _end_turn(st, "complete" if pt == "task_complete" else "aborted")
        if pt == "task_complete":
            msg = pl.get("last_agent_message")
            st["text"] = msg if isinstance(msg, str) else None
            st["error"] = pl.get("error")
    elif typ == "response_item" and pt == "message" and pl.get("role") == "user":
        st["ask"] = None
    elif typ == "response_item" and pt in ("function_call", "custom_tool_call") and st["turn"] is not None:
        when = ts if ts is not None else st["last"]
        arg = pl.get("arguments") if pt == "function_call" else pl.get("input")
        if pt == "function_call" and pl.get("name") == QUESTION_CALL:
            st["ask"] = when
        elif (isinstance(arg, str) and "sandbox_permissions" in arg and "require_escalated" in arg
              and isinstance(pl.get("call_id"), str) and when is not None):
            st["esc"][pl["call_id"]] = when


def _stamp_of(line: bytes) -> Optional[float]:
    m = _STAMP_RE.match(line)
    return _epoch(m.group(1).decode("ascii", "replace")) if m else None


def _scan(st: dict, whole: bytes) -> None:
    """Whole lines (ending in a newline) into the state: the header line, then only the needle lines, in order."""
    pos = 0
    if not st["started"]:
        nl = whole.find(b"\n")
        _apply(st, whole[:nl], True)
        st["started"] = True
        pos = nl + 1
    starts = set()
    for n in _NEEDLES:
        i = whole.find(n, pos)
        while i >= 0:
            starts.add(whole.rfind(b"\n", 0, i) + 1)
            i = whole.find(n, whole.find(b"\n", i))
    for m in _USER_RE.finditer(whole, pos):
        starts.add(whole.rfind(b"\n", 0, m.start()) + 1)
    for start in sorted(starts):
        _apply(st, whole[start:whole.find(b"\n", start)], False)
    if len(whole) > pos:
        t = _stamp_of(whole[whole.rfind(b"\n", 0, len(whole) - 1) + 1:-1])
        if t is not None:
            st["last"] = t


def _refresh(path: Path, stat) -> dict:
    """The cache entry of one rollout, brought up to date (as snapshot._refresh): key (size, mtime_ns, inode);
    same -> nothing read; grown -> only the bytes after the last whole line; shrunk or another inode -> from
    the start. The view is the running state plus the unfinished last line, judged on a copy."""
    k = str(path)
    key = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
    e = _CACHE.get(k)
    if e is not None and e["key"] == key:
        return e
    if e is None or e["ino"] != stat.st_ino or stat.st_size < e["offset"]:
        e = {"key": None, "ino": stat.st_ino, "offset": 0, "state": _fresh(), "view": None}
    _CACHE.pop(k, None)  # back in only once the read finished
    rest = b""
    with open(path, "rb") as fh:
        fh.seek(e["offset"])
        pos = e["offset"]
        while pos < stat.st_size:
            block = fh.read(min(_BLOCK, stat.st_size - pos))
            if not block:
                break
            pos += len(block)
            READ["codex"] += len(block)
            data = rest + block
            cut = data.rfind(b"\n") + 1
            if cut:
                _scan(e["state"], data[:cut])
            rest = data[cut:]
    e["offset"] = pos - len(rest)
    view = e["state"]
    if rest:
        view = copy.deepcopy(view)
        _apply(view, rest, not view["started"])
        view["started"] = True
        view["bad"] = e["state"]["bad"]  # a line still being written is not a bad line
        t = _stamp_of(rest)
        if t is not None:
            view["last"] = t
    e["view"] = view
    e["key"] = key
    _CACHE[k] = e
    return e


def _names(path: Path) -> tuple:
    """({thread id: thread name}, whether a line was unreadable), from session_index.jsonl, incrementally."""
    try:
        stat = os.stat(path)
    except OSError:
        _INDEX.clear()
        return {}, False
    key = (stat.st_size, stat.st_mtime_ns, stat.st_ino)
    if _INDEX.get("key") == key:
        return _INDEX["names"], _INDEX["bad"]
    if _INDEX.get("ino") != stat.st_ino or stat.st_size < _INDEX.get("offset", 0):
        _INDEX.clear()
        _INDEX.update(ino=stat.st_ino, offset=0, names={}, bad=False)
    with open(path, "rb") as fh:
        fh.seek(_INDEX["offset"])
        data = fh.read(stat.st_size - _INDEX["offset"])
    READ["codex"] += len(data)
    cut = data.rfind(b"\n") + 1
    for line in data[:cut].split(b"\n")[:-1]:
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            _INDEX["bad"] = True
            continue
        if not isinstance(rec, dict):
            _INDEX["bad"] = True
            continue
        if isinstance(rec.get("id"), str) and isinstance(rec.get("thread_name"), str) and rec["thread_name"].strip():
            _INDEX["names"][rec["id"]] = rec["thread_name"]
    _INDEX["offset"] += cut
    _INDEX["key"] = key if cut == len(data) else None  # an unfinished last line is read again next time
    return _INDEX["names"], _INDEX["bad"]


# ---------------------------------------------------------------- the processes

def _etime(s: str) -> Optional[int]:
    m = _ETIME_RE.match(s)
    if not m:
        return None
    d, h, mi, se = (int(x) if x else 0 for x in m.groups())
    return ((d * 24 + h) * 60 + mi) * 60 + se


def _ps_failed(problems: list) -> None:
    if "codex: ps did not answer" not in problems:
        problems.append("codex: ps did not answer")


def _processes(now: float, problems: list) -> tuple:
    """([{"pid", "start", "pwd", "pane"}] of the terminal Codex processes, whether the desktop app is alive).
    A terminal Codex: basename in TERMINAL_BASENAMES, no ancestor (PARENT_LEVELS up) that is the app or another
    Codex, argv's first token not DAEMON_TOKEN. Only the pid, the parent pid, the elapsed time and whether the
    program is a Codex or the app leave the first `ps`; only PWD, TERN_PANE and the daemon yes/no leave the
    second. Failure -> ([], False) and one problem line."""
    fake_path = os.environ.get(ENV_CODEX_PS_JSON)
    fake = None
    try:
        if fake_path:
            fake = json.loads(Path(fake_path).read_text(encoding="utf-8"))
            if not isinstance(fake, dict) or not isinstance(fake.get("comm"), list):
                raise ValueError
            lines = fake["comm"]
        else:
            proc = subprocess.run(["ps", "-axo", "pid=,ppid=,etime=,comm="], capture_output=True, text=True,
                                  errors="replace", timeout=snapshot.PS_TIMEOUT_S, check=False)
            if not proc.stdout.strip() and proc.returncode != 0:
                raise RuntimeError
            lines = proc.stdout.splitlines()
            del proc
    except Exception:  # noqa: BLE001 - nothing of the line or the error goes out
        _ps_failed(problems)
        return [], False
    parent, app, found, program = {}, set(), {}, {}
    for line in lines:
        m = _COMM_RE.match(line) if isinstance(line, str) else None
        if not m:
            continue
        pid, comm = int(m.group(1)), m.group(4)
        parent[pid] = int(m.group(2))
        if comm == APP_PROCESS_PATH:
            app.add(pid)
        elif os.path.basename(comm) in TERMINAL_BASENAMES:
            el = _etime(m.group(3))
            if el is not None:
                found[pid] = now - el
                program[pid] = comm  # only to strip it off the argv line in _read_env
        del comm
    del lines
    terms = {}  # a codex none of whose ancestors is the app or another codex (the TUI's children are not)
    for pid, start in found.items():
        p, ok = pid, True
        for _ in range(PARENT_LEVELS):
            p = parent.get(p)
            if p is None or p <= 1:
                break
            if p in app or p in found:
                ok = False
                break
        if ok:
            terms[pid] = start
    for p in [p for p in _ENVS if p not in terms]:
        del _ENVS[p]
    new = [p for p in terms if p not in _ENVS or abs(_ENVS[p][0] - terms[p]) > START_SLACK_S]
    if new:
        got = _read_env({p: program[p] for p in new}, fake, problems)
        if got is not None:
            for p in new:
                _ENVS[p] = (terms[p],) + got.get(p, (None, None, False))
    del program
    # `codex resume` is a new process writing on in the same rollout: it is matched once its first record lands
    # (the thread's last record is then after the process started).
    out = [{"pid": p, "start": terms[p], "pwd": _ENVS[p][1], "pane": _ENVS[p][2]}
           for p in terms if p in _ENVS and not _ENVS[p][3]]  # an `app-server` daemon is never a session
    return out, bool(app)


def _first_token(command: str, prog: str) -> Optional[str]:
    """argv's first token after the program in a `ps` command line. The program is the comm path (it may hold
    spaces) when the line starts with it, else argv[0] is a bare name, up to the first space. Only a plain
    word is returned (`app-server`, `resume`); a flag, a path or nothing is None."""
    if command.startswith(prog):
        rest = command[len(prog):]
    else:
        parts = command.split(None, 1)
        rest = " " + parts[1] if len(parts) > 1 else ""
    m = _TOKEN_RE.match(rest)
    return m.group(1) if m else None


def _read_env(programs: dict, fake, problems: list) -> Optional[dict]:
    """{pid: (PWD or None, pane or None, is the app-server daemon)} from one `ps eww` over the pids of
    `programs` ({pid: comm path}); PWD and TERN_PANE are the last match of each, the daemon is told by argv's
    first token alone. None on failure."""
    pids = list(programs)
    try:
        if fake is not None:
            lines = fake.get("env")
            if not isinstance(lines, list):
                raise ValueError
        else:
            proc = subprocess.run(["ps", "eww", "-o", "pid=,command=", "-p", ",".join(str(p) for p in pids)],
                                  capture_output=True, text=True, errors="replace", timeout=snapshot.PS_TIMEOUT_S,
                                  check=False)
            if not proc.stdout.strip() and proc.returncode != 0:
                raise RuntimeError
            lines = proc.stdout.splitlines()
            del proc
    except Exception:  # noqa: BLE001 - nothing of the line or the error goes out
        _ps_failed(problems)
        return None
    wanted, out = set(pids), {}
    for line in lines:
        m = snapshot._PID_RE.match(line) if isinstance(line, str) else None
        if not m or int(m.group(1)) not in wanted:
            continue
        pid = int(m.group(1))
        pwd = _PWD_RE.findall(line)
        pane = _TPANE_RE.findall(line)
        daemon = _first_token(line[m.end():].lstrip(), programs[pid]) == DAEMON_TOKEN
        out[pid] = (pwd[-1] if pwd else None, int(pane[-1]) if pane else None, daemon)
    del lines
    return out


# ---------------------------------------------------------------- the state rule

def codex_state(v: dict, now: float, config=None) -> dict:
    """The state of one Codex thread from its running state `v` (rules 2-6 of the module docstring; rule 1,
    liveness, is the caller's). Returns the shape of snapshot.session_state, plus "inferred"."""
    if v["turn"] is not None:
        if v["ask"] is not None:  # 2
            r = {"state": "waiting", "waiting_for": "question or form", "since": v["ask"]}
            if not QUESTION_WRITTEN:
                r["inferred"] = True
            return r
        reviewer = v["reviewer"] if v["reviewer"] is not None else "user"  # older rollouts have no reviewer
        if v["esc"] and v["policy"] in ASKING_POLICIES and reviewer == "user":  # 3
            first = min(v["esc"].values())
            if now - first > PERMISSION_WAIT_S:
                return {"state": "waiting", "waiting_for": "permission request", "inferred": True, "since": first}
        if v["last"] is not None and now - v["last"] > snapshot.STUCK_S:
            return {"state": "stopped", "stuck": True}
        r = {"state": "working"}
        if v["turn"]["start"] is not None:
            r["since"] = v["turn"]["start"]
        return r
    if v["end"] == "complete":  # 4
        if v["error"] is not None:
            return {"state": "stopped"}
        return snapshot.turn_end_state(v["text"], config)
    return {"state": "stopped"}  # 5, 6


def _helper_state(v: dict, mt: float, now: float, mother_busy: bool):
    """(state, since) of a helper thread, or None when it has left the map (snapshot.read_helpers' rule)."""
    if v["turn"] is None and v["end"] is not None:
        if now - mt > snapshot.GONE_S:
            return None
        return ("done" if v["end"] == "complete" else "closed"), mt
    last = max(mt, v["last"] or mt)
    silent = now - last
    if (silent > snapshot.DEAD_S and not mother_busy) or silent > snapshot.SILENT_MAX_S:
        if silent > snapshot.DEAD_S + snapshot.GONE_S:
            return None
        return "closed", last
    return "working", (v["turn"] or {}).get("start") or mt


# ---------------------------------------------------------------- the source

def _walk(sessions: Path) -> list:
    """[(path, stat)] of every rollout under sessions/YYYY/MM/DD."""
    out = []
    for d1 in sorted(os.scandir(sessions), key=lambda x: x.name):
        if not d1.is_dir():
            continue
        for d2 in sorted(os.scandir(d1.path), key=lambda x: x.name):
            if not d2.is_dir():
                continue
            for d3 in sorted(os.scandir(d2.path), key=lambda x: x.name):
                if not d3.is_dir():
                    continue
                for f in sorted(os.scandir(d3.path), key=lambda x: x.name):
                    if f.name.startswith("rollout-") and f.name.endswith(".jsonl") and f.is_file():
                        try:
                            out.append((Path(f.path), f.stat()))
                        except OSError:
                            continue
    return out


class Codex:
    """The second session source: Codex threads, terminal and desktop app."""
    name = "Codex"

    def read(self, ctx) -> SourceResult:
        if sandboxed():
            return SourceResult(nodes=[], lines=[])
        base = codex_dir()
        sessions = base / "sessions"
        if not sessions.is_dir():
            _CACHE.clear()
            return SourceResult(nodes=[], lines=[])
        now, problems = ctx.now, ctx.problems
        terms, app_alive = _processes(now, problems)
        floor = now - snapshot.MAX_AGE_S
        if terms:  # a thread a live process may hold was written after that process started
            floor = min(floor, min(t["start"] for t in terms) - START_SLACK_S)
        threads, seen, bad = {}, set(), 0
        for path, stat in _walk(sessions):
            if stat.st_mtime < floor:
                continue
            seen.add(str(path))
            try:
                v = _refresh(path, stat)["view"]
            except OSError:
                _CACHE.pop(str(path), None)
                continue
            bad += bool(v["bad"])
            h = v["header"]
            if h and h["id"] and not h["guardian"] and h["id"] not in threads:
                threads[h["id"]] = (v, stat.st_mtime)
        for k in [k for k in _CACHE if k not in seen]:
            del _CACHE[k]
        names, names_bad = _names(base / "session_index.jsonl")
        if bad or names_bad:
            problems.append(f"codex: {bad + int(names_bad)} file(s) with unreadable lines")

        def last_of(tid):
            v, mt = threads[tid]
            return v["last"] if v["last"] is not None else mt

        tops = [t for t, (v, _) in threads.items() if not v["header"]["helper"]]
        live: dict = {}  # thread id -> its process, or None for the app
        terminal = [t for t in tops if threads[t][0]["header"]["originator"] not in DESKTOP_ORIGINATORS]
        for p in sorted(terms, key=lambda x: (-x["start"], x["pid"])):
            if p["pwd"] is None:
                continue
            match = [t for t in terminal if t not in live and threads[t][0]["header"]["cwd"] == p["pwd"]
                     and last_of(t) + START_SLACK_S >= p["start"]]
            if match:
                live[max(match, key=last_of)] = p
        if app_alive:
            for t in tops:
                if threads[t][0]["header"]["originator"] in DESKTOP_ORIGINATORS:
                    live[t] = None
        tabs = ctx.tabs() if any(p is not None and p["pane"] is not None for p in live.values()) else {}

        children: dict = {}
        for t, (v, mt) in threads.items():
            if v["header"]["helper"] and v["header"]["parent"]:
                children.setdefault(v["header"]["parent"], []).append(t)

        nodes, lines, hidden, hidden_n = [], [], set(), 0
        order = sorted(live, key=lambda t: (threads[t][0]["header"]["start"] or 0, snapshot._hash(t, 12)))
        for t in order:
            v, mt = threads[t]
            p = live[t]
            r = codex_state(v, now, ctx.config)
            if p is None:
                kind, where = "app", "Codex app"
            elif p["pane"] is not None:
                n = (tabs.get(p["pane"]) or {}).get("number")
                kind, where = "tern", (f"Codex · Tern · tab {n}" if isinstance(n, int) else "Codex · Tern")
            else:
                kind, where = "other", "Codex · other terminal"
            nid = "x:" + snapshot._hash(t, 12)
            node = {"id": nid, "agent": "codex", "kind": kind,
                    "title": snapshot._clean(names.get(t) or "") or "unnamed session", "where": where,
                    "state": r["state"], "since": snapshot._iso(r["since"] if "since" in r else last_of(t))}
            if kind == "tern":
                node["pane"] = p["pane"]
            if "waiting_for" in r:
                node["waiting_for"] = r["waiting_for"]
            if r.get("guessed"):
                node["guessed"] = True
            if r.get("inferred"):
                node["inferred"] = True
            helpers = self._helpers(t, nid, threads, children, now, r["state"] == "working")
            if r["state"] in ("done", "stopped") and now - max(last_of(t), mt) > snapshot.MAX_AGE_S:
                hidden_n += 1  # the card and its helpers stay out
                hidden.add(nid)
                hidden.update(h["id"] for h, _ in helpers)
                continue
            nodes.append(node)
            for h, frm in helpers:
                nodes.append(h)
                lines.append({"from": frm, "to": h["id"], "kind": "called"})
        return SourceResult(nodes=nodes, lines=lines, hidden_n=hidden_n, hidden=hidden)

    @staticmethod
    def _helpers(tid, nid, threads, children, now, mother_busy) -> list:
        """[(helper node, the id it is called from)] under thread `tid`, helpers of helpers included."""
        out, todo, done = [], [(tid, nid)], {tid}
        while todo:
            parent, pid = todo.pop(0)
            for c in sorted(children.get(parent, []), key=lambda x: threads[x][1]):
                if c in done:
                    continue
                done.add(c)
                v, mt = threads[c]
                s = _helper_state(v, mt, now, mother_busy)
                if s is None:
                    continue
                hid = "a:" + snapshot._hash(c, 8)
                out.append(({"id": hid, "agent": "codex", "kind": "helper",
                             "title": snapshot._clean(v["header"]["nickname"] or "") or "helper",
                             "where": "Codex helper", "state": s[0], "since": snapshot._iso(s[1])}, pid))
                todo.append((c, hid))
        return out
