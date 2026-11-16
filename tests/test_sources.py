"""The session-source seam (session_map/sources/): Claude Code first, selection by SESSION_MAP_SOURCES, a failing
source isolated, a Claude-only map unchanged by the seam, and turn_end_state kept equal to session_state's rule 5.

Fixtures come from tests/test_snapshot.py (World, records, live `sleep` pids). Its World writes under that
module's TMP, which its tearDownModule deletes when its own tests finish (before this module runs under
discover), so this module points World at a temp folder of its own and removes it, with the pids it started,
at the end. Every test clears CODEX_HOME and SESSION_MAP_SOURCES, so no real ~/.codex is ever read.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tests import test_snapshot as ts
from session_map import snapshot, sources
from session_map.snapshot import Config

NOW = 1_800_000_000.0  # a fixed clock for the pure state calls (TurnEndContract)
_TMP = None
_PROCS_FROM = 0
_PATCHES: list = []


def setUpModule():
    global _TMP, _PROCS_FROM
    _TMP = Path(tempfile.mkdtemp(prefix="session-map-sources-test-"))
    (_TMP / "home").mkdir()
    _PROCS_FROM = len(ts._PROCS)
    for p in (mock.patch.object(ts, "TMP", _TMP), mock.patch.dict(os.environ, {"HOME": str(_TMP / "home")})):
        p.start()
        _PATCHES.append(p)


def tearDownModule():
    for p in reversed(_PATCHES):
        p.stop()
    for p in ts._PROCS[_PROCS_FROM:]:
        try:
            p.kill()
            p.wait(timeout=5)
        except Exception:  # noqa: BLE001 - already gone
            pass
    shutil.rmtree(_TMP, ignore_errors=True)


class _Base(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for k in ("CODEX_HOME", sources.ENV_SOURCES, "SESSION_MAP_CODEX_DIR", "SESSION_MAP_ADAPTERS"):
            os.environ.pop(k, None)
        snapshot.forget()
        self.addCleanup(snapshot.forget)
        self.now = ts.time.time()  # one clock per test, so two joins can be compared byte for byte

    def world(self):
        m = ts.World()
        _, sid = m.session(pane=4, number=1, name="first", status="busy")
        m.conversation(sid, [ts.r_user("go"), ts.r_asst("Working on it.")])
        _, sid2 = m.session(name="second")
        m.conversation(sid2, [ts.r_user("x"), ts.r_asst("Done.\nShall I go on?"), ts.r_end()])
        return m

    def join(self, m, **env):
        with m.active(), mock.patch.dict(os.environ, env):
            return snapshot._join(self.now)


class _FakeCodex:
    """Stands in for sources/codex.py's Codex: counts its reads and returns one Codex-shaped node."""
    name = "Codex"
    reads = 0

    def read(self, ctx):
        type(self).reads += 1
        return sources.SourceResult(nodes=[{"id": "x:0123456789ab", "agent": "codex", "kind": "app",
                                            "title": "a thread", "where": "Codex app", "state": "done",
                                            "since": None}], lines=[])


def _fake_codex_module():
    """A stand-in module for session_map.sources.codex, seen by `from . import codex` whether or not the real
    module exists (both the package attribute and sys.modules are patched)."""
    mod = types.ModuleType("session_map.sources.codex")
    _FakeCodex.reads = 0
    mod.Codex = _FakeCodex
    return [mock.patch.dict(sys.modules, {mod.__name__: mod}),
            mock.patch.object(sources, "codex", mod, create=True)]


