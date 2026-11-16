"""The no-network / no-stray-program bar over the sources subpackage (session_map/sources/).

tests/test_structure.py scans only session_map/*.py (its MODULES pin stays unedited); this file runs the very
same check_python over every session_map/sources/*.py and pins that module list, so a new source file cannot
land unscanned. The ruler is shown both ways: the real files are quiet, a planted bad file in a temp copy of the
folder is flagged.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from tests.test_structure import PACKAGE, check_python

SOURCES = PACKAGE / "sources"
# The pin. Task 2.1 (the Codex source) adds "codex.py" here in the same change that creates the file, so the
# list is always exactly what ships; a file added without this edit turns test_every_module_is_pinned red.
MODULES = ("__init__.py",)

_BAD = ('import subprocess\nimport urllib.request\n'
        'def f():\n    subprocess.run(["lsof", "-p", "1"])\n')


class SourcesStructure(unittest.TestCase):

    def test_every_module_is_pinned(self):
        self.assertEqual(sorted(p.name for p in SOURCES.glob("*.py")), sorted(MODULES))

    def test_real_modules_are_clean(self):
        for name in MODULES:
            with self.subTest(module=name):
                self.assertEqual(check_python(SOURCES / name), [])

    def test_planted_bad_file_in_a_copy_is_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "sources"
            shutil.copytree(SOURCES, copy, ignore=shutil.ignore_patterns("__pycache__"))
            for p in copy.glob("*.py"):  # the copy of the real files is as quiet as the originals
                self.assertEqual(check_python(p), [], p.name)
            bad = copy / "bad.py"
            bad.write_text(_BAD, encoding="utf-8")
            e = check_python(bad)
            self.assertEqual(len(e), 2, e)
            self.assertTrue(any("import forbidden: urllib.request" in x for x in e), e)
            self.assertTrue(any("runs a program outside the list: lsof" in x for x in e), e)
            init = copy / "__init__.py"  # the same lines appended to a real module are flagged there too
            init.write_text(init.read_text(encoding="utf-8") + "\n" + _BAD, encoding="utf-8")
            e = check_python(init)
            self.assertTrue(any("urllib.request" in x for x in e) and any("lsof" in x for x in e), e)


if __name__ == "__main__":
    unittest.main()
