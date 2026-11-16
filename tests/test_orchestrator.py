import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from session_map import orchestrator  # noqa: E402
from session_map.orchestrator import (  # noqa: E402
    HANDLE_RE,
    VERDICTS,
    Step,
    Tab,
    env_regex,
    load_adapters,
    validate_step,
)

GOOD_ADAPTER = """
class _Adapter:
    name = "Demo"
    env_var = "ORCA_TERMINAL_HANDLE"
    env_pattern = r"term_[0-9a-f-]+"

    def steps(self, live_session_ids, live_handles):
        return []

    def focus(self, handle):
        return True

adapter = _Adapter()
"""

NO_ENV_ADAPTER = """
class _Adapter:
    name = "Plain"
    env_var = None
    env_pattern = None

    def steps(self, live_session_ids, live_handles):
        return []

    def focus(self, handle):
        return False

adapter = _Adapter()
"""


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        sys.path.insert(0, self.tmp)
        self.addCleanup(sys.path.remove, self.tmp)
        self._mods = []
        self.addCleanup(self._drop_modules)

    def _drop_modules(self):
        for name in list(sys.modules):
            if name in self._mods or name.startswith("_session_map_adapter_"):
                sys.modules.pop(name, None)

    def write(self, filename, text):
        path = os.path.join(self.tmp, filename)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        if filename.endswith(".py"):
            self._mods.append(filename[:-3])
        return path

    def load(self, value):
        problems = []
        with mock.patch.dict(os.environ, {"SESSION_MAP_ADAPTERS": value}):
            adapters = load_adapters(problems)
        return adapters, problems


