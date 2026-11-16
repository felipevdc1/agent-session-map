"""Tern: which panes are live, their tab titles and numbers, and bringing a pane to the front.

Everything comes from the `tern` CLI, called with list arguments only:
- `tern ls --json` (read once per snapshot) for the live terminal blocks and their tab numbers;
- `tern focus <pane>` to bring a pane's tab to the front;
- `tern browser '{"op":"open","owner":<pane>,"url":...,"how":"beside"}'` to open a page beside a pane.

`SESSION_MAP_TERN_JSON` (a path to a JSON file) replaces the `tern ls --json` call, for tests.
A missing or failing `tern` never raises: `read_tabs` adds one problem sentence and returns nothing.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

TIMEOUT_S = 3
ENV_JSON = "SESSION_MAP_TERN_JSON"
_PANE_RE = re.compile(r"^\d{1,20}$")  # Tern block ids can be 13 digits or more


def exe() -> str | None:
    """The `tern` program: ~/.local/bin/tern when it is a file, else the one on PATH, else None."""
    local = Path.home() / ".local" / "bin" / "tern"
    return str(local) if local.is_file() else shutil.which("tern")


def _run(args: list) -> bool:
    """One command with list args; True when it exits 0. Its output is never shown nor kept."""
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=TIMEOUT_S, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def _raw() -> dict:
    """The parsed `tern ls --json` (or the SESSION_MAP_TERN_JSON file); raises RuntimeError with a plain sentence."""
    path = os.environ.get(ENV_JSON)
    if path:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError:
            raise RuntimeError("the Tern listing file could not be read") from None
    else:
        program = exe()
        if not program:
            raise RuntimeError("tern is not installed here")
        try:
            proc = subprocess.run([program, "ls", "--json"], capture_output=True, text=True,
                                  timeout=TIMEOUT_S, check=False)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"tern ls did not answer within {TIMEOUT_S} seconds") from None
        except OSError:
            raise RuntimeError("tern could not be started") from None
        if proc.returncode != 0:
            raise RuntimeError(f"tern ls failed (exit {proc.returncode})")
        text = proc.stdout
    try:
        data = json.loads(text)
    except ValueError:
        raise RuntimeError("tern ls returned something that is not JSON") from None
    if not isinstance(data, dict):
        raise RuntimeError("tern ls returned an unexpected shape")
    return data


def read_tabs(problems: list) -> dict:
    """{pane: {"title", "number"}} of the live Tern terminal blocks.

    A block counts when it is live, not detached and not listed in the top-level `detached` array.
    `number` is its tab's number (None when Tern gives none); `title` is Tern's raw title (cleaning is the caller's).
    """
    try:
        data = _raw()
    except RuntimeError as exc:
        problems.append(f"Tern tabs: {exc}.")
        return {}
    detached = {b.get("id") if isinstance(b, dict) else b for b in data.get("detached") or []}
    out: dict = {}
    for session in data.get("sessions") or []:
        if not isinstance(session, dict):
            continue
        for tab in session.get("tabs") or []:
            if not isinstance(tab, dict):
                continue
            number = tab.get("number")
            for block in tab.get("blocks") or []:
                if not isinstance(block, dict):
                    continue
                pane = block.get("id")
                if (pane is None or not block.get("live") or block.get("detached")
                        or pane in detached):
                    continue
                out[pane] = {"title": block.get("title") or "",
                             "number": number if isinstance(number, int) else None}
    return out


def focus(pane) -> bool:
    """Bring a pane's tab to the front (`tern focus <pane>`, then the Tern app on macOS); True when Tern found it."""
    program = exe()
    if not program or not _PANE_RE.fullmatch(str(pane)):
        return False
    if not _run([program, "focus", str(pane)]):
        return False
    if sys.platform == "darwin":
        _run(["open", "-a", "Tern"])
    return True


def open_beside(pane, url: str) -> bool:
    """Open `url` in a Tern browser block beside `pane`; True when Tern accepted it."""
    program = exe()
    if not program or not _PANE_RE.fullmatch(str(pane)):
        return False
    request = json.dumps({"op": "open", "owner": int(pane), "url": url, "how": "beside"})
    return _run([program, "browser", request])
