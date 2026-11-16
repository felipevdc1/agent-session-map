"""No model, no network, no stray programs, checked by structure: what the package imports, which
programs it runs, where it loads code dynamically, and where the page talks to.

check_python(path) and check_page(path) each return a list of violations (empty = clean).
Every rule is tested both ways: planted bad files in a temp folder must be flagged, planted good
files and the real package files must stay quiet.

Scope: every module in session_map/ and session_map/page.html. Commands an adapter runs (for
example the Orca adapter in examples/) are outside this check; the example adapter has its own test.
"""

from __future__ import annotations

import ast
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

PACKAGE = REPO / "session_map"
MODULES = ("__init__.py", "__main__.py", "transcript.py", "tern.py", "orchestrator.py",
           "snapshot.py", "server.py", "cli.py")

# Standard-library modules the package may import anywhere.
STDLIB_OK = {
    "__future__", "argparse", "base64", "collections", "contextlib", "copy", "dataclasses",
    "datetime", "enum", "errno", "fnmatch", "functools", "glob", "hashlib", "hmac", "html",
    "importlib", "importlib.util", "io", "itertools", "json", "logging", "math", "os", "os.path",
    "pathlib", "random", "re", "secrets", "select", "shutil", "signal", "stat", "statistics",
    "string", "struct", "subprocess", "sys", "tempfile", "textwrap", "threading", "time",
    "traceback", "typing", "unicodedata", "uuid", "zoneinfo",
}
# Only the local server may open sockets and serve HTTP (its probe uses a plain socket).
SERVER_ONLY = {"socket", "socketserver", "http", "http.server"}
# Never, anywhere (named so the message is explicit).
FORBIDDEN = {"urllib", "urllib.request", "urllib.parse", "urllib3", "http.client", "ssl",
             "requests", "httpx", "aiohttp", "anthropic", "openai"}
_FORBIDDEN_ROOTS = {"requests", "httpx", "aiohttp", "anthropic", "openai", "urllib3"}
PACKAGE_NAME = "session_map"
# Programs the package may run (by basename).
PROGRAMS = {"ps", "tern", "open", "xdg-open"}
# The one self-spawn, allowed in server.py only.
SELF_SPAWN = ("-m", PACKAGE_NAME, "serve")
_SUBPROCESS = {"run", "Popen", "check_output", "call", "check_call", "getoutput", "getstatusoutput"}
# Dynamic imports: allowed only inside orchestrator.load_adapters.
DYNAMIC_IMPORT = {"import_module", "spec_from_file_location", "module_from_spec", "exec_module", "__import__"}
DYNAMIC_HOME = ("orchestrator.py", "load_adapters")
# Dynamic code: never.
DYNAMIC_CODE = {"exec", "eval", "compile"}


def _judge_module(name, server):
    if name == PACKAGE_NAME or name.startswith(PACKAGE_NAME + "."):
        return None
    if name in FORBIDDEN or name.split(".")[0] in _FORBIDDEN_ROOTS:
        return f"import forbidden: {name}"
    if name in STDLIB_OK:
        return None
    if name in SERVER_ONLY:
        return None if server else f"import only allowed in server.py: {name}"
    return f"import outside the allowed list: {name}"


def _assignments(scope, name):
    """Values assigned to `name` anywhere inside `scope` (a function or the module)."""
    vals = []
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    vals.append(node.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
            if node.value is not None:
                vals.append(node.value)
    return vals


def _params(func):
    if func is None:
        return set()
    a = func.args
    names = [p.arg for p in a.posonlyargs + a.args + a.kwonlyargs]
    names += [p.arg for p in (a.vararg, a.kwarg) if p is not None]
    return set(names)


def _module_values(module, name):
    return [n.value for n in module.body if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)]


