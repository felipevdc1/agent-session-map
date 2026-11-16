"""Small, generic helpers for reading Claude Code conversation files.

Claude Code writes each conversation as JSON lines under
``~/.claude/projects/<encoded cwd>/<sessionId>.jsonl``. That format is internal
to Claude Code; these helpers read only the few fields the map relies on and
treat anything unexpected as "nothing there".
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import List, Optional, Set, Union

__all__ = [
    "BACKGROUND_RE",
    "RETURNED_RE",
    "accepted",
    "background_ids",
    "blocks",
    "encode_cwd",
    "has_text",
    "is_compaction_block",
    "is_idle_compaction_mark",
    "is_turn_end",
    "parse_iso",
    "text",
    "tool_result_texts",
]

# A command started in the background (both wordings Claude Code uses) and its return notice.
BACKGROUND_RE = re.compile(r"(?:running in background with ID: |moved to the background \(ID: )([A-Za-z0-9_-]+)")
RETURNED_RE = re.compile(r"<task-notification>\s*<task-id>([A-Za-z0-9_-]+)</task-id>")


def encode_cwd(path: Union[str, Path]) -> str:
    """The folder name Claude Code uses for a cwd under ``projects/``: every non-alphanumeric becomes ``-``."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def blocks(rec: dict) -> list:
    """The content blocks of a record's message; a plain-string content becomes one text block."""
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return []
    c = msg.get("content")
    if isinstance(c, str):
        return [{"type": "text", "text": c}]
    return c if isinstance(c, list) else []


def text(rec: dict) -> str:
    """The record's text blocks joined by newlines (empty when it has none)."""
    return "\n".join(b.get("text", "") for b in blocks(rec) if isinstance(b, dict) and b.get("type") == "text")


def is_idle_compaction_mark(rec: dict) -> bool:
    """The ``system/informational`` record that closes an idle compaction: Claude Code compacts a session that
    sat idle after its turn ended, appending compact_boundary, the summary and this mark behind the finished turn."""
    return (rec.get("type") == "system" and rec.get("subtype") == "informational"
            and str(rec.get("content", "")).startswith("Compacted while idle"))


def is_compaction_block(rec: dict) -> bool:
    """A record of that compaction block, which is not part of the session's turn."""
    return (rec.get("type") == "user" and bool(rec.get("isCompactSummary"))) or (
        rec.get("type") == "system" and rec.get("subtype") == "compact_boundary")


def tool_result_texts(block: dict) -> List[str]:
    """The text of a tool_result block, whose content is a string or a list of text blocks."""
    c = block.get("content")
    if isinstance(c, str):
        return [c]
    if isinstance(c, list):
        return [str(b.get("text", "")) for b in c if isinstance(b, dict) and b.get("type") == "text"]
    return []


def parse_iso(s) -> Optional[dt.datetime]:
    """An ISO timestamp (``Z`` or an offset) as an aware datetime; naive ones are taken as UTC.
    None when missing or malformed."""
    if not isinstance(s, str):
        return None
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def background_ids(path: Union[str, Path]) -> Set[str]:
    """Ids of background commands the conversation started (main chain only) that have no return notice yet.

    The notice arrives as a ``user`` record's text (the session was idle) or as the ``prompt`` of a
    ``queued_command`` attachment (it arrived mid-turn). An unreadable file reads as nothing in the background.
    """
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return set()
    started: Set[str] = set()
    returned: Set[str] = set()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("isSidechain"):
            continue
        if rec.get("type") == "user":
            for b in blocks(rec):
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    for txt in tool_result_texts(b):
                        started.update(BACKGROUND_RE.findall(txt))
                elif b.get("type") == "text":
                    returned.update(RETURNED_RE.findall(str(b.get("text", ""))))
        elif rec.get("type") == "attachment":
            att = rec.get("attachment")
            if isinstance(att, dict) and att.get("type") == "queued_command":
                returned.update(RETURNED_RE.findall(str(att.get("prompt", ""))))
    return started - returned


def accepted(rec: dict) -> bool:
    """The records the map reads: user, assistant, or system/turn_duration, outside sidechains."""
    if rec.get("isSidechain"):
        return False
    t = rec.get("type")
    return t in ("user", "assistant") or (t == "system" and rec.get("subtype") == "turn_duration")


def has_text(rec: dict) -> bool:
    """An assistant record on the main chain that carries at least one text block."""
    return (rec.get("type") == "assistant" and not rec.get("isSidechain")
            and any(isinstance(b, dict) and b.get("type") == "text" for b in blocks(rec)))


def is_turn_end(rec) -> bool:
    """A turn end: system/turn_duration, whether or not helper agents are still pending
    (a pending helper is background work, counted separately)."""
    return isinstance(rec, dict) and rec.get("type") == "system" and rec.get("subtype") == "turn_duration"