class LoadAdapters(_Base):
    def test_unset_env_gives_nothing(self):
        problems = []
        env = {k: v for k, v in os.environ.items() if k != "SESSION_MAP_ADAPTERS"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(load_adapters(problems), [])
        self.assertEqual(problems, [])

    def test_empty_env_gives_nothing(self):
        self.assertEqual(self.load(""), ([], []))
        self.assertEqual(self.load(" , ,"), ([], []))

    def test_by_module_name(self):
        self.write("fixture_good_mod.py", GOOD_ADAPTER)
        adapters, problems = self.load("fixture_good_mod:adapter")
        self.assertEqual(problems, [])
        self.assertEqual([a.name for a in adapters], ["Demo"])

    def test_by_file_path(self):
        path = self.write("fixture_file_adapter.py", NO_ENV_ADAPTER)
        adapters, problems = self.load(path + ":adapter")
        self.assertEqual(problems, [])
        self.assertEqual([a.name for a in adapters], ["Plain"])

    def test_broken_entries_are_one_problem_each_and_others_still_load(self):
        self.write("fixture_good2.py", GOOD_ADAPTER)
        self.write("fixture_raises.py", "raise RuntimeError('boom at " + self.tmp + "')\n")
        self.write("fixture_noattr.py", "x = 1\n")
        self.write("fixture_shape.py", "class A:\n    name = 'S'\n    env_var = None\n    env_pattern = None\nadapter = A()\n")
        self.write("fixture_half_env.py", GOOD_ADAPTER.replace('env_pattern = r"term_[0-9a-f-]+"', "env_pattern = None"))
        self.write("fixture_bad_var.py", GOOD_ADAPTER.replace('"ORCA_TERMINAL_HANDLE"', '"bad-name"'))
        self.write("fixture_bad_pat.py", GOOD_ADAPTER.replace('r"term_[0-9a-f-]+"', 'r"term_[("'))
        self.write("fixture_noname.py", NO_ENV_ADAPTER.replace('name = "Plain"', "name = 3"))
        missing_file = os.path.join(self.tmp, "nowhere.py")
        entries = [
            "fixture_good2:adapter",
            "fixture_does_not_exist:adapter",
            "fixture_raises:adapter",
            "fixture_noattr:adapter",
            "fixture_shape:adapter",
            "fixture_half_env:adapter",
            "fixture_bad_var:adapter",
            "fixture_bad_pat:adapter",
            "fixture_noname:adapter",
            missing_file + ":adapter",
            "no_colon_here",
            os.path.join(self.tmp, "fixture_file_adapter.py") + ":adapter",
        ]
        self.write("fixture_file_adapter.py", NO_ENV_ADAPTER)
        adapters, problems = self.load(",".join(entries))
        self.assertEqual([a.name for a in adapters], ["Demo", "Plain"])
        self.assertEqual(len(problems), len(entries) - 2, problems)
        for line in problems:
            self.assertNotIn(self.tmp, line)
            self.assertNotIn(os.path.realpath(self.tmp), line)

    def test_problem_names_the_entry(self):
        _, problems = self.load("fixture_absent_mod:adapter")
        self.assertEqual(len(problems), 1)
        self.assertIn("fixture_absent_mod:adapter", problems[0])


class ValidateStep(unittest.TestCase):
    def check(self, step):
        problems = []
        ok = validate_step(step, problems)
        self.assertEqual(len(problems), 0 if ok else 1)
        return ok

    def test_good_steps_pass(self):
        self.assertTrue(self.check(Step(key="site.plan-1_a", label="site · plan", tabs=(Tab("s1", "h1"),))))
        for verdict in VERDICTS + (None,):
            self.assertTrue(self.check(Step(key="k", label="l", tabs=(), verdict=verdict, pending=True, depends_on=("a",))))

    def test_bad_key_dropped(self):
        for key in ("", "a b", "x/y", "k" * 81, 7, None):
            self.assertFalse(self.check(Step(key=key, label="l", tabs=())), key)
        self.assertTrue(self.check(Step(key="k" * 80, label="l", tabs=())))

    def test_bad_verdict_dropped(self):
        for verdict in ("Done", "running", "pending", ""):
            self.assertFalse(self.check(Step(key="k", label="l", tabs=(), verdict=verdict)), verdict)

    def test_bad_tabs_dropped(self):
        self.assertFalse(self.check(Step(key="k", label="l", tabs=[Tab("s", "h")])))
        self.assertFalse(self.check(Step(key="k", label="l", tabs=(("s", "h"),))))
        self.assertFalse(self.check(Step(key="k", label="l", tabs=None)))

    def test_bad_label_and_depends_on_dropped(self):
        self.assertFalse(self.check(Step(key="k", label=None, tabs=())))
        self.assertFalse(self.check(Step(key="k", label="l", tabs=(), depends_on=["a"])))
        self.assertFalse(self.check(Step(key="k", label="l", tabs=(), depends_on=(1,))))

    def test_not_a_step_dropped(self):
        self.assertFalse(self.check({"key": "k"}))


class EnvRegex(unittest.TestCase):
    class A:
        name = "Demo"
        env_var = "ORCA_TERMINAL_HANDLE"
        env_pattern = r"term_[0-9a-f-]+"

    def test_matches_value(self):
        rx = env_regex(self.A)
        m = rx.search("PATH=/bin ORCA_TERMINAL_HANDLE=term_ab-12 HOME=/x")
        self.assertEqual(m.group(1), "term_ab-12")
        self.assertEqual(rx.search("ORCA_TERMINAL_HANDLE=term_ff").group(1), "term_ff")

    def test_anchored(self):
        rx = env_regex(self.A)
        self.assertIsNone(rx.search("FOO_ORCA_TERMINAL_HANDLE=term_ab"))
        self.assertIsNone(rx.search("ORCA_TERMINAL_HANDLE=term_abXYZ"))
        self.assertIsNone(rx.search("ORCA_TERMINAL_HANDLE=term_ab;rm OTHER=1"))

    def test_no_env_var(self):
        class B:
            env_var = None
            env_pattern = None

        self.assertIsNone(env_regex(B))

    def test_env_var_is_escaped_and_checked(self):
        class C:
            env_var = "A.B"
            env_pattern = "x"

        with self.assertRaises(ValueError):
            env_regex(C)


class Shapes(unittest.TestCase):
    def test_frozen_and_defaults(self):
        step = Step(key="k", label="l", tabs=())
        self.assertIsNone(step.verdict)
        self.assertFalse(step.pending)
        self.assertEqual(step.depends_on, ())
        self.assertIsNone(step.manager_session_id)
        with self.assertRaises(Exception):
            step.key = "other"

    def test_handle_re(self):
        self.assertTrue(HANDLE_RE.match("term_ab-12"))
        self.assertFalse(HANDLE_RE.match("a b"))
        self.assertFalse(HANDLE_RE.match("x" * 81))
        self.assertFalse(HANDLE_RE.match("a.b"))

    def test_verdicts(self):
        self.assertEqual(orchestrator.VERDICTS, ("done", "waiting", "closed"))


if __name__ == "__main__":
    unittest.main()
