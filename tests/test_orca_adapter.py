import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from session_map.orchestrator import load_adapters, validate_step  # noqa: E402

ADAPTER_REF = f"{ROOT / 'examples' / 'orca_adapter.py'}:adapter"
EXAMPLE_STEPS = ROOT / "examples" / "steps.example.json"

H1 = "term_0000aaaa-0000-0000-0000-000000000001"
H2 = "term_0000aaaa-0000-0000-0000-000000000002"
H3 = "term_0000aaaa-0000-0000-0000-000000000003"

LIST_JSON = {
    "id": "x",
    "ok": True,
    "result": {
        "terminals": [
            {"handle": H1, "title": "site · plan", "connected": True},
            {"handle": H2, "title": "site · build", "connected": True},
            {"handle": H3, "title": "gone", "connected": False},
            {"handle": "not a handle", "title": "odd", "connected": True},
        ]
    },
}

# A fake program: records its argv (one JSON line per call) and prints the fixed list answer.
FAKE = """#!{python}
import json, os, sys
with open(os.environ["FAKE_LOG"], "a") as fh:
    fh.write(json.dumps([os.path.basename(sys.argv[0])] + sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["terminal", "list"]:
    with open(os.environ["FAKE_LIST"]) as fh:
        sys.stdout.write(fh.read())
"""


class OrcaAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp)
        bindir = os.path.join(self.tmp, "bin")
        os.mkdir(bindir)
        for prog in ("orca", "open"):
            path = os.path.join(bindir, prog)
            with open(path, "w") as fh:
                fh.write(FAKE.format(python=sys.executable))
            os.chmod(path, 0o755)
        self.log = os.path.join(self.tmp, "calls.log")
        self.list_file = os.path.join(self.tmp, "list.json")
        self.write_list(LIST_JSON)
        env = {"PATH": bindir, "FAKE_LOG": self.log, "FAKE_LIST": self.list_file, "SESSION_MAP_ADAPTERS": ADAPTER_REF}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("SESSION_MAP_STEPS_FILE", None)
        problems = []
        adapters = load_adapters(problems)
        self.assertEqual(problems, [])
        self.assertEqual(len(adapters), 1)
        self.adapter = adapters[0]

    def write_list(self, data):
        with open(self.list_file, "w") as fh:
            fh.write(data if isinstance(data, str) else json.dumps(data))

    def write_steps(self, text):
        path = os.path.join(self.tmp, "steps.json")
        with open(path, "w") as fh:
            fh.write(text)
        os.environ["SESSION_MAP_STEPS_FILE"] = path

    def calls(self):
        with open(self.log) as fh:
            return [json.loads(line) for line in fh]

    def steps(self):
        steps = self.adapter.steps(set(), set())
        problems = []
        self.assertTrue(all(validate_step(s, problems) for s in steps), problems)
        return steps

    def test_shape(self):
        self.assertEqual(self.adapter.name, "Orca")
        self.assertEqual(self.adapter.env_var, "ORCA_TERMINAL_HANDLE")
        self.assertEqual(self.adapter.env_pattern, r"term_[0-9a-f-]+")

    def test_layer1_one_step_per_connected_terminal(self):
        steps = self.steps()
        self.assertEqual([s.label for s in steps], ["site · plan", "site · build"])
        self.assertEqual([s.tabs[0].handle for s in steps], [H1, H2])
        for s in steps:
            self.assertEqual(len(s.tabs), 1)
            self.assertIsNone(s.tabs[0].session_id)
            self.assertIsNone(s.verdict)
            self.assertFalse(s.pending)
            self.assertRegex(s.key, r"^tab-[0-9a-f]{12}$")
            self.assertNotIn(s.tabs[0].handle, s.key)
        self.assertEqual(self.adapter.problems, [])
        self.assertEqual(self.calls(), [["orca", "terminal", "list", "--json"]])

    def test_keys_are_stable(self):
        self.assertEqual([s.key for s in self.steps()], [s.key for s in self.steps()])

    def test_orca_failure_is_one_problem(self):
        self.write_list("not json")
        self.assertEqual(self.steps(), [])
        self.assertEqual(len(self.adapter.problems), 1)

    def test_layer2_replaces_same_handles(self):
        os.environ["SESSION_MAP_STEPS_FILE"] = str(EXAMPLE_STEPS)
        steps = self.steps()
        self.assertEqual([s.key for s in steps], ["site-plan", "site-build", "site-test"])
        plan, build, test = steps
        self.assertEqual(plan.tabs[0].session_id, "plan-session")
        self.assertEqual(plan.manager_session_id, "manager-session")
        self.assertEqual(build.depends_on, ("site-plan",))
        self.assertEqual(test.tabs, ())
        self.assertEqual(self.adapter.problems, [])

    def test_layer2_keeps_unclaimed_layer1_tabs(self):
        self.write_steps(json.dumps({"steps": [
            {"key": "only", "label": "only", "state": "running", "tabs": [{"session_id": "s", "handle": H1}]}]}))
        steps = self.steps()
        self.assertEqual([s.label for s in steps], ["site · build", "only"])

    def test_state_to_step_cell_by_cell(self):
        table = {
            "pending": (True, None),
            "running": (False, None),
            "done": (False, "done"),
            "waiting": (False, "waiting"),
            "closed": (False, "closed"),
        }
        for state, (pending, verdict) in table.items():
            with self.subTest(state=state):
                self.write_steps(json.dumps({"steps": [{"key": "k", "label": "l", "state": state, "tabs": []}]}))
                step = self.steps()[-1]
                self.assertEqual((step.key, step.pending, step.verdict), ("k", pending, verdict))

    def test_bad_step_file_is_one_problem_and_layer1_stays(self):
        bad = [
            "{not json",
            json.dumps({"nosteps": []}),
            json.dumps({"steps": [{"key": "k", "label": "l", "state": "finished", "tabs": []}]}),
            json.dumps({"steps": [{"key": "bad key!", "label": "l", "state": "done", "tabs": []}]}),
        ]
        for text in bad:
            with self.subTest(text=text):
                self.write_steps(text)
                steps = self.steps()
                self.assertEqual([s.tabs[0].handle for s in steps], [H1, H2])
                self.assertEqual(len(self.adapter.problems), 1)
                self.assertNotIn(self.tmp, self.adapter.problems[0])

    def test_missing_step_file_is_one_problem(self):
        os.environ["SESSION_MAP_STEPS_FILE"] = os.path.join(self.tmp, "absent.json")
        self.assertEqual(len(self.steps()), 2)
        self.assertEqual(len(self.adapter.problems), 1)

    def test_problems_reset_each_call(self):
        self.write_steps("{not json")
        self.steps()
        os.environ.pop("SESSION_MAP_STEPS_FILE")
        self.steps()
        self.assertEqual(self.adapter.problems, [])

    def test_focus_argv_exact(self):
        self.assertTrue(self.adapter.focus(H1))
        calls = self.calls()
        self.assertEqual(calls[0], ["orca", "terminal", "switch", "--terminal", H1, "--json"])
        if sys.platform == "darwin":
            self.assertEqual(calls[1:], [["open", "-a", "Orca"]])
        else:
            self.assertEqual(calls[1:], [])

    def test_focus_refuses_non_handle(self):
        for bad in ("term_X; rm", "--json", "", "abc"):
            with self.subTest(bad=bad):
                self.assertFalse(self.adapter.focus(bad))
        self.assertFalse(os.path.exists(self.log))

    def test_focus_false_when_orca_fails(self):
        os.chmod(os.path.join(self.tmp, "bin", "orca"), 0o644)  # no longer runnable
        with mock.patch("shutil.which", return_value=os.path.join(self.tmp, "bin", "orca")):
            self.assertFalse(self.adapter.focus(H1))


if __name__ == "__main__":
    unittest.main()