class _Resolver:
    """Resolves an expression to the set of strings it can be, or None (unknown)."""

    def __init__(self, module):
        self.module = module
        self.functions = {n.name: n for n in module.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def _union(self, items, depth):
        """Union of names() over [(node, function, binds)]; None if any is unknown or the list is empty."""
        if not items:
            return None
        out = set()
        for node, f, b in items:
            r = self.names(node, f, depth + 1, b)
            if r is None:
                return None
            out |= r
        return out

    def _returns(self, fdef, call, func, binds, depth):
        """What module function `fdef` returns when called as `call`: its params bound to the call's args."""
        a = fdef.args
        if call.keywords or a.kwonlyargs or a.kwarg or a.posonlyargs:
            return None
        params = [p.arg for p in a.args]
        if len(call.args) > len(params) and a.vararg is None:
            return None
        if len(call.args) < len(params) - len(a.defaults):
            return None
        new = {p: [(arg, func, binds)] for p, arg in zip(params, call.args)}
        for p, default in zip(params[len(params) - len(a.defaults):], a.defaults):
            new.setdefault(p, [(default, fdef, {})])
        if a.vararg is not None:
            new[a.vararg.arg] = [(arg, func, binds) for arg in call.args[len(params):]]
        returns = [n for n in ast.walk(fdef) if isinstance(n, ast.Return)]
        if not returns or any(r.value is None for r in returns):
            return None
        return self._union([(r.value, fdef, new) for r in returns], depth)

    def names(self, node, func, depth=0, binds=None):
        binds = binds or {}
        if depth > 12:
            return None
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.IfExp):
            return self._union([(node.body, func, binds), (node.orelse, func, binds)], depth)
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            return self._union([(v, func, binds) for v in node.values], depth)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):  # Path / "x" -> its last part
            return self.names(node.right, func, depth + 1, binds)
        if isinstance(node, ast.Call):
            f = node.func
            # tern.exe() from another module of the package: tern.py's own exe() is judged in tern.py
            if isinstance(f, ast.Attribute) and f.attr == "exe" and isinstance(f.value, ast.Name) \
                    and f.value.id == "tern" and not node.args and not node.keywords:
                return {"tern"}
            fname = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
            if fname in {"str", "Path", "expanduser", "abspath", "realpath", "fspath", "which"} \
                    and len(node.args) == 1 and not node.keywords:
                return self.names(node.args[0], func, depth + 1, binds)
            if isinstance(f, ast.Name) and f.id in self.functions:  # a helper like exe(): what it returns
                return self._returns(self.functions[f.id], node, func, binds, depth)
            return None
        if isinstance(node, ast.Name):
            if node.id in binds:
                return self._union(binds[node.id], depth)
            items = []
            if func is not None:
                items = [(v, func, binds) for v in _assignments(func, node.id)]
                for n in ast.walk(func):  # `for c in extras:` -> what extras holds
                    if isinstance(n, ast.For) and isinstance(n.target, ast.Name) and n.target.id == node.id:
                        if not isinstance(n.iter, ast.Name) or n.iter.id not in binds:
                            return None
                        items += binds[n.iter.id]
                if not items and node.id in _params(func):  # an unbound parameter: unknown
                    return None
            if not items:  # module constant: top-level assignments only
                items = [(v, None, {}) for v in _module_values(self.module, node.id)]
            return self._union(items, depth)
        return None

    def lists(self, node, func, depth=0):
        """[(List/Tuple literal, its function)] the argv expression `node` can be, or None (unknown)."""
        if depth > 8:
            return None
        if isinstance(node, (ast.List, ast.Tuple)):
            return [(node, func)]
        if isinstance(node, ast.IfExp):
            a, b = self.lists(node.body, func, depth + 1), self.lists(node.orelse, func, depth + 1)
            return None if a is None or b is None else a + b
        if isinstance(node, ast.Name):
            vals, owner = (_assignments(func, node.id), func) if func is not None else ([], None)
            if not vals:
                if node.id in _params(func):
                    return None
                vals, owner = _module_values(self.module, node.id), None
            if not vals:
                return None
            out = []
            for v in vals:
                r = self.lists(v, owner, depth + 1)
                if r is None:
                    return None
                out += r
            return out
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in self.functions:
            fdef = self.functions[node.func.id]
            returns = [n for n in ast.walk(fdef) if isinstance(n, ast.Return)]
            if not returns or any(r.value is None for r in returns):
                return None
            out = []
            for r in returns:
                got = self.lists(r.value, fdef, depth + 1)
                if got is None:
                    return None
                out += got
            return out
        return None


