"""Tests for session_map.tern. The real `tern` is never called: listings come from SESSION_MAP_TERN_JSON
and every program run goes through a patched subprocess.run."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from session_map import tern

LONG_PANE = 1234567890123  # Tern block ids can be 13 digits long


def block(pane, title="a title", live=True, **extra):
    b = {"id": pane, "title": title, "cwd": "/tmp", "program": "zsh", "live": live}
    b.update(extra)
    return b


class WithListing(unittest.TestCase):
    """Each test writes its own `tern ls --json` into a temp file pointed at by SESSION_MAP_TERN_JSON."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.file = Path(self.dir.name) / "tern.json"
        env = mock.patch.dict(os.environ, {tern.ENV_JSON: str(self.file)})
        env.start()
        self.addCleanup(env.stop)
        run = mock.patch.object(subprocess, "run", side_effect=AssertionError("no program may run"))
        run.start()
        self.addCleanup(run.stop)

    def write(self, data):
        self.file.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")

    def tabs(self, *tabs, detached=()):
        """tabs: (number, [blocks]) pairs, all in one Tern session."""
        self.write({"sessions": [{"id": 1, "name": "s", "tabs": [
            {"id": 10 + i, "number": n, "blocks": bs} for i, (n, bs) in enumerate(tabs)]}],
            "detached": list(detached)})

    def read(self):
        problems: list = []
        return tern.read_tabs(problems), problems


class ReadTabs(WithListing):

    def test_live_blocks_with_title_and_tab_number(self):
        self.tabs((1, [block(3, "first")]), (4, [block(7, "second"), block(8, "")]))
        tabs, problems = self.read()
        self.assertEqual(tabs, {3: {"title": "first", "number": 1}, 7: {"title": "second", "number": 4},
                                8: {"title": "", "number": 4}})
        self.assertEqual(problems, [])

    def test_title_is_kept_raw_with_claude_spinner(self):
        # Cleaning the spinner mark off the title is the snapshot's job; Tern gives it as is.
        self.tabs((2, [block(3, "⠂ Fix the page")]))
        tabs, _ = self.read()
        self.assertEqual(tabs[3]["title"], "⠂ Fix the page")

    def test_dead_and_detached_blocks_left_out(self):
        self.tabs((1, [block(1, live=False), block(2, detached=True), block(3), block(4)]),
                  detached=[{"id": 3}, 4])
        self.assertEqual(self.read()[0], {})

    def test_detached_list_alone_leaves_a_block_out(self):
        self.tabs((1, [block(5), block(6)]), detached=[{"id": 5, "title": "x"}])
        self.assertEqual(list(self.read()[0]), [6])

    def test_any_program_counts(self):
        # Every live block counts, whatever program runs in it.
        self.tabs((1, [block(5, program="htop"), block(6, program="zsh")]))
        self.assertEqual(sorted(self.read()[0]), [5, 6])

    def test_thirteen_digit_pane_id(self):
        self.tabs((9, [block(LONG_PANE, "long")]))
        self.assertEqual(self.read()[0], {LONG_PANE: {"title": "long", "number": 9}})

    def test_missing_number_and_odd_entries(self):
        self.write({"sessions": [None, {"tabs": ["x", {"blocks": [None, block(None), block(2)]},
                                                  {"number": "3", "blocks": [block(4)]}]}]})
        tabs, problems = self.read()
        self.assertEqual(tabs, {2: {"title": "a title", "number": None}, 4: {"title": "a title", "number": None}})
        self.assertEqual(problems, [])

    def test_empty_listing(self):
        self.write({})
        self.assertEqual(self.read(), ({}, []))

    def test_bad_json_is_one_problem(self):
        for bad in ("x", "{nope", "[1, 2]", ""):
            with self.subTest(bad=bad):
                self.write(bad)
                tabs, problems = self.read()
                self.assertEqual(tabs, {})
                self.assertEqual(len(problems), 1)
                self.assertTrue(problems[0].startswith("Tern tabs: "))

    def test_missing_file_is_one_problem_without_its_path(self):
        tabs, problems = self.read()  # the file was never written
        self.assertEqual(tabs, {})
        self.assertEqual(len(problems), 1)
        self.assertNotIn(self.dir.name, problems[0])
        self.assertNotIn("tern.json", problems[0])


