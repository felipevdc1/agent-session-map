"""The orchestrator extension point.

An orchestrator (a tool that opens agent sessions in its own terminal tabs)
plugs into the map through an adapter: an object with a ``name``, an optional
environment variable that carries its terminal handle, and two methods,
``steps`` and ``focus``. Adapters are listed in ``SESSION_MAP_ADAPTERS`` as
comma-separated ``module:attr`` entries, where ``module`` is a dotted
importable name or a path to a ``.py`` file.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import re
import sys
from dataclasses import dataclass
from typing import List, Optional, Pattern, Protocol, Set, Tuple

VERDICTS = ("done", "waiting", "closed")

KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
HANDLE_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
ENV_VAR_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

ENV_ADAPTERS = "SESSION_MAP_ADAPTERS"


@dataclass(frozen=True)
class Tab:
    session_id: Optional[str]  # Claude sessionId of the session running in this tab
    handle: Optional[str]  # the orchestrator's own terminal id (matched against env_var in ps)


@dataclass(frozen=True)
class Step:
    key: str  # stable, unique per adapter, [A-Za-z0-9_.-]{1,80}; becomes part of a node id
    label: str  # card title, e.g. "site · plan"
    tabs: Tuple[Tab, ...]  # oldest first; the last one is the current tab
    verdict: Optional[str] = None  # "done" | "waiting" | "closed" | None (None = let the core decide)
    pending: bool = False  # True = the step has not opened yet, shown as a dashed "next" card
    depends_on: Tuple[str, ...] = ()  # keys of steps this one follows
    manager_session_id: Optional[str] = None  # the session that opened it, shown as an "opened" line


class Adapter(Protocol):
    name: str  # shown in the card's "where", e.g. "Orca"
    env_var: Optional[str]  # e.g. "ORCA_TERMINAL_HANDLE"
    env_pattern: Optional[str]  # anchored value regex, e.g. r"term_[0-9a-f-]+"

    def steps(self, live_session_ids: Set[str], live_handles: Set[str]) -> List[Step]: ...

    def focus(self, handle: str) -> bool: ...  # bring that tab to the front; True on success


_ABS_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/]|/)[^\s'\"]*")


def _scrub(text: str) -> str:
    """Replace absolute paths in a message so no disk path leaks into a problem line."""
    return _ABS_PATH_RE.sub("<path>", text)


def env_regex(adapter) -> Optional[Pattern[str]]:
    """The regex that finds this adapter's handle in a process environment line.

    Returns None for an adapter without ``env_var``. Raises ValueError when
    ``env_var`` is not an environment variable name, and re.error when
    ``env_pattern`` does not compile.
    """
    env_var = getattr(adapter, "env_var", None)
    if env_var is None:
        return None
    if not isinstance(env_var, str) or not ENV_VAR_RE.match(env_var):
        raise ValueError("env_var is not a valid environment variable name")
    pattern = getattr(adapter, "env_pattern", None)
    if not isinstance(pattern, str):
        raise ValueError("env_pattern must be a string when env_var is set")
    return re.compile(r"(?:^|\s)" + re.escape(env_var) + "=(" + pattern + r")(?:\s|$)")


def _check_adapter(obj) -> Optional[str]:
    """None when ``obj`` has the adapter shape, else the reason it does not."""
    name = getattr(obj, "name", None)
    if not isinstance(name, str) or not name:
        return "has no 'name' string"
    env_var = getattr(obj, "env_var", None)
    env_pattern = getattr(obj, "env_pattern", None)
    for field, value in (("env_var", env_var), ("env_pattern", env_pattern)):
        if value is not None and not isinstance(value, str):
            return f"'{field}' must be a string or None"
    if (env_var is None) != (env_pattern is None):
        return "'env_var' and 'env_pattern' must be set together or not at all"
    for method in ("steps", "focus"):
        if not callable(getattr(obj, method, None)):
            return f"has no callable '{method}'"
    try:
        env_regex(obj)
    except (ValueError, re.error) as exc:
        return _scrub(str(exc))
    return None


def _is_file_ref(module_ref: str) -> bool:
    return module_ref.endswith(".py") or "/" in module_ref or "\\" in module_ref


def load_adapters(problems: list) -> list:
    """The adapters named in SESSION_MAP_ADAPTERS, in order.

    A failing entry adds one problem line to ``problems`` and is skipped;
    this never raises. This is the only place in the package that imports
    code dynamically.
    """
    raw = os.environ.get(ENV_ADAPTERS, "")
    adapters = []
    for index, entry in enumerate(e.strip() for e in raw.split(",")):
        if not entry:
            continue
        module_ref, sep, attr = entry.rpartition(":")
        if not sep or not module_ref or not attr:
            problems.append(f"adapter entry '{_scrub(entry)}' is not of the form module:attr")
            continue
        is_file = _is_file_ref(module_ref)
        shown_ref = os.path.basename(module_ref.replace("\\", "/")) if is_file else module_ref
        shown = f"{shown_ref}:{attr}"
        mod_name = ""
        try:
            if is_file:
                if not os.path.isfile(module_ref):
                    raise ImportError("adapter file not found")
                stem = re.sub(r"\W", "_", os.path.splitext(shown_ref)[0])
                mod_name = f"_session_map_adapter_{index}_{stem}"
                spec = importlib.util.spec_from_file_location(mod_name, module_ref)
                if spec is None or spec.loader is None:
                    raise ImportError("adapter file cannot be loaded")
                module = importlib.util.module_from_spec(spec)
                sys.modules[mod_name] = module  # dataclasses in the adapter look their module up here
                spec.loader.exec_module(module)
            else:
                module = importlib.import_module(module_ref)
        except Exception as exc:
            sys.modules.pop(mod_name, None)
            problems.append(f"adapter '{shown}' could not be imported: {type(exc).__name__}: {_scrub(str(exc))}")
            continue
        if not hasattr(module, attr):
            problems.append(f"adapter '{shown}' not found in its module")
            continue
        obj = getattr(module, attr)
        reason = _check_adapter(obj)
        if reason:
            problems.append(f"adapter '{shown}' {reason}")
            continue
        adapters.append(obj)
    return adapters


def validate_step(step, problems: list) -> bool:
    """True when ``step`` is a well-formed Step; else False with one problem line."""
    if not isinstance(step, Step):
        problems.append(f"dropped a step that is not a Step ({type(step).__name__})")
        return False
    key = step.key
    shown = repr(key)[:90]
    if not isinstance(key, str) or not KEY_RE.match(key):
        problems.append(f"dropped step {shown}: key must match [A-Za-z0-9_.-]{{1,80}}")
        return False
    if step.verdict is not None and step.verdict not in VERDICTS:
        problems.append(f"dropped step {shown}: verdict must be one of {', '.join(VERDICTS)} or None")
        return False
    if not isinstance(step.label, str):
        problems.append(f"dropped step {shown}: label must be a string")
        return False
    if not isinstance(step.tabs, tuple) or not all(isinstance(t, Tab) for t in step.tabs):
        problems.append(f"dropped step {shown}: tabs must be a tuple of Tab")
        return False
    if not isinstance(step.depends_on, tuple) or not all(isinstance(d, str) for d in step.depends_on):
        problems.append(f"dropped step {shown}: depends_on must be a tuple of strings")
        return False
    return True