def check_python(path):
    path = Path(path)
    server = path.name == "server.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    res = _Resolver(tree)
    errors = []

    def where(node):
        return f"{path.name}:{getattr(node, 'lineno', '?')}"

    # imports, and the local names that reach subprocess / os / importlib functions
    sub_aliases, os_aliases = {"subprocess"}, {"os"}
    sub_funcs, os_funcs, imp_funcs = {}, {}, {}  # local name -> real name
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                e = _judge_module(a.name, server)
                if e:
                    errors.append(f"{where(node)}: {e}")
                if a.name == "subprocess":
                    sub_aliases.add(a.asname or a.name)
                if a.name == "os":
                    os_aliases.add(a.asname or a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # a relative import stays inside the package
                continue
            mod = node.module or ""
            for a in node.names:
                full = f"{mod}.{a.name}"
                target = full if (full in STDLIB_OK or full in SERVER_ONLY or full in FORBIDDEN) else mod
                e = _judge_module(target, server)
                if e:
                    errors.append(f"{where(node)}: {e}")
                if mod == "subprocess":
                    sub_funcs[a.asname or a.name] = a.name
                if mod == "os":
                    os_funcs[a.asname or a.name] = a.name
                if mod in {"importlib", "importlib.util"} and a.name in DYNAMIC_IMPORT:
                    imp_funcs[a.asname or a.name] = a.name

    calls = []  # (call, its enclosing function)

    def visit(node, func):
        for child in ast.iter_child_nodes(node):
            f = func
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                f = child
            if isinstance(child, ast.Call):
                calls.append((child, f))
                check_call(child, f)
            visit(child, f)

    def sub_of(call):
        fn = call.func
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name) and fn.value.id in sub_aliases:
            return fn.attr if fn.attr in _SUBPROCESS else None
        n = sub_funcs.get(fn.id) if isinstance(fn, ast.Name) else None
        return n if n in _SUBPROCESS else None

    # Wrappers: a module function that hands one of its own parameters to subprocess (or to another
    # wrapper). The call inside is not judged; every call site of the wrapper is, by the same rule.
    wrappers = {}  # name -> (index, param name)

    def execution(call):
        """(label, the argv argument) when `call` runs a program, else None."""
        sub = sub_of(call)
        if sub:
            if sub in {"getoutput", "getstatusoutput"}:
                return None
            arg = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "args"), None)
            return (f"subprocess.{sub}", arg)
        if isinstance(call.func, ast.Name) and call.func.id in wrappers:
            i, p = wrappers[call.func.id]
            arg = call.args[i] if len(call.args) > i else next((k.value for k in call.keywords if k.arg == p), None)
            return (f"{call.func.id}()", arg)
        return None

    def parameter(arg, func):
        """(index, name) when `arg` is a never-reassigned parameter of a module-level `func`."""
        if not isinstance(arg, ast.Name) or func is None or res.functions.get(func.name) is not func:
            return None
        params = [p.arg for p in func.args.args]
        if arg.id not in params or _assignments(func, arg.id):
            return None
        return (params.index(arg.id), arg.id)

    def is_sys_executable(node):
        return isinstance(node, ast.Attribute) and node.attr == "executable" \
            and isinstance(node.value, ast.Name) and node.value.id == "sys"

    def check_program(call, func, label, argv):
        found = res.lists(argv, func) if argv is not None else None
        if not found or any(not lst.elts for lst, _ in found):
            errors.append(f"{where(call)}: {label} without a literal argument list")
            return
        for lst, owner in found:
            head = lst.elts[0]
            if is_sys_executable(head):
                rest = tuple(e.value if isinstance(e, ast.Constant) else None for e in lst.elts[1:])
                if not server or rest != SELF_SPAWN:
                    errors.append(f"{where(call)}: {label} runs sys.executable other than the one self-spawn")
                continue
            progs = res.names(head, owner)
            if progs is None:
                errors.append(f"{where(call)}: {label} with a program that cannot be resolved")
                continue
            for p in sorted(progs):
                if os.path.basename(p) not in PROGRAMS:
                    errors.append(f"{where(call)}: {label} runs a program outside the list: {p}")

    def check_executions():
        changed = True
        while changed:
            changed = False
            for call, f in calls:
                ex = execution(call)
                pr = parameter(ex[1], f) if ex else None
                if pr and f.name not in wrappers:
                    wrappers[f.name] = pr
                    changed = True
        for call, f in calls:
            ex = execution(call)
            if not ex:
                continue
            if f is not None and f.name in wrappers and parameter(ex[1], f) == wrappers[f.name]:
                continue  # inside the wrapper: judged at its call sites
            check_program(call, f, *ex)
        used = {id(call.func) for call, _ in calls}
        for n in ast.walk(tree):  # a wrapper passed around uncalled would dodge the call-site check
            if isinstance(n, ast.Name) and n.id in wrappers and isinstance(n.ctx, ast.Load) and id(n) not in used:
                errors.append(f"{where(n)}: {n.id} (runs programs) used without being called")

    def check_call(call, func):
        fn = call.func
        base = attr = None
        if isinstance(fn, ast.Attribute):  # attr even for a.b.c(); base only for a plain name.c()
            attr = fn.attr
            base = fn.value.id if isinstance(fn.value, ast.Name) else None
        name = fn.id if isinstance(fn, ast.Name) else None
        if name in DYNAMIC_CODE:
            errors.append(f"{where(call)}: dynamic code is forbidden: {name}()")
            return
        dynamic = imp_funcs.get(name) or ("__import__" if name == "__import__" else None) \
            or (attr if attr in DYNAMIC_IMPORT else None)
        if dynamic:
            home = func is not None and (path.name, func.name) == DYNAMIC_HOME \
                and res.functions.get(func.name) is func
            if not home:
                errors.append(f"{where(call)}: dynamic import outside orchestrator.load_adapters: {dynamic}()")
            return
        os_name = attr if base in os_aliases else os_funcs.get(name)
        if os_name and (os_name in {"system", "popen"} or os_name.startswith(("exec", "spawn", "posix_spawn"))):
            errors.append(f"{where(call)}: os.{os_name} is forbidden")
            return
        sub_name = attr if base in sub_aliases else sub_funcs.get(name)
        if not sub_name or sub_name not in _SUBPROCESS:
            return
        if sub_name in {"getoutput", "getstatusoutput"}:
            errors.append(f"{where(call)}: subprocess.{sub_name} runs through the shell")
            return
        for kw in call.keywords:
            if kw.arg == "shell" and not (isinstance(kw.value, ast.Constant) and kw.value.value is False):
                errors.append(f"{where(call)}: subprocess.{sub_name} with shell=")
            if kw.arg == "executable":
                errors.append(f"{where(call)}: subprocess.{sub_name} with executable=")
        # the program itself is judged in check_executions (after wrappers are known)

    visit(tree, None)
    check_executions()
    return errors


