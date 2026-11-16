"""The session-source extension point.

A source is one kind of agent whose sessions the map draws (Claude Code, Codex): an object with a ``name`` and
a ``read(ctx)`` that returns a ``SourceResult``. Sources are code in this package, never loaded from a path like
the orchestrator adapters: a source reads private local files, so it ships reviewed with the package.

``SESSION_MAP_SOURCES`` picks them: a comma list of names (default every built-in, ``claude,codex``). Rules:
- the order on the map is always the built-in order, Claude Code first, whatever order the list gives;
- an unknown name is one problem line; ``claude`` is always read even when left out (the map is built around
  it), with a problem line saying so;
- the core reads Claude Code first and unwrapped; every other source runs inside ``_safe``, so one that raises
  adds ``source <name>: failed (<Exc>)`` and the rest still comes out.
The contract every source keeps: no path, id or message text in a node (ids are hashes, titles are cleaned),
the same five states, and reads that are incremental (a second snapshot over unchanged files reads nothing).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Protocol

ENV_SOURCES = "SESSION_MAP_SOURCES"
BUILTIN = ("claude", "codex")  # every built-in source name, in map order


def _no_tabs() -> dict:
    return {}


@dataclass(frozen=True)
class Context:
    """What a source gets for one join: `now` (epoch), `config` (snapshot.Config, the turn markers), `problems`
    (the join's problem list: append short texts that never hold a path) and `tabs` (a call that returns the
    Tern tabs, `tern ls` at most once per join; call it only when one of the source's processes has a pane)."""
    now: float
    config: object
    problems: list = field(default_factory=list)
    tabs: Callable[[], dict] = _no_tabs


@dataclass
class SourceResult:
    """One source's part of the snapshot: its nodes and lines (appended after the sources before it), how many
    cards it left out by the age cut (`hidden_n`) and their ids (`hidden`, which merge_closed never turns into
    closed), and whether its files looked unlike the known format."""
    nodes: list
    lines: list
    hidden_n: int = 0
    hidden: set = field(default_factory=set)
    unknown_format: bool = False


class Source(Protocol):
    name: str  # shown in a problem line, e.g. "Codex"

    def read(self, ctx: Context) -> SourceResult: ...


def _names(problems: list) -> set:
    """The names SESSION_MAP_SOURCES asks for (every built-in when unset or empty), with its problem lines."""
    raw = os.environ.get(ENV_SOURCES, "")
    asked = {x.strip().lower() for x in raw.split(",") if x.strip()}
    if not asked:
        return set(BUILTIN)
    unknown = sorted(asked - set(BUILTIN))
    if unknown:
        problems.append(f"sources: unknown name(s) in {ENV_SOURCES}: {', '.join(unknown)}")
    if "claude" not in asked:
        problems.append(f"sources: claude is always read ({ENV_SOURCES} left it out)")
    return (asked & set(BUILTIN)) | {"claude"}


def selected(problems: list) -> list:
    """The sources to read, in map order: Claude Code first, then the others SESSION_MAP_SOURCES keeps."""
    from .. import snapshot  # here, not at the top: snapshot imports this package inside its functions
    names = _names(problems)
    out: list = [snapshot.ClaudeCode()]
    if "codex" in names:
        from . import codex
        out.append(codex.Codex())
    return out
