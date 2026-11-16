"""The snapshot of the live session map: nodes and lines, read from disk, never from a model.

Sources, each read on its own (one that fails adds a line to `problems` and the rest still comes out):
- the session registry Claude Code keeps, `~/.claude/sessions/<pid>.json` (`SESSION_MAP_REGISTRY_DIR`),
  a session is live when its process answers `os.kill(pid, 0)`;
- one `ps eww` over the live pids (`SESSION_MAP_PS_JSON`: a JSON list of `ps` lines, for tests); only the
  Tern pane number and each adapter's terminal handle leave it, each through an anchored regex; the rest of
  every line is dropped in `_read_ps`. Handles never enter the snapshot;
- `tern ls --json` through `tern.read_tabs` (`SESSION_MAP_TERN_JSON`);
- the orchestrator adapters named in `SESSION_MAP_ADAPTERS` (see `orchestrator.py`);
- each live session's conversation and its helpers' `subagents/agent-*.meta.json`, under the conversations
  folder of the session's own cwd (`SESSION_MAP_PROJECTS_DIR` replaces `~/.claude/projects`).

The cache lives in this module's memory, so a server that imports this module and calls `snapshot()` every
few seconds benefits; a one-off CLI run starts cold. Per conversation file (and per helper transcript), keyed
by (size, mtime_ns, inode): same key -> zero bytes read; grown -> only the new bytes are scanned for titles
and background commands (a last line with no newline yet is judged on a copy and read again once it ends),
and at most the last WINDOW bytes for the last utterance; shrunk or another inode -> read again from the start.
`READ` counts the bytes read, for the tests. `merge_closed` keeps a session that died as `closed` for
GONE_S; it is the server's to call, with the previous snapshot it remembers.
Python 3.9+ stdlib. CLI exit codes: 0 ok, 1 error, 2 bad arguments.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from . import orchestrator, tern, transcript

PAGE = Path(__file__).resolve().parent / "page.html"
DATA_TAG = '<script id="data" type="application/json">null</script>'

GONE_S = 600  # a finished helper (and a dead one, and a vanished session) leaves the map this long after it stopped
DEAD_S = 300  # an unfinished helper silent this long, with its mother not busy, is dead
SILENT_MAX_S = 900  # ... and silent this long it is dead even with its mother busy (old metas never get `finished`)
STUCK_S = 15 * 60  # busy with the conversation untouched for longer is stuck: stopped
MAX_AGE_S = 8 * 3600  # a done/stopped card with no change for longer is left out (hidden_n)
PS_TIMEOUT_S = 3
TITLE_MAX = 80

KNOWN_STATUSES = ("idle", "busy", "waiting", "shell")
EXPECTED_FIELDS = ("pid", "sessionId", "cwd", "status", "kind", "statusUpdatedAt")
SEEN_VERSIONS = ("2.1.322",)  # Claude Code versions whose registry format this reader was checked against

ENV_REGISTRY = "SESSION_MAP_REGISTRY_DIR"
ENV_PROJECTS = "SESSION_MAP_PROJECTS_DIR"
ENV_PS_JSON = "SESSION_MAP_PS_JSON"
ENV_DONE_LINES = "SESSION_MAP_DONE_LINES"
ENV_WAIT_MARKS = "SESSION_MAP_WAIT_MARKS"

_PANE_RE = re.compile(r"(?:^|\s)TERN_PANE=(\d+)(?:\s|$)")
_PID_RE = re.compile(r"^\s*(\d+)\s")
_SIGN_RE = re.compile(r"^[\u2800-\u28FF\u2733\u273B\u273D\u2736\u2722\u2726\u2727\u00B7\u2022*]+\s*")
_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_PATH_RE = re.compile(r"(?:/(?:Users|home)/|~/)\S*")  # a home path in a title
_ENV_ASSIGN_RE = re.compile(r"\b[A-Z][A-Z0-9_]*=\S*")
_DISK_RE = re.compile(r"""(?:^|(?<=[\s'"=(\[]))(?:~|\$\{?HOME\}?)?/[^\s'"\])]+""")  # a disk path in a problem

WINDOW = 64 * 1024  # at most this much of a conversation's end is read for its last utterance
_CEILING = 2 * 1024 * 1024  # the tail reader never reads more than this much of a conversation's end
_BLOCK = 4 * 1024 * 1024  # the scan reads new bytes in blocks this size
READ = {"scan": 0, "tail": 0}  # conversation bytes read by this process: the scan and the tail (tests)
_CACHE: dict = {}  # str(path) -> the running state of one conversation file (see _refresh)
_USED: set = set()  # paths touched by the current join; the others leave the cache at its end
_LOCK = threading.RLock()  # one join at a time: the cache is shared by a server's threads
_VANISHED: dict = {}  # node id -> epoch it vanished, for merge_closed (the server's memory)
_HIDDEN: set = set()  # ids the last join left out on purpose (the age cut), for merge_closed
_PANES: dict = {}  # pid -> (startedAt, Tern pane or None, {adapter index: handle}): `ps eww` only for a new pid
_ENV_RES: list = []  # [(adapter index, compiled handle regex)] of the adapters in use
_ENV_KEY: tuple = ()  # the patterns _PANES was filled with; another set empties it
_ADAPTERS: dict = {"env": None, "adapters": [], "problems": []}  # loaded once per SESSION_MAP_ADAPTERS value
_MATCHED: dict = {}  # pid -> (adapter index, current tab handle or None), from the last join (focus_target)
_IN_USE: list = []  # the adapters of the last join

WHERE = {"background": "in the background", "other": "other terminal", "helper": "helper"}


# ---------------------------------------------------------------- configuration

@dataclass(frozen=True)
class Config:
    """Optional turn markers. `done_lines`: a last line equal to one of these means done; `wait_marks`: any of
    these anywhere in the last text means waiting. Both empty by default."""
    done_lines: Tuple[str, ...] = ()
    wait_marks: Tuple[str, ...] = ()


def _env_list(name: str) -> Tuple[str, ...]:
    return tuple(x.strip() for x in (os.environ.get(name) or "").split("|") if x.strip())


def read_config() -> Config:
    """Config from SESSION_MAP_DONE_LINES and SESSION_MAP_WAIT_MARKS (each a `|`-separated list)."""
    return Config(done_lines=_env_list(ENV_DONE_LINES), wait_marks=_env_list(ENV_WAIT_MARKS))


# ---------------------------------------------------------------- paths and time

def _claude_base() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude"))


def registry_dir() -> Path:
    env = os.environ.get(ENV_REGISTRY)
    return Path(env) if env else _claude_base() / "sessions"


def projects_dir() -> Path:
    env = os.environ.get(ENV_PROJECTS)
    return Path(env) if env else _claude_base() / "projects"


def conversations_folder(cwd: str) -> Path:
    """The conversations folder of a session, by its own cwd."""
    return projects_dir() / transcript.encode_cwd(cwd)


def _mtime(p) -> Optional[float]:
    try:
        return Path(p).stat().st_mtime
    except OSError:
        return None


def _iso(epoch) -> Optional[str]:
    """Local time, ISO with offset, to the second."""
    try:
        return dt.datetime.fromtimestamp(float(epoch)).astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _iso_ms(ms) -> Optional[str]:
    return _iso(ms / 1000) if isinstance(ms, (int, float)) and not isinstance(ms, bool) else None


def _hash(text: str, n: int) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:n]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


# ---------------------------------------------------------------- the registry

def read_registry(problems: list, signals: Optional[dict] = None) -> list:
    """The live sessions of the registry: one dict per file whose process answers. Tolerant: a missing field
    falls to the disk rules and counts as a problem. `signals["unknown_format"]` turns True when the registry
    looks unlike the known format: its folder is missing, a file holds no readable pid (a JSON that is not an
    object, or no pid in it nor in its name), or an expected field is missing in a file of a version not yet
    seen. A file that does not parse (it may be mid-write) is only a problem."""
    signals = {} if signals is None else signals
    d = registry_dir()
    if not d.is_dir():
        signals["unknown_format"] = True
        return []
    try:
        files = sorted(d.glob("*.json"))
    except OSError:
        problems.append("session registry: could not read the folder")
        return []
    out, unreadable, off, unknown = [], 0, False, False
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            unreadable += 1
            continue
        pid = data.get("pid") if isinstance(data, dict) else None
        if not isinstance(pid, int) or isinstance(pid, bool):
            try:
                pid = int(f.stem) if isinstance(data, dict) else None
            except ValueError:
                pid = None
        if pid is None:
            off = True
            signals["unknown_format"] = True
            continue
        if any(k not in data for k in EXPECTED_FIELDS):
            off = True
            if data.get("version") not in SEEN_VERSIONS:
                signals["unknown_format"] = True
        if not _alive(pid):
            continue
        status = data.get("status")
        if status not in KNOWN_STATUSES:
            unknown = True
        out.append({
            "pid": pid,
            "sid": data.get("sessionId") if isinstance(data.get("sessionId"), str) else None,
            "cwd": data.get("cwd") if isinstance(data.get("cwd"), str) else None,
            "kind": data.get("kind"),
            "tmux": bool(data.get("tmux")),
            "status": status,
            "waitingFor": data.get("waitingFor"),
            "name": data.get("name") if isinstance(data.get("name"), str) else None,
            "nameSource": data.get("nameSource"),
            "statusUpdatedAt": data.get("statusUpdatedAt"),
            "startedAt": data.get("startedAt") if _num(data.get("startedAt")) else 0,
            "version": data.get("version"),
            "alive": True,
        })
    if unreadable:
        problems.append(f"session registry: {unreadable} unreadable file(s)")
    if off:
        problems.append("session registry: not in the known format" if signals.get("unknown_format")
                        else "session registry: an expected field is missing")
    if unknown:
        problems.append("session registry: an unknown status (treated as stopped)")
    out.sort(key=lambda s: (s["startedAt"], s["pid"]))
    return out


# ---------------------------------------------------------------- the process environment (ps)

def _use_adapters(adapters: list) -> None:
    """Point the ps reader at these adapters' handle regexes; another set of patterns empties the ps cache."""
    global _ENV_RES, _ENV_KEY
    res = []
    for i, a in enumerate(adapters):
        rx = orchestrator.env_regex(a)
        if rx is not None:
            res.append((i, rx))
    key = tuple((i, rx.pattern) for i, rx in res)
    if key != _ENV_KEY:
        _PANES.clear()
    _ENV_RES, _ENV_KEY = res, key


def read_panes(pids, problems: list) -> dict:
    """{pid: Tern pane number} from one `ps eww` over `pids`. Only `TERN_PANE` and the adapters' handles leave
    `_read_ps`: every line is matched by anchored regexes and dropped there, and no failure message is built
    from a line (any failure is just "ps did not answer"). A process's environment never changes, so each
    answer is kept in this process (`_PANES`, keyed by pid and, when `pids` is a {pid: startedAt} dict, by its
    start too) and `ps` runs only for pids not seen yet; a pid no longer asked about leaves the cache."""
    return {p: v[0] for p, v in _env(pids, problems).items() if v[0] is not None}


def read_handles(pids, problems: list) -> dict:
    """{pid: {adapter index: handle}}, read_panes' sibling over the same `ps` and the same cache."""
    return {p: dict(v[1]) for p, v in _env(pids, problems).items() if v[1]}


def _env(pids, problems: list, prune: bool = True) -> dict:
    """{pid: (pane or None, {adapter index: handle})} through `_PANES`; `ps` only for pids not seen yet (or seen
    with another start); `prune` drops the pids no longer asked about."""
    ident = dict(pids) if isinstance(pids, dict) else {p: None for p in pids}
    ident = {p: v for p, v in ident.items() if isinstance(p, int) and not isinstance(p, bool)}
    if prune:
        for p in [p for p in _PANES if p not in ident]:
            del _PANES[p]
    out = {p: _PANES[p][1:] for p in ident if p in _PANES and _PANES[p][0] == ident[p]}
    new = [p for p in ident if p not in out]
    if not new:
        return out
    got = _read_ps(new, problems)
    if got is None:
        return out
    for p in new:
        if p in got:
            _PANES[p] = (ident[p],) + got[p]
            out[p] = got[p]
    return out


def _read_ps(pids: list, problems: list) -> Optional[dict]:
    """{pid: (pane or None, {adapter index: handle})} for every pid `ps` listed; None when it failed."""
    path = os.environ.get(ENV_PS_JSON)
    try:
        if path:
            lines = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(lines, list):
                raise ValueError
        else:
            proc = subprocess.run(["ps", "eww", "-o", "pid=,command=", "-p", ",".join(str(p) for p in pids)],
                                  capture_output=True, text=True, errors="replace", timeout=PS_TIMEOUT_S,
                                  check=False)
            if not proc.stdout.strip() and proc.returncode != 0:
                raise RuntimeError
            lines = proc.stdout.splitlines()
            del proc
    except Exception:  # noqa: BLE001 - nothing of the line or the error goes out
        problems.append("ps did not answer")
        return None
    wanted = set(pids)
    out: dict = {}
    for line in lines:
        if not isinstance(line, str):
            continue
        m = _PID_RE.match(line)
        if not m or int(m.group(1)) not in wanted:
            continue
        p = _PANE_RE.search(line)
        handles = {}
        for i, rx in _ENV_RES:
            h = rx.search(line)
            if h and orchestrator.HANDLE_RE.fullmatch(h.group(1)):
                handles[i] = h.group(1)
        out[int(m.group(1))] = (int(p.group(1)) if p else None, handles)
    del lines
    return out


def focus_target(pid: int):
    """(adapter, handle) to bring the orchestrated session `pid` to the front, or None. The handle comes from
    the process environment (the ps cache, else one `ps` for that pid alone, not kept), which survives a
    `/clear`; else the current tab handle of the step `pid` was matched to in the last snapshot."""
    if not isinstance(pid, int) or isinstance(pid, bool):
        return None
    with _LOCK:
        m = _MATCHED.get(pid)
        if m is None or m[0] >= len(_IN_USE):
            return None
        index, step_handle = m
        adapter = _IN_USE[index]
        if pid in _PANES:
            handle = _PANES[pid][2].get(index)
        else:
            got = _read_ps([pid], [])
            handle = got.get(pid, (None, {}))[1].get(index) if got else None
    handle = handle or step_handle
    if isinstance(handle, str) and orchestrator.HANDLE_RE.fullmatch(handle):
        return adapter, handle
    return None


# ---------------------------------------------------------------- adapters

def _adapters(problems: list) -> list:
    """The adapters of SESSION_MAP_ADAPTERS, imported once per value of that variable; their load problems
    are repeated in every snapshot."""
    env = os.environ.get(orchestrator.ENV_ADAPTERS, "")
    if _ADAPTERS["env"] != env:
        found: list = []
        _ADAPTERS.update(env=env, adapters=orchestrator.load_adapters(found), problems=found)
    problems.extend(_ADAPTERS["problems"])
    return list(_ADAPTERS["adapters"])


def _steps_of(adapter, live_sids: set, live_handles: set, problems: list) -> list:
    """The adapter's valid steps, first of each key. An adapter may expose an optional `problems` list
    attribute (refreshed by each `steps()` call) for non-fatal trouble; its lines join the snapshot's."""
    raw = adapter.steps(set(live_sids), set(live_handles))
    extra = getattr(adapter, "problems", None)
    for p in extra if isinstance(extra, (list, tuple)) else []:
        if isinstance(p, str):
            problems.append(f"adapter {adapter.name}: {p}")
    if not isinstance(raw, (list, tuple)):
        raise TypeError("steps() did not return a list")
    out, seen = [], set()
    for st in raw:
        if not orchestrator.validate_step(st, problems):
            continue
        if st.key in seen:
            problems.append(f"adapter {adapter.name}: dropped a second step with key {st.key!r}")
            continue
        seen.add(st.key)
        out.append(st)
    return out


# ---------------------------------------------------------------- helpers (subagents)

def read_helpers(folder: Path, sid: str, mother_busy: bool, now: float, problems: list) -> tuple:
    """(helpers to show, every helper transcript of the session) under <folder>/<sid>/subagents.
    finished -> done, gone GONE_S after the meta's mtime; unfinished -> working until the dead rule:
    silent > DEAD_S with the mother not busy (or silent > SILENT_MAX_S at all) -> closed, gone GONE_S later."""
    d = folder / sid / "subagents"
    if not d.is_dir():
        return [], []
    try:
        metas = sorted(d.glob("agent-*.meta.json"))
        transcripts = sorted(d.glob("agent-*.jsonl"))
    except OSError:
        problems.append("helpers: could not read a folder")
        return [], []
    out, unreadable = [], 0
    for m in metas:
        aid = m.name[len("agent-"):-len(".meta.json")]
        try:
            data = json.loads(m.read_text(encoding="utf-8"))
            mt = m.stat().st_mtime
        except (OSError, ValueError):
            unreadable += 1
            continue
        if not isinstance(data, dict):
            unreadable += 1
            continue
        try:
            jl_mt = (d / f"agent-{aid}.jsonl").stat().st_mtime
        except OSError:
            jl_mt = mt
        if data.get("finished") is True:
            if now - mt > GONE_S:
                continue
            state, since = "done", mt
        else:
            silent = now - max(mt, jl_mt)
            if (silent > DEAD_S and not mother_busy) or silent > SILENT_MAX_S:
                if silent > DEAD_S + GONE_S:
                    continue
                state, since = "closed", max(mt, jl_mt)
            else:
                state, since = "working", mt
        team = data.get("teamName") if isinstance(data.get("teamName"), str) else None
        agent_type = data.get("agentType") if isinstance(data.get("agentType"), str) else None
        desc = data.get("description") if isinstance(data.get("description"), str) else None
        parent = data.get("parentAgentId") if isinstance(data.get("parentAgentId"), str) else None
        out.append({"aid": aid, "parent": parent, "state": state, "since": since,
                    "title": desc or agent_type or "helper",
                    "where": f"team {team}" if team else (agent_type or "helper")})
    if unreadable:
        problems.append(f"helpers: {unreadable} unreadable file(s)")
    return out, transcripts


# ---------------------------------------------------------------- the conversation's tail and the state rule

def read_tail(path) -> dict:
    """{"exists", "last", "text", "changed", "background"}: the last accepted record, the last assistant text,
    the epoch of the last timestamped record ("changed", the last change) and the background command ids still
    running, through the cache (_refresh, _tail). A copy: the caller may change it."""
    if path is None or not Path(path).is_file():
        return {"exists": False, "last": None, "text": None, "changed": None}
    try:
        e = _refresh(Path(path))
        if e is None:
            return {"exists": False, "last": None, "text": None, "changed": None}
        if e["view"]["tail"] is None:
            r = _tail(Path(path), e["key"][0])
            e["view"]["tail"] = {"exists": True, "last": r["last"],
                                 "text": transcript.text(r["said"]) if r["said"] else None,
                                 "changed": r["stamp"], "background": e["view"]["background"]}
        return dict(e["view"]["tail"])
    except OSError:
        _CACHE.pop(str(path), None)
        return {"exists": True, "last": None, "text": None, "changed": None}


def read_titles(path) -> dict:
    """{"customTitle", "aiTitle"}: the last title of each kind in the conversation, through the cache.
    Missing or unreadable -> both None."""
    try:
        e = _refresh(Path(path))
    except OSError:
        _CACHE.pop(str(path), None)
        return {"customTitle": None, "aiTitle": None}
    if e is None:
        return {"customTitle": None, "aiTitle": None}
    return dict(e["view"]["titles"])


def waiting_in_words(waiting_for) -> str:
    w = str(waiting_for or "").lower()
    if w == "input needed":
        return "question or form"
    if "permission" in w or "request" in w:
        return "permission request"
    if "dialog" in w:
        return "dialog open"
    return "your decision"


def _question_line(line: str) -> bool:
    """`?` at the end of the line (closing marks allowed after it), or a whole parenthesis holding a `?`
    ("(Want another pass? Just say so.)")."""
    s = line.strip()
    if s.rstrip(")]}*_\"'\u201d\u00bb\u2019 ").endswith("?"):
        return True
    return s.startswith("(") and s.endswith(")") and "?" in s


def _working(changed) -> dict:
    r = {"state": "working"}
    if _num(changed):
        r["since"] = changed
    return r


def session_state(node, registry, step, tail, config: Optional[Config] = None) -> dict:
    """The one rule of a session's state, top to bottom; the first signal that exists decides.
    `node`: {"now": epoch} (optional); `registry`: the registry dict or None; `step`: the orchestrator Step whose
    CURRENT tab this session is, or None; `tail`: read_tail's dict, with "changed" = the last timestamped change
    of the conversation (or of a helper's transcript); `config`: the optional turn markers.
    Returns {"state", optional "waiting_for", "guessed", "stuck", and "since" (epoch) for working}.
    1. no registry or dead -> closed. 2. waiting -> waiting, with what it waits for in words.
    3. busy -> working, unless the conversation is untouched for more than STUCK_S -> stopped and stuck (the
       registry's statusUpdatedAt does not move during a long busy run, so it cannot tell how long work has
       gone on). "Untouched" counts the last timestamped record, never the file's mtime.
    4. a step verdict: done, closed or waiting.
    5. the turn ended: background work (a pending helper, or a background command with no return) -> working,
       bounded like busy; no text -> stopped; a last line among `done_lines` -> done; any `wait_marks` ->
       waiting; a question in the last three lines -> waiting and guessed; else done.
    6. otherwise (a user record after the end, an assistant record with no end, nothing yet) -> stopped.
    idle, shell (a turn that ended with a background shell alive) and unknown statuses all go to rules 4-6."""
    config = config or Config()
    if not registry or not registry.get("alive"):
        return {"state": "closed"}  # 1
    status = registry.get("status")
    if status == "waiting":
        return {"state": "waiting", "waiting_for": waiting_in_words(registry.get("waitingFor"))}  # 2
    tail = tail or {}
    changed = tail.get("changed")
    now = (node or {}).get("now") or time.time()
    if status == "busy":  # 3
        if _num(changed) and now - changed > STUCK_S:
            return {"state": "stopped", "stuck": True}
        return _working(changed)
    verdict = getattr(step, "verdict", None) if step is not None else None
    if verdict == "done":  # 4
        return {"state": "done"}
    if verdict == "closed":
        return {"state": "closed"}
    if verdict == "waiting":
        return {"state": "waiting"}
    last = tail.get("last")
    if transcript.is_turn_end(last):  # 5
        background = (last.get("pendingBackgroundAgentCount") or 0) > 0 or bool(tail.get("background"))
        if background:  # a background command alive for hours (a server) is not work going on
            if _num(changed) and now - changed > STUCK_S:
                return {"state": "stopped", "stuck": True}
            return _working(changed)
        text = tail.get("text")
        if text is None:
            return {"state": "stopped"}
        lines = [x.strip() for x in text.splitlines() if x.strip()]
        if lines and lines[-1].strip("*_ ") in set(config.done_lines):
            return {"state": "done"}
        if any(m in text for m in config.wait_marks if m):
            return {"state": "waiting"}
        if any(_question_line(x) for x in lines[-3:]):
            return {"state": "waiting", "guessed": True}
        return {"state": "done"}
    return {"state": "stopped"}  # 6


# ---------------------------------------------------------------- the cache

_TITLE_FIELDS = {"custom-title": "customTitle", "ai-title": "aiTitle"}
_TITLE_NEEDLES = (b'"custom-title"', b'"ai-title"')
_BG_NEEDLES = (b"background", b"task-notification")  # literals BACKGROUND_RE / RETURNED_RE need


def _new(ino) -> dict:
    return {"key": None, "ino": ino, "offset": 0,
            "titles": {"customTitle": None, "aiTitle": None},
            "bg": {"started": set(), "returned": set(), "undecodable": False}, "view": None}


def _bg_of_line(bg: dict, text: str) -> None:
    """transcript.background_ids' body for one line (it splits the whole text with splitlines, so the pieces
    are the same)."""
    for piece in text.splitlines():
        piece = piece.strip()
        if not piece:
            continue
        try:
            rec = json.loads(piece)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("isSidechain"):
            continue
        if rec.get("type") == "user":
            for b in transcript.blocks(rec):
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    for txt in transcript.tool_result_texts(b):
                        bg["started"].update(transcript.BACKGROUND_RE.findall(txt))
                elif b.get("type") == "text":
                    bg["returned"].update(transcript.RETURNED_RE.findall(str(b.get("text", ""))))
        elif rec.get("type") == "attachment":
            att = rec.get("attachment")
            if isinstance(att, dict) and att.get("type") == "queued_command":
                bg["returned"].update(transcript.RETURNED_RE.findall(str(att.get("prompt", ""))))


def _scan_line(titles: dict, bg: dict, raw: bytes) -> None:
    """One line (no newline) into the running state: titles and background ids. A line that does not decode
    leaves nothing in the background, as background_ids, which reads the whole file as UTF-8."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        bg["undecodable"] = True
        return
    if any(n in raw for n in _BG_NEEDLES):
        _bg_of_line(bg, text)
    if not any(n in raw for n in _TITLE_NEEDLES):
        return
    try:
        rec = json.loads(raw)
    except ValueError:
        return
    if not isinstance(rec, dict):
        return
    field = _TITLE_FIELDS.get(rec.get("type"))
    if field and isinstance(rec.get(field), str) and rec[field].strip():
        titles[field] = rec[field]


def _scan_block(e: dict, whole: bytes) -> None:
    """Whole lines (ending in a newline) into the state. Only the lines holding a needle (a title, a background
    literal) are parsed; when the block is valid UTF-8 (a newline never sits inside a UTF-8 sequence, so each
    line is) they are found by C-speed search, else line by line."""
    titles, bg = e["titles"], e["bg"]
    try:
        whole.decode("utf-8")
    except UnicodeDecodeError:
        for line in whole.split(b"\n")[:-1]:
            _scan_line(titles, bg, line)
        return
    starts = set()
    for n in _TITLE_NEEDLES + _BG_NEEDLES:
        i = whole.find(n)
        while i >= 0:
            starts.add(whole.rfind(b"\n", 0, i) + 1)
            i = whole.find(n, whole.find(b"\n", i))
    for start in sorted(starts):
        _scan_line(titles, bg, whole[start:whole.find(b"\n", start)])


def _refresh(path: Path) -> Optional[dict]:
    """The cache entry of one conversation file, brought up to date; None when the file is missing.
    Key (size, mtime_ns, inode): same -> nothing read; grown -> the bytes after `offset` (the end of the last
    whole line already scanned); shrunk or another inode -> from the start. The view is the running state plus
    the unfinished last line, judged on a copy. A rewrite in place that keeps or grows the size is not detected
    (Claude Code only appends). Raises OSError when the file cannot be read."""
    k = str(path)
    _USED.add(k)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        _CACHE.pop(k, None)
        return None
    key = (st.st_size, st.st_mtime_ns, st.st_ino)
    e = _CACHE.get(k)
    if e is not None and e["key"] == key:
        return e
    if e is None or e["ino"] != st.st_ino or st.st_size < e["offset"]:
        e = _new(st.st_ino)
    _CACHE.pop(k, None)  # back in only once the read finished
    rest = b""
    with open(path, "rb") as fh:
        fh.seek(e["offset"])
        pos = e["offset"]
        while pos < st.st_size:
            block = fh.read(min(_BLOCK, st.st_size - pos))
            if not block:
                break
            pos += len(block)
            READ["scan"] += len(block)
            data = rest + block
            cut = data.rfind(b"\n") + 1
            if cut:
                _scan_block(e, data[:cut])
            rest = data[cut:]
    e["offset"] = pos - len(rest)
    titles, bg = dict(e["titles"]), e["bg"]
    if rest:
        bg = {"started": set(bg["started"]), "returned": set(bg["returned"]), "undecodable": bg["undecodable"]}
        _scan_line(titles, bg, rest)
    e["view"] = {"titles": titles, "tail": None,
                 "background": set() if bg["undecodable"] else bg["started"] - bg["returned"]}
    e["key"] = key
    _CACHE[k] = e
    return e


def _stamp(rec: dict) -> Optional[float]:
    t = transcript.parse_iso(rec.get("timestamp"))
    return t.timestamp() if t is not None else None


def _tail(path: Path, size: int) -> dict:
    """{"last": last accepted record, "said": last assistant record with text, "stamp": epoch of the last record
    WITH a timestamp}, walked backwards (a trailing idle-compaction block seen through), over the last WINDOW
    bytes, and over the last _CEILING bytes only when the window lacks one of them (the text only matters when
    the last record is a turn end, the one case the state rule reads it). Never more than _CEILING: past it a
    kind is None. The timestamp, not the file's mtime, is "the last change": Claude Code appends untimestamped
    metadata (titles, mode, last prompt) that moves the mtime."""
    found: dict = {"last": None, "said": None, "stamp": None}
    for amount in (WINDOW, _CEILING):
        start = max(0, size - amount)
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read(size - start)
        READ["tail"] += len(data)
        lines = data.decode("utf-8", "replace").splitlines()
        if start > 0:
            lines = lines[1:]  # the first line of a tail is cut
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
            if found["last"] is None and transcript.accepted(rec):
                found["last"] = rec
            if found["said"] is None and transcript.has_text(rec):
                found["said"] = rec
            if found["stamp"] is None:
                found["stamp"] = _stamp(rec)
            if all(v is not None for v in found.values()):
                break
        missing = found["last"] is None or found["stamp"] is None or (
            found["said"] is None and transcript.is_turn_end(found["last"]))
        if start == 0 or not missing:
            break
    return found


def forget() -> None:
    """Empty the caches (the next read is cold, and the adapters are imported again)."""
    with _LOCK:
        _CACHE.clear()
        _PANES.clear()
        _MATCHED.clear()
        _ADAPTERS.update(env=None, adapters=[], problems=[])


# ---------------------------------------------------------------- titles

def _clean(text) -> str:
    """A title for the map: no Claude spinner sign in front, one line, no path, no UUID, no KEY=value."""
    t = _SIGN_RE.sub("", str(text or "")).strip()
    t = " ".join(t.split())
    t = _UUID_RE.sub("…", t)
    t = _PATH_RE.sub("…", t)
    t = _ENV_ASSIGN_RE.sub("…", t)
    return t if len(t) <= TITLE_MAX else t[: TITLE_MAX - 1].rstrip() + "…"


def _title_of_session(s: dict, titles: dict, tern_title) -> str:
    """A session card's title, the first that exists: the name the person gave (`nameSource == "user"`) ->
    the conversation's last customTitle -> its last aiTitle -> the Tern title (tern only) -> the derived name."""
    name = s.get("name")
    for c in (name if s.get("nameSource") == "user" else None, titles.get("customTitle"), titles.get("aiTitle"),
              tern_title, name):
        t = _clean(c) if isinstance(c, str) else ""
        if t:
            return t
    return "unnamed session"


# ---------------------------------------------------------------- the snapshot's version and the closed merge

def _with_version(generated_at, nodes, lines, problems_n, hidden_n, unknown_format=False) -> dict:
    """The snapshot with its `version`: the short sha256 of everything but `version` and `generated_at`.
    `unknown_format` appears only when true."""
    body = {"nodes": nodes, "lines": lines, "problems_n": problems_n, "hidden_n": hidden_n}
    if unknown_format:
        body["unknown_format"] = True
    version = hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    return {"version": version, "generated_at": generated_at, **body}


def merge_closed(previous, current: dict, now: float, memory: Optional[dict] = None, hidden=None) -> dict:
    """Rule 1 for the server: `current` plus the session (`p:`) and helper (`a:`) nodes of `previous` that
    vanished, shown as `closed` for GONE_S since they vanished, then gone; `version` recomputed.
    `previous` is the previous result of this function (or None), `now` an epoch in seconds.
    - `memory` ({id: epoch it vanished}, kept between calls; default: this module's) holds the clock; a kept
      node's `since` is the vanishing time, its `pane`, `waiting_for` and `guessed` are dropped. A node that
      comes back is the live one again.
    - `hidden` (default: the ids the last snapshot of this process left out by the age cut) never turn into
      `closed`: they are alive, only hidden.
    - Left to their own rule, never kept: `next` nodes, a helper that was `done`, and a node that was already
      `closed` before vanishing without having been kept here (the snapshot's own rule took it out).
    - Lines: those of `current`, plus those of `previous` whose two ends are both in the result."""
    memory = _VANISHED if memory is None else memory
    hidden = _HIDDEN if hidden is None else set(hidden)
    ids = {n["id"] for n in current["nodes"]}
    kept = []
    for n in (previous or {}).get("nodes") or []:
        i = n.get("id")
        if not isinstance(i, str) or i in ids or i in hidden or not i.startswith(("p:", "a:")):
            continue
        if i not in memory and (n.get("state") == "closed" or (i.startswith("a:") and n.get("state") == "done")):
            continue
        since = memory.setdefault(i, now)
        if now - since >= GONE_S:
            continue
        new = {k: v for k, v in n.items() if k not in ("pane", "waiting_for", "guessed")}
        new.update(state="closed", since=_iso(since))
        kept.append(new)
    alive = {n["id"] for n in kept}
    for i in list(memory):
        if i not in alive:
            del memory[i]  # came back, or its time is over
    if not kept:
        return current
    nodes = list(current["nodes"]) + kept
    every = ids | alive
    lines = list(current["lines"])
    seen = {(ln.get("from"), ln.get("to"), ln.get("kind")) for ln in lines}
    for ln in (previous or {}).get("lines") or []:
        key = (ln.get("from"), ln.get("to"), ln.get("kind"))
        if ((ln.get("from") in alive or ln.get("to") in alive) and ln.get("from") in every
                and ln.get("to") in every and key not in seen):
            seen.add(key)
            lines.append(ln)
    return _with_version(current["generated_at"], nodes, lines, current["problems_n"], current.get("hidden_n", 0),
                         current.get("unknown_format", False))


# ---------------------------------------------------------------- the join

def _safe(label: str, problems: list, fn, default, *args):
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 - one source failing never stops the others
        problems.append(f"{label}: failed ({type(exc).__name__})")
        return default


def _join(now: Optional[float] = None) -> tuple:
    """(snapshot, problem texts). One at a time (the cache is shared); the files it did not touch leave the cache."""
    global _HIDDEN
    with _LOCK:
        _USED.clear()
        hidden: set = set()
        try:
            return _join_unlocked(now, hidden)
        finally:
            _HIDDEN = hidden
            for k in [k for k in _CACHE if k not in _USED]:
                del _CACHE[k]


def _match_steps(adapters, sessions, by_sid, by_handle, problems, lines) -> list:
    """Each adapter's steps onto the live sessions; returns the `next` nodes and appends `opened`/`follows`
    lines. A session is matched to a step's current tab by handle first, then by session id; to older tabs by
    session id only. Only the current tab's session takes the step's verdict. A step counts when one of its tabs
    or its manager is live; a pending step counts when its manager is live or a step it follows counts."""
    next_nodes = []
    for i, ad in enumerate(adapters):
        live_handles = {h for (j, h) in by_handle if j == i}
        steps = _safe(f"adapter {ad.name}", problems, _steps_of, [], ad, set(by_sid), live_handles, problems)
        node_of, live, pending = {}, set(), []
        for st in steps:
            mgr = by_sid.get(st.manager_session_id) if isinstance(st.manager_session_id, str) else None
            current = st.tabs[-1] if st.tabs else None
            for tab in st.tabs:
                s = None
                if tab is current and isinstance(tab.handle, str):
                    s = by_handle.get((i, tab.handle))
                if s is None and isinstance(tab.session_id, str):
                    s = by_sid.get(tab.session_id)
                if s is None:
                    continue
                live.add(st.key)
                s["orch"] = (ad.name, st.label)
                if tab is current:
                    s["step"] = st
                    s["match"] = (i, tab.handle if isinstance(tab.handle, str) else None)
                elif s["match"] is None:
                    s["match"] = (i, None)
                if tab is current or st.key not in node_of:
                    node_of[st.key] = f"p:{s['pid']}"
                if mgr is not None and mgr is not s:
                    lines.append({"from": f"p:{mgr['pid']}", "to": f"p:{s['pid']}", "kind": "opened"})
            if mgr is not None:
                live.add(st.key)
            if st.pending and not st.tabs:
                nid = f"n:{i}:{_hash(st.key, 12)}"
                node_of[st.key] = nid
                pending.append((nid, st, mgr))
        grew = True
        while grew:
            grew = False
            for _, st, mgr in pending:
                if st.key not in live and (mgr is not None or any(d in live for d in st.depends_on)):
                    live.add(st.key)
                    grew = True
        for nid, st, mgr in pending:
            if st.key not in live:
                continue
            next_nodes.append({"id": nid, "kind": "next", "title": _clean(st.label) or "next step",
                               "where": _clean(f"{ad.name} · next"), "state": "next", "since": None})
            frm = [node_of[d] for d in st.depends_on if d in node_of and d in live and node_of[d] != nid]
            if not frm and mgr is not None:
                frm = [f"p:{mgr['pid']}"]
            lines += [{"from": x, "to": nid, "kind": "follows"} for x in frm]
    return next_nodes


def _join_unlocked(now, hidden: set) -> tuple:
    global _IN_USE
    now = time.time() if now is None else now
    problems: list = []
    signals: dict = {}
    config = read_config()
    sessions = _safe("session registry", problems, read_registry, [], problems, signals)
    adapters = _safe("adapters", problems, _adapters, [], problems)
    try:
        _use_adapters(adapters)
    except Exception as exc:  # noqa: BLE001 - load_adapters already checked them; never a crash here
        problems.append(f"adapters: failed ({type(exc).__name__})")
        adapters = []
        _use_adapters(adapters)
    ident = {s["pid"]: s["startedAt"] for s in sessions}
    env = _safe("ps", problems, _env, {}, ident, problems)
    tabs = _safe("Tern tabs", problems, tern.read_tabs, {}, problems) if any(
        v[0] is not None for v in env.values()) else {}

    by_sid, by_handle = {}, {}
    for s in sessions:
        pane, handles = env.get(s["pid"], (None, {}))
        s["pane"] = pane
        for i, h in handles.items():
            by_handle.setdefault((i, h), s)
        if s["sid"]:
            by_sid[s["sid"]] = s
        s.update({"orch": None, "step": None, "match": None})

    lines: list = []
    next_nodes = _match_steps(adapters, sessions, by_sid, by_handle, problems, lines)

    session_nodes, hidden_n = [], 0
    for s in sessions:
        if s["orch"]:
            kind = "orchestrated"
        elif s["kind"] == "bg" or s["tmux"]:
            kind = "background"
        elif s["pane"] is not None:
            kind = "tern"
        else:
            kind = "other"
        tern_info = tabs.get(s["pane"]) or {}
        folder = conversations_folder(s["cwd"]) if s["cwd"] else None
        conversation = folder / f"{s['sid']}.jsonl" if folder is not None and s["sid"] else None
        titles = _safe("a session's conversation", problems, read_titles, {}, conversation) if conversation else {}
        if kind == "orchestrated":
            title, where = s["orch"][1], s["orch"][0]
        else:
            title = _title_of_session(s, titles, tern_info.get("title") if kind == "tern" else None)
            if kind == "tern":
                where = f"Tern · tab {tern_info['number']}" if isinstance(tern_info.get("number"), int) else "Tern"
            else:
                where = WHERE[kind]
        helpers, transcripts = ([], []) if folder is None or not s["sid"] else _safe(
            "helpers", problems, read_helpers, ([], []), folder, s["sid"], s["status"] == "busy", now, problems)
        tail = _safe("a session's conversation", problems, read_tail, {"exists": False}, conversation)
        # the last change counts the helpers' transcripts too: a long helper writes only there. Each is its last
        # timestamped record; a transcript whose mtime is not past the best so far cannot beat it.
        best = tail.get("changed")
        for t in transcripts:
            mt = _mtime(t)
            if mt is None or (_num(best) and mt <= best):
                continue
            c = _safe("helpers", problems, read_tail, {}, t).get("changed")
            if _num(c) and (not _num(best) or c > best):
                best = c
        tail["changed"] = best
        r = _safe("a session's state", problems, session_state, {"state": "stopped"},
                  {"now": now}, s, s["step"], tail, config)
        last = max([x for x in (best, s["statusUpdatedAt"] / 1000 if _num(s["statusUpdatedAt"]) else None)
                    if _num(x)] or [now])
        nid = f"p:{s['pid']}"
        node = {"id": nid, "kind": kind, "title": _clean(title) or "unnamed session", "where": _clean(where),
                "state": r["state"],
                "since": _iso(r["since"]) if "since" in r else _iso_ms(s["statusUpdatedAt"])}
        if kind == "tern":
            node["pane"] = s["pane"]
        if "waiting_for" in r:
            node["waiting_for"] = r["waiting_for"]
        if r.get("guessed"):
            node["guessed"] = True
        helper_ids = {a["aid"]: f"a:{_hash(a['aid'], 8)}" for a in helpers}
        if r["state"] in ("done", "stopped") and now - last > MAX_AGE_S:
            hidden_n += 1  # the card and its helpers stay out
            hidden.add(nid)
            hidden.update(helper_ids.values())
            continue
        session_nodes.append(node)
        for a in sorted(helpers, key=lambda x: x["since"]):
            hid = helper_ids[a["aid"]]
            session_nodes.append({"id": hid, "kind": "helper", "title": _clean(a["title"]) or "helper",
                                  "where": _clean(a["where"]) or "helper", "state": a["state"],
                                  "since": _iso(a["since"])})
            parent = helper_ids.get(a["parent"]) if a["parent"] else None
            lines.append({"from": parent or nid, "to": hid, "kind": "called"})

    _IN_USE = list(adapters)
    _MATCHED.clear()
    _MATCHED.update({s["pid"]: s["match"] for s in sessions if s["match"] is not None})
    problems[:] = [_DISK_RE.sub("…", p) for p in problems]  # problems never show a disk path
    nodes = session_nodes + next_nodes
    ids = {n["id"] for n in nodes}
    kept, seen = [], set()
    for ln in lines:
        key = (ln["from"], ln["to"], ln["kind"])
        if ln["from"] in ids and ln["to"] in ids and ln["from"] != ln["to"] and key not in seen:
            seen.add(key)
            kept.append(ln)
    generated_at = dt.datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds")
    return _with_version(generated_at, nodes, kept, len(problems), hidden_n,
                         bool(signals.get("unknown_format"))), problems


def snapshot(problems: Optional[list] = None) -> dict:
    """The snapshot of now, through this process's cache. When `problems` is a list, the problem texts are
    appended to it (`problems_n` in the snapshot counts them)."""
    snap, found = _join()
    if problems is not None:
        problems.extend(found)
    return snap


# ---------------------------------------------------------------- the prototype

def embed(page_text: str, snap) -> str:
    """`page_text` with `snap` in place of the `null` of its one data script (the fixed escape: `<` as
    \\u003c, U+2028/U+2029 as \\u2028/\\u2029)."""
    if page_text.count(DATA_TAG) != 1:
        raise ValueError("the page does not hold exactly one empty data script")
    j = (json.dumps(snap, ensure_ascii=False).replace("<", "\\u003c")
         .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
    return page_text.replace(DATA_TAG, DATA_TAG.replace(">null<", ">" + j + "<"))


def prototype(out_path, snap: Optional[dict] = None, page_path=None) -> Path:
    """Write the page with a snapshot embedded (the snapshot of now by default) to `out_path`, atomically."""
    out = Path(out_path)
    html = embed(Path(page_path or PAGE).read_text(encoding="utf-8"), snapshot() if snap is None else snap)
    fd, tmp = tempfile.mkstemp(dir=str(out.resolve().parent), prefix=".tmp-", suffix=".html")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(html)
        os.replace(tmp, out)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return out


# ---------------------------------------------------------------- CLI

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="session_map.snapshot", description="The live session map's snapshot.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot", help="print the snapshot as JSON")
    s.add_argument("--problems", action="store_true", help="print only the texts of the sources that failed")
    p = sub.add_parser("prototype", help="write the page with the snapshot of now embedded")
    p.add_argument("--out", default="prototype.html", help="where to write it (default ./prototype.html)")
    try:
        a = ap.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2
    try:
        snap, problems = _join()
        if a.cmd == "snapshot":
            if a.problems:
                print("\n".join(problems) if problems else "no problems")
            else:
                print(json.dumps(snap, ensure_ascii=False, indent=1))
            return 0
        print(f"written: {prototype(a.out, snap)}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"error: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