_SVG_NS = "http://www.w3.org/2000/svg"
_FETCH_RE = re.compile(r"\bfetch\s*\(\s*(?:([\"'`])([^\"'`]*)\1)?")
_URL_RE = re.compile(r"(?:\b[A-Za-z][A-Za-z0-9+.-]*:)?//[A-Za-z0-9.-]+(?:\.[A-Za-z]{2,}|:\d+)[^\s\"'`<>)]*")


def check_page(path):
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    errors = []

    def line(pos):
        return f"{path.name}:{text.count(chr(10), 0, pos) + 1}"

    for m in _FETCH_RE.finditer(text):
        target = m.group(2)
        if target is None:
            errors.append(f"{line(m.start())}: fetch( without a literal address")
        elif not re.fullmatch(r"/(?:state|focus)(?:\?.*)?", target):
            errors.append(f"{line(m.start())}: fetch outside /state and /focus: {target}")
    rules = [
        (r"<script\b[^>]*\bsrc\s*=", "<script src=>", re.IGNORECASE),
        (r"<link\b[^>]*\bhref\s*=", "<link href=>", re.IGNORECASE),
        (r"<(?:img|iframe|frame|audio|video|source|track|embed|object)\b[^>]*\b(?:src|srcset|data)\s*=",
         "embedded resource", re.IGNORECASE),
        (r"\.(?:src|srcset)\s*=(?!=)", "src assignment", 0),
        (r"\burl\s*\(\s*(?![\"']?data:)", "CSS url()", 0),
        (r"\bimport\s*\(", "dynamic import(", re.IGNORECASE),
        (r"\bimportScripts\b", "importScripts", 0),
        (r"\bnew\s+(?:Shared)?Worker\b", "Worker", 0),
        (r"\bnew\s+WebSocket\b", "WebSocket", re.IGNORECASE),
        (r"\bXMLHttpRequest\b", "XMLHttpRequest", re.IGNORECASE),
        (r"\bsendBeacon\b", "sendBeacon", re.IGNORECASE),
        (r"\bwindow\.open\s*\(", "window.open", 0),
        (r"\blocation\s*\.\s*(?:href\s*=(?!=)|assign\b|replace\b)", "navigation", 0),
        (r"@import\b", "CSS @import", re.IGNORECASE),
    ]
    for pattern, label, flags in rules:
        for m in re.finditer(pattern, text, flags):
            errors.append(f"{line(m.start())}: {label}")
    for m in re.finditer(r"\bnew\s+EventSource\s*\(\s*(?:([\"'`])([^\"'`]*)\1)?", text):
        target = m.group(2)
        if target is None or not re.fullmatch(r"/(?:state|focus)(?:\?.*)?", target):
            errors.append(f"{line(m.start())}: EventSource outside /state and /focus: {target}")
    for m in _URL_RE.finditer(text):
        url = m.group(0)
        if url.startswith("//") and text[max(0, m.start() - 1)] not in "\"'`(=":  # a JS comment, not a URL
            continue
        if url.rstrip("\"';,") == _SVG_NS:
            continue
        errors.append(f"{line(m.start())}: external address: {url}")
    return errors