class WithoutListingFile(unittest.TestCase):
    """No SESSION_MAP_TERN_JSON: the CLI path, with `exe` and subprocess.run patched."""

    def setUp(self):
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(tern.ENV_JSON, None)

    def read(self):
        problems: list = []
        return tern.read_tabs(problems), problems

    def test_tern_missing(self):
        with mock.patch.object(tern, "exe", return_value=None), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("must not run")):
            self.assertEqual(self.read(), ({}, ["Tern tabs: tern is not installed here."]))

    def test_one_ls_call_with_list_args_and_timeout(self):
        out = json.dumps({"sessions": [{"tabs": [{"number": 2, "blocks": [block(LONG_PANE, "t")]}]}]})
        done = subprocess.CompletedProcess([], 0, stdout=out, stderr="")
        with mock.patch.object(tern, "exe", return_value="/fake/tern"), \
                mock.patch.object(subprocess, "run", return_value=done) as run:
            tabs, problems = self.read()
        self.assertEqual((tabs, problems), ({LONG_PANE: {"title": "t", "number": 2}}, []))
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["/fake/tern", "ls", "--json"])
        self.assertEqual(run.call_args.kwargs["timeout"], tern.TIMEOUT_S)
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_failing_tern_is_one_problem(self):
        cases = {
            "exit": subprocess.CompletedProcess([], 2, stdout="", stderr="error at /home/someone/x"),
            "timeout": subprocess.TimeoutExpired(["tern"], 3),
            "oserror": OSError("no such file /home/someone/.local/bin/tern"),
            "not json": subprocess.CompletedProcess([], 0, stdout="hello", stderr=""),
        }
        for name, result in cases.items():
            with self.subTest(name=name):
                kw = {"side_effect": result} if isinstance(result, BaseException) else {"return_value": result}
                with mock.patch.object(tern, "exe", return_value="/fake/tern"), \
                        mock.patch.object(subprocess, "run", **kw):
                    tabs, problems = self.read()
                self.assertEqual(tabs, {})
                self.assertEqual(len(problems), 1)
                self.assertTrue(problems[0].startswith("Tern tabs: "))
                self.assertNotIn("/home/", problems[0])
                self.assertNotIn("/fake/", problems[0])

    def test_exe_prefers_local_bin(self):
        with tempfile.TemporaryDirectory() as home:
            with mock.patch.object(Path, "home", return_value=Path(home)), \
                    mock.patch("shutil.which", return_value="/on/path/tern"):
                self.assertEqual(tern.exe(), "/on/path/tern")
                local = Path(home) / ".local" / "bin" / "tern"
                local.parent.mkdir(parents=True)
                local.write_text("", encoding="utf-8")
                self.assertEqual(tern.exe(), str(local))


class FocusAndOpen(unittest.TestCase):

    def setUp(self):
        patch = mock.patch.object(tern, "exe", return_value="/fake/tern")
        patch.start()
        self.addCleanup(patch.stop)

    def ok(self, code=0):
        return subprocess.CompletedProcess([], code, stdout="", stderr="")

    def test_focus_runs_tern_then_the_app(self):
        with mock.patch.object(subprocess, "run", return_value=self.ok()) as run, \
                mock.patch.object(tern.sys, "platform", "darwin"):
            self.assertTrue(tern.focus(LONG_PANE))
        self.assertEqual([c.args[0] for c in run.call_args_list],
                         [["/fake/tern", "focus", str(LONG_PANE)], ["open", "-a", "Tern"]])

    def test_focus_off_macos_skips_the_app(self):
        with mock.patch.object(subprocess, "run", return_value=self.ok()) as run, \
                mock.patch.object(tern.sys, "platform", "linux"):
            self.assertTrue(tern.focus("42"))
        self.assertEqual([c.args[0] for c in run.call_args_list], [["/fake/tern", "focus", "42"]])

    def test_focus_fails_when_tern_does_not_find_the_pane(self):
        with mock.patch.object(subprocess, "run", return_value=self.ok(1)) as run:
            self.assertFalse(tern.focus(3))
        run.assert_called_once()

    def test_focus_refuses_a_pane_that_is_not_a_number(self):
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("must not run")):
            for bad in ("", "3; rm", "-1", "1" * 21, None, "abc"):
                with self.subTest(bad=bad):
                    self.assertFalse(tern.focus(bad))
                    self.assertFalse(tern.open_beside(bad, "http://127.0.0.1:9131/"))

    def test_focus_without_tern(self):
        with mock.patch.object(tern, "exe", return_value=None), \
                mock.patch.object(subprocess, "run", side_effect=AssertionError("must not run")):
            self.assertFalse(tern.focus(3))
            self.assertFalse(tern.open_beside(3, "http://127.0.0.1:9131/"))

    def test_timeout_and_oserror_are_false(self):
        for err in (subprocess.TimeoutExpired(["tern"], 3), OSError("gone")):
            with self.subTest(err=type(err).__name__), mock.patch.object(subprocess, "run", side_effect=err):
                self.assertFalse(tern.focus(3))
                self.assertFalse(tern.open_beside(3, "http://127.0.0.1:9131/"))

    def test_open_beside_request(self):
        url = "http://127.0.0.1:9131/"
        with mock.patch.object(subprocess, "run", return_value=self.ok()) as run:
            self.assertTrue(tern.open_beside(str(LONG_PANE), url))
        args = run.call_args.args[0]
        self.assertEqual(args[:2], ["/fake/tern", "browser"])
        self.assertEqual(json.loads(args[2]), {"op": "open", "owner": LONG_PANE, "url": url, "how": "beside"})
        self.assertEqual(run.call_args.kwargs["timeout"], tern.TIMEOUT_S)

    def test_open_beside_refused(self):
        with mock.patch.object(subprocess, "run", return_value=self.ok(1)):
            self.assertFalse(tern.open_beside(3, "http://127.0.0.1:9131/"))


if __name__ == "__main__":
    unittest.main()
