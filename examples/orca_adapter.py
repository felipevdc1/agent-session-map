"""Example adapter: Orca tabs on the session map.

Layer 1 (Orca alone): every connected Orca terminal becomes a step titled
with its Orca title, so the card shows that title and its focus button
brings the tab to the front.

Layer 2 (optional): when SESSION_MAP_STEPS_FILE names a JSON file written by
an orchestrator on top of Orca (shape: examples/steps.example.json), its steps
replace layer 1's for the same handles and add pending steps and verdicts.

Problems found on the last steps() call are listed in ``adapter.problems``
instead of raised, so a broken step file never hides the layer 1 tabs.

Run with: SESSION_MAP_ADAPTERS=examples/orca_adapter.py:adapter session-map open
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

from session_map.orchestrator import HANDLE_RE, KEY_RE, Step, Tab

ORCA_APP_BIN = "/Applications/Orca.app/Contents/Resources/bin/orca"
STATE_TO_STEP = {  # state in the step file -> (pending, verdict)
    "pending": (True, None),
    "running": (False, None),
    "done": (False, "done"),
    "waiting": (False, "waiting"),
    "closed": (False, "closed"),
}


def _orca():
    return shutil.which("orca") or ORCA_APP_BIN


def _key(handle):
    return "tab-" + hashlib.sha256(handle.encode()).hexdigest()[:12]


class OrcaAdapter:
    name = "Orca"
    env_var = "ORCA_TERMINAL_HANDLE"
    env_pattern = r"term_[0-9a-f-]+"

    def __init__(self):
        self.problems = []

    def _is_handle(self, value):
        return isinstance(value, str) and bool(HANDLE_RE.match(value)) and bool(re.fullmatch(self.env_pattern, value))

    def _live_terminals(self):
        out = subprocess.run([_orca(), "terminal", "list", "--json"], capture_output=True, text=True, timeout=3)
        terminals = json.loads(out.stdout)["result"]["terminals"]
        return [t for t in terminals if t.get("connected") is True and self._is_handle(t.get("handle"))]

    def _file_steps(self, path):
        with open(path, encoding="utf-8") as fh:
            entries = json.load(fh)["steps"]
        steps = []
        for e in entries:
            pending, verdict = STATE_TO_STEP[e["state"]]
            if not (isinstance(e["key"], str) and KEY_RE.match(e["key"]) and isinstance(e["label"], str)):
                raise ValueError("a step has a bad key or label")
            tabs = tuple(Tab(t.get("session_id"), t.get("handle") if self._is_handle(t.get("handle")) else None)
                         for t in e.get("tabs") or [])
            steps.append(Step(key=e["key"], label=e["label"], tabs=tabs, verdict=verdict, pending=pending,
                              depends_on=tuple(e.get("depends_on") or ()),
                              manager_session_id=e.get("manager_session_id")))
        return steps

    def steps(self, live_session_ids, live_handles):
        self.problems = []
        terminals = []
        try:
            terminals = self._live_terminals()
        except Exception as exc:  # orca missing, slow or answering in another shape
            self.problems.append(f"orca terminal list failed: {type(exc).__name__}")
        file_steps = []
        path = os.environ.get("SESSION_MAP_STEPS_FILE")
        if path:
            try:
                file_steps = self._file_steps(path)
            except Exception as exc:  # one problem for the whole file; layer 1 stays
                self.problems.append(f"step file ignored: {type(exc).__name__}")
        claimed = {t.handle for s in file_steps for t in s.tabs if t.handle}
        layer1 = [Step(key=_key(t["handle"]), label=str(t.get("title") or ""), tabs=(Tab(None, t["handle"]),))
                  for t in terminals if t["handle"] not in claimed]
        return layer1 + file_steps

    def focus(self, handle):
        if not self._is_handle(handle):
            return False
        try:
            done = subprocess.run([_orca(), "terminal", "switch", "--terminal", handle, "--json"],
                                  capture_output=True, timeout=3)
            if done.returncode != 0:
                return False
            if sys.platform == "darwin":
                subprocess.run(["open", "-a", "Orca"], capture_output=True, timeout=3)
            return True
        except (OSError, subprocess.SubprocessError):
            return False


adapter = OrcaAdapter()