# --------------------------------------------------------------------------- tests

def _plant(folder, name, content):
    p = Path(folder) / name
    p.write_text(content, encoding="utf-8")
    return p


class _Planted(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def errors(self, content, name="snapshot.py"):
        return check_python(_plant(self.tmp.name, name, content))


class ImportRuleBites(_Planted):
    """Imports: stdlib and the package only; network libraries never; sockets in server.py only."""

    def test_network_import_and_unknown_program_both_flagged(self):
        e = self.errors('import subprocess\nimport urllib.request\n'
                        'def f():\n    subprocess.run(["claude", "-p", "x"])\n')
        self.assertEqual(len(e), 2, e)
        self.assertTrue(any("urllib.request" in x for x in e), e)
        self.assertTrue(any("claude" in x for x in e), e)

    def test_from_imports_of_network_modules_flagged(self):
        e = self.errors('from http import client\nfrom urllib import request\nimport ssl\nimport requests\n')
        self.assertEqual(len(e), 4, e)

    def test_third_party_and_lookalike_package_flagged(self):
        e = self.errors('import yaml\nfrom session_mapx import thing\nimport session_map_extra\n')
        self.assertEqual(len(e), 3, e)

    def test_package_and_relative_imports_pass(self):
        good = ('from . import tern\nfrom .orchestrator import Step\nfrom .. import x\n'
                'import session_map.tern\nfrom session_map import snapshot\nimport datetime as dt\n'
                'from typing import Optional\n')
        self.assertEqual(self.errors(good), [])

    def test_sockets_only_in_server(self):
        good = 'import socket\nimport socketserver\nfrom http.server import ThreadingHTTPServer\n'
        self.assertEqual(len(self.errors(good, "snapshot.py")), 3)
        self.assertEqual(len(self.errors(good, "tern.py")), 3)
        self.assertEqual(self.errors(good, "server.py"), [])

    def test_server_still_refuses_http_clients(self):
        e = self.errors('import urllib.request\nimport http.client\nfrom http import client\n', "server.py")
        self.assertEqual(len(e), 3, e)


class ProcessRuleBites(_Planted):
    """No shell, no os.system/popen/exec, no alias that dodges the rule, no unresolved program."""

    def test_shell_true_flagged(self):
        e = self.errors('import subprocess\nsubprocess.run(["ps"], shell=True)\n')
        self.assertTrue(any("shell=" in x for x in e), e)
        e = self.errors('import subprocess\nsubprocess.run("ps aux", shell=True)\n')
        self.assertTrue(any("shell=" in x for x in e) and any("literal argument list" in x for x in e), e)
        e = self.errors('import subprocess\nsubprocess.getoutput("ps")\n')
        self.assertTrue(any("through the shell" in x for x in e), e)

    def test_os_system_popen_exec_flagged(self):
        e = self.errors('import os\nos.system("tern ls")\nos.popen("ps")\nos.execv("/bin/ps", ["ps"])\n')
        self.assertEqual(len(e), 3, e)
        e = self.errors('from os import system as s\ns("ps")\n')
        self.assertEqual(len(e), 1, e)

    def test_aliases_do_not_escape(self):
        e = self.errors('import subprocess as sp\nfrom subprocess import Popen as P\n'
                        'sp.check_output(["curl", "x"])\nP(["claude"])\n')
        self.assertEqual(len(e), 2, e)

    def test_unresolved_program_flagged(self):
        e = self.errors('import subprocess, sys\ndef f(cmd):\n    subprocess.run(cmd)\n'
                        'def g(cmd):\n    f(["claude"])\n    f(cmd + ["x"])\n'
                        'def h():\n    subprocess.run([sys.argv[1], "x"])\n')
        self.assertEqual(len(e), 3, e)

    def test_unbound_parameter_does_not_borrow_a_module_constant(self):
        e = self.errors('import subprocess\nprog = "ps"\ndef f(prog):\n    subprocess.run([prog, "x"])\n'
                        'def g():\n    f("claude")\n')
        self.assertTrue(any("cannot be resolved" in x for x in e), e)

    def test_executable_keyword_flagged(self):
        e = self.errors('import subprocess\nsubprocess.run(["ps"], executable="/bin/sh")\n')
        self.assertTrue(any("executable=" in x for x in e), e)

    def test_dynamic_code_flagged_everywhere(self):
        for code in ('exec("x = 1")', 'eval("1")', 'compile("1", "f", "eval")'):
            e = self.errors(f'def load_adapters(problems):\n    {code}\n', "orchestrator.py")
            self.assertEqual(len(e), 1, (code, e))
            self.assertIn("dynamic code", e[0])


_GOOD_ORCHESTRATOR = (
    'import importlib\nimport importlib.util\nimport sys\n'
    'from importlib import import_module as load\n'
    'def load_adapters(problems):\n'
    '    spec = importlib.util.spec_from_file_location("m", "a.py")\n'
    '    module = importlib.util.module_from_spec(spec)\n'
    '    sys.modules["m"] = module\n'
    '    spec.loader.exec_module(module)\n'
    '    importlib.import_module("pkg.mod")\n'
    '    load("pkg.mod")\n'
    '    __import__("pkg")\n'
    '    return [module]\n'
)


class DynamicImportRuleBites(_Planted):
    """importlib / __import__ calls live only in orchestrator.load_adapters."""

    _CALLS = ('importlib.import_module("x")', 'importlib.util.spec_from_file_location("m", "a.py")',
              'importlib.util.module_from_spec(spec)', 'spec.loader.exec_module(module)', '__import__("x")',
              'load("x")')

    def test_load_adapters_in_orchestrator_stays_quiet(self):
        self.assertEqual(self.errors(_GOOD_ORCHESTRATOR, "orchestrator.py"), [])

    def test_same_call_in_another_function_flagged(self):
        for call in self._CALLS:
            e = self.errors(_GOOD_ORCHESTRATOR + f'def other(spec, module):\n    {call}\n', "orchestrator.py")
            self.assertEqual(len(e), 1, (call, e))
            self.assertIn("outside orchestrator.load_adapters", e[0])

    def test_same_call_at_module_level_flagged(self):
        e = self.errors(_GOOD_ORCHESTRATOR + 'importlib.import_module("x")\n', "orchestrator.py")
        self.assertEqual(len(e), 1, e)

    def test_same_function_in_another_module_flagged(self):
        e = self.errors(_GOOD_ORCHESTRATOR, "snapshot.py")
        self.assertEqual(len(e), 6, e)
        self.assertTrue(all("outside orchestrator.load_adapters" in x for x in e), e)

    def test_nested_function_inside_load_adapters_flagged(self):
        e = self.errors('import importlib\ndef load_adapters(problems):\n'
                        '    def inner():\n        return importlib.import_module("x")\n    return inner()\n',
                        "orchestrator.py")
        self.assertEqual(len(e), 1, e)

    def test_method_named_load_adapters_flagged(self):
        e = self.errors('import importlib\nclass C:\n    def load_adapters(self):\n'
                        '        importlib.import_module("x")\n', "orchestrator.py")
        self.assertEqual(len(e), 1, e)


# The shapes the real package uses: a generic _run(args) wrapper, exe() and tern.exe(), a
# browser_command() that returns one of two lists, ps with a literal list, and the one self-spawn.
_GOOD_SERVER = (
    'import shutil\nimport subprocess\nimport sys\nfrom pathlib import Path\nfrom . import tern\n'
    'def exe():\n    local = Path.home() / ".local" / "bin" / "tern"\n'
    '    return str(local) if local.is_file() else shutil.which("tern")\n'
    'def _run(args):\n    r = subprocess.run(args, capture_output=True, check=False)\n'
    '    return r.returncode == 0\n'
    'def focus(p):\n    program = exe()\n    if _run([program, "focus", p]):\n        _run(["open", "-a", "Tern"])\n'
    'def listing():\n    return subprocess.run([exe(), "ls", "--json"], capture_output=True, check=False)\n'
    'def via_module(p):\n    _run([tern.exe(), "focus", p])\n'
    'def browser_command(url):\n    return ["open", url] if sys.platform == "darwin" else ["xdg-open", url]\n'
    'def show(url):\n    _run(browser_command(url))\n'
    'def ps(pids):\n    subprocess.run(["ps", "eww", "-o", "pid=,command=", "-p", ",".join(pids)],'
    ' capture_output=True)\n'
    'def spawn():\n    subprocess.Popen([sys.executable, "-m", "session_map", "serve"], start_new_session=True)\n'
)


class ProgramRuleBites(_Planted):
    """Only ps, tern, open / xdg-open, and the one self-spawn from server.py."""

    def errors(self, content, name="server.py"):
        return super().errors(content, name)

    def test_good_server_stays_quiet(self):
        self.assertEqual(self.errors(_GOOD_SERVER), [])

    def test_wrapper_called_with_another_program_flagged(self):
        e = self.errors(_GOOD_SERVER + 'def bad():\n    _run(["claude", "-p", "x"])\n')
        self.assertEqual(len(e), 1, e)
        self.assertIn("_run() runs a program outside the list: claude", e[0])

    def test_orca_is_outside_the_core(self):
        e = self.errors(_GOOD_SERVER + 'def bad():\n    _run(["orca", "terminal", "list", "--json"])\n')
        self.assertEqual(len(e), 1, e)
        self.assertIn("orca", e[0])

    def test_wrapper_of_wrapper_and_passed_uncalled(self):
        e = self.errors(_GOOD_SERVER + 'def r2(cmd):\n    return _run(cmd)\n'
                        'def bad():\n    r2(["claude"])\n    list(map(_run, [["claude"]]))\n')
        self.assertTrue(any("r2() runs a program outside the list: claude" in x for x in e), e)
        self.assertTrue(any("used without being called" in x for x in e), e)

    def test_helpers_that_return_another_program_flagged(self):
        e = self.errors(_GOOD_SERVER.replace('shutil.which("tern")', 'shutil.which("claude")'))
        self.assertEqual(len(e), 2, e)  # focus() and listing() both use exe()
        self.assertTrue(all("claude" in x for x in e), e)
        e = self.errors(_GOOD_SERVER.replace('["xdg-open", url]', '["curl", url]'))
        self.assertEqual(len(e), 1, e)
        self.assertIn("curl", e[0])
        e = self.errors(_GOOD_SERVER.replace('_run([tern.exe(),', '_run([other.exe(),'))
        self.assertEqual(len(e), 1, e)
        self.assertIn("cannot be resolved", e[0])

    def test_browser_command_with_an_unknown_head_flagged(self):
        e = self.errors(_GOOD_SERVER.replace('["open", url] if', '[url] if'))
        self.assertTrue(any("cannot be resolved" in x for x in e), e)

    def test_other_sys_executable_forms_flagged(self):
        for argv in ('[sys.executable, "-c", "x"]', '[sys.executable, "-m", "session_map", "snapshot"]',
                     '[sys.executable, "-m", "other", "serve"]', '[sys.executable, "-m", "session_map", "serve", "x"]',
                     '[sys.executable]', '[sys.executable, str(Path(__file__))]'):
            e = self.errors(_GOOD_SERVER + f'def bad():\n    subprocess.Popen({argv})\n')
            self.assertEqual(len(e), 1, (argv, e))
            self.assertIn("sys.executable", e[0])

    def test_self_spawn_outside_server_flagged(self):
        e = super().errors('import subprocess, sys\n'
                           'def spawn():\n    subprocess.Popen([sys.executable, "-m", "session_map", "serve"])\n',
                           "snapshot.py")
        self.assertEqual(len(e), 1, e)
        self.assertIn("sys.executable", e[0])


class PageRuleBites(_Planted):
    def page(self, content):
        return check_page(_plant(self.tmp.name, "page.html", content))

    def test_cdn_and_external_api_flagged(self):
        e = self.page('<script src="https://cdn.example.com/x.js"></script>\n'
                      '<script>fetch("https://api.example.com/v1").then(f)</script>\n')
        self.assertTrue(any("<script src=>" in x for x in e), e)
        self.assertTrue(any("fetch outside" in x for x in e), e)
        self.assertTrue(any("external address: https://cdn" in x for x in e), e)

    def test_other_local_endpoints_flagged(self):
        for target in ("/status", "/go", "/state/../x", "/focusx", "state"):
            e = self.page(f'<script>fetch("{target}");</script>\n')
            self.assertTrue(any("fetch outside" in x for x in e), (target, e))

    def test_other_ways_out_flagged(self):
        e = self.page('<link href="style.css" rel="stylesheet">\n<img src="x.png">\n<script>\n'
                      'import("./m.js");\nnew WebSocket("ws://x");\nnew EventSource("/events");\nfetch(url);\n'
                      'var r = new XMLHttpRequest();\nnavigator.sendBeacon("/x");\nvar u = "//evil.example.com/p";\n'
                      'img.src = "a.png";\nwindow.open("/x");\nlocation.href = "/x";\nnew Worker("w.js");\n'
                      'var s = "ws://127.0.0.1:9131/s";\n</script>\n<style>@import "x.css"; a { b: url(x.png) }</style>\n')
        for label in ("<link href=>", "embedded resource", "dynamic import(", "WebSocket", "EventSource",
                      "fetch( without", "XMLHttpRequest", "sendBeacon", "evil.example.com", "src assignment",
                      "window.open", "navigation", "Worker", "ws://127.0.0.1:9131", "CSS @import", "CSS url()"):
            self.assertTrue(any(label in x for x in e), (label, e))

    def test_good_page_stays_quiet(self):
        good = ('<svg xmlns="http://www.w3.org/2000/svg"></svg>\n<script>\n'
                'var SVGNS = "http://www.w3.org/2000/svg";\n// a comment // with slashes\n'
                'fetch("/state", {cache: "no-store"});\nfetch("/focus", {method: "POST"});\n'
                'fetch("/state?x=1");\nif (a == b) {}\n</script>\n<style>a { b: url(data:image/png;base64,AA) }</style>\n')
        self.assertEqual(self.page(good), [])


class RealFilesQuiet(unittest.TestCase):
    """The real package files: zero violations, and no module left unscanned."""

    def test_every_module_is_in_scope(self):
        self.assertEqual(sorted(p.name for p in PACKAGE.glob("*.py")), sorted(MODULES))

    def test_python_modules(self):
        for name in MODULES:
            with self.subTest(module=name):
                self.assertEqual(check_python(PACKAGE / name), [])

    def test_page(self):
        self.assertEqual(check_page(PACKAGE / "page.html"), [])


if __name__ == "__main__":
    unittest.main()