class Selection(_Base):

    def with_fake_codex(self):
        for p in _fake_codex_module():
            p.start()
            self.addCleanup(p.stop)

    def test_claude_is_always_first(self):
        self.with_fake_codex()
        for value in (None, "claude,codex", "codex,claude", "codex"):
            with self.subTest(sources=value), mock.patch.dict(os.environ):
                if value is not None:
                    os.environ[sources.ENV_SOURCES] = value
                got = sources.selected([])
                self.assertIsInstance(got[0], snapshot.ClaudeCode)
                self.assertEqual([type(s).__name__ for s in got], ["ClaudeCode", "_FakeCodex"])

    def test_claude_nodes_come_before_other_sources(self):
        self.with_fake_codex()
        m = self.world()
        r, problems = self.join(m, **{sources.ENV_SOURCES: "codex,claude"})
        self.assertEqual(problems, [])
        ids = [n["id"] for n in r["nodes"]]
        self.assertEqual(ids[-1], "x:0123456789ab")
        self.assertTrue(all(i.startswith("p:") for i in ids[:-1]) and len(ids) == 3, ids)

    def test_only_claude_means_no_codex_read(self):
        """The spy both ways: the default list reads Codex once, SESSION_MAP_SOURCES=claude reads it never."""
        self.with_fake_codex()
        m = self.world()
        self.join(m)
        self.assertEqual(_FakeCodex.reads, 1)
        r, problems = self.join(m, **{sources.ENV_SOURCES: "claude"})
        self.assertEqual(_FakeCodex.reads, 1)
        self.assertEqual(problems, [])
        self.assertFalse(any(n["id"].startswith("x:") for n in r["nodes"]))

    def test_unknown_name_is_one_problem_line(self):
        m = self.world()
        r, problems = self.join(m, **{sources.ENV_SOURCES: "claude,gemini"})
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("gemini", problems[0])
        self.assertEqual(r["problems_n"], 1)
        self.assertEqual(len([n for n in r["nodes"] if n["id"].startswith("p:")]), 2)

    def test_claude_left_out_is_still_read_with_a_line(self):
        m = self.world()
        r, problems = self.join(m, **{sources.ENV_SOURCES: "codex"})
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("claude", problems[0])
        self.assertEqual(len([n for n in r["nodes"] if n["id"].startswith("p:")]), 2)

    def test_a_failing_source_is_isolated(self):
        class Boom:
            name = "Boom"

            def read(self, ctx):
                raise ValueError("broke at " + "/some/disk/path")

        m = self.world()
        with mock.patch.object(sources, "selected", lambda problems: [snapshot.ClaudeCode(), Boom()]):
            r, problems = self.join(m)
        self.assertEqual(problems, ["source Boom: failed (ValueError)"])
        self.assertEqual(r["problems_n"], 1)
        self.assertEqual(len([n for n in r["nodes"] if n["id"].startswith("p:")]), 2)

    def test_claude_failing_is_not_swallowed(self):
        """Decision 2: an exception in the Claude part escapes the join, as before the seam."""
        m = self.world()
        with mock.patch.object(snapshot, "_claude_part", side_effect=RuntimeError("x")):
            with self.assertRaises(RuntimeError):
                self.join(m)


class ClaudeOnlyUnchanged(_Base):

    def test_default_sources_equal_claude_only(self):
        """A Claude fixture map through the default source list (Codex selected, sandboxed off by the fixture
        environment, or absent) is byte-identical to the map with SESSION_MAP_SOURCES=claude."""
        m = self.world()
        m.helper(json.loads(next(m.reg.glob("*.json")).read_text(encoding="utf-8"))["sessionId"],
                 "a1111111111111111", {"agentType": "Explore", "description": "Reads the code"})
        full, p1 = self.join(m)
        only, p2 = self.join(m, **{sources.ENV_SOURCES: "claude"})
        self.assertEqual((p1, p2), ([], []))
        self.assertEqual(json.dumps(full, sort_keys=True), json.dumps(only, sort_keys=True))
        self.assertGreaterEqual(len(full["nodes"]), 2)
        self.assertEqual(ts.violations(full), [])


class TurnEndContract(unittest.TestCase):
    """Contract drift: turn_end_state(text) equals session_state's rule 5 for a turn-end tail holding the same
    text, and both equal the expected column (so a rule changed in both places still turns this red)."""

    MARKERS = Config(done_lines=("All done.",), wait_marks=("[needs you]",))
    D, W, G, S = {"state": "done"}, {"state": "waiting"}, {"state": "waiting", "guessed": True}, {"state": "stopped"}
    # (text, expected with the default config, expected with MARKERS)
    TABLE = [
        (None, S, S),
        ("Done, all green.", D, D),
        ("", D, D),
        ("Shall I go on?\nAll done.", G, D),
        ("**All done.**", D, D),
        ("Plan ready.\n[needs you] Option 1 or 2.", D, W),
        ("First step done.\nShall I go on?", G, G),
        ("Changed it.\n(Want another pass? Just say so.)\nSecond.\nThird.", G, G),
        ("A question? long ago\nOne.\nTwo.\nThree.", D, D),
        ("Is it “ready?”", G, G),
        ("Ready. Want me to start?\n", G, G),
    ]

    def rule5(self, text, config):
        tail = {"exists": True, "last": ts.r_end(), "text": text, "changed": NOW, "background": set()}
        return snapshot.session_state({"now": NOW}, {"alive": True, "status": "idle"}, None, tail, config)

    def test_same_result_for_every_text(self):
        for text, plain, marked in self.TABLE:
            for config, expected in ((None, plain), (Config(), plain), (self.MARKERS, marked)):
                with self.subTest(text=text, config=config):
                    got = snapshot.turn_end_state(text, config)
                    self.assertEqual(got, self.rule5(text, config))
                    self.assertEqual(got, expected)

    def test_the_table_holds_every_outcome(self):
        seen = {json.dumps(e, sort_keys=True) for _, a, b in self.TABLE for e in (a, b)}
        self.assertEqual(seen, {json.dumps(x, sort_keys=True) for x in (self.D, self.W, self.G, self.S)})


if __name__ == "__main__":
    unittest.main()
