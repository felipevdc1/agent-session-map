"""Tests for page.html: the pure layout, the state classes, the CSS motion
budget and the embedded-snapshot contract.

Run from the repository root:
  python3 -m unittest discover -s tests -p "test_page.py"

The page's pure logic lives in <script id="logic"> and exports itself to
node via a guarded module.exports; node runs it here. SESSION_MAP_PAGE swaps
the page under test (used to show each checker fails on a planted bad page).

Every checker below is also fed a planted bad input in CheckersBite, so a
blind checker cannot pass silently.
"""

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PAGE = Path(os.environ.get("SESSION_MAP_PAGE") or REPO / "session_map" / "page.html")
EXAMPLE = REPO / "examples" / "example.json"

DATA_TAG = '<script id="data" type="application/json">null</script>'
STATES = ["working", "waiting", "done", "stopped", "closed", "next"]
WIDTHS = (1440, 390)

NODE = shutil.which("node")


def snapshot_embed():
    """The package's own embed function, or None when the core is not importable."""
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))
    try:
        mod = importlib.import_module("session_map.snapshot")
    except Exception:  # noqa: BLE001
        return None
    return getattr(mod, "embed", None)


# ---------- the escape rule of the fixed format (one place) ----------

def embed(page, snapshot):
    j = json.dumps(snapshot, ensure_ascii=False)
    j = j.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
    assert page.count(DATA_TAG) == 1
    return page.replace(DATA_TAG, DATA_TAG.replace(">null<", ">" + j + "<"))


def embed_unescaped(page, snapshot):
    j = json.dumps(snapshot, ensure_ascii=False)
    return page.replace(DATA_TAG, DATA_TAG.replace(">null<", ">" + j + "<"))


class _Scripts(HTMLParser):
    """Collects every <script> as (attrs, text), the way a browser ends one at </script."""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.scripts = []
        self._open = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self._open = (dict(attrs), [])

    def handle_data(self, data):
        if self._open is not None:
            self._open[1].append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self._open is not None:
            self.scripts.append((self._open[0], "".join(self._open[1])))
            self._open = None


def scripts_of(html):
    p = _Scripts()
    p.feed(html)
    p.close()
    return p.scripts


def logic_of(html):
    m = re.search(r'<script id="logic">(.*?)</script>', html, re.S)
    if not m:
        raise AssertionError('<script id="logic"> not found')
    return m.group(1)


# ---------- node bridge ----------

_HARNESS = r"""
const M = require(process.argv[1]);
let input = "";
process.stdin.on("data", c => input += c);
process.stdin.on("end", () => {
  const p = JSON.parse(input);
  const out = {
    placed: p.cases.map(c => M.layout(c.snapshot, c.width, c.heights || undefined)),
    classes: p.states.map(s => M.stateClass(s)),
  };
  process.stdout.write(JSON.stringify(out));
});
"""


def run_node(html, cases, states=()):
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "logic.js"
        path.write_text(logic_of(html), encoding="utf-8")
        r = subprocess.run(
            [NODE, "-e", _HARNESS, str(path)],
            input=json.dumps({"cases": cases, "states": list(states)}),
            capture_output=True, text=True, timeout=30,
        )
    if r.returncode != 0:
        raise AssertionError("node failed: " + r.stderr[-2000:])
    return json.loads(r.stdout)


# ---------- a generated 40-node snapshot (max depth 3, so it fits 4 columns at 1440) ----------

def snapshot_40():
    nodes, lines = [], []

    def node(id_, kind, state, minute):
        since = None if minute is None else "2025-01-01T%02d:%02d:00+00:00" % (18 + minute // 60, minute % 60)
        nodes.append({"id": id_, "kind": kind, "title": "card " + id_, "where": kind,
                      "state": state, "since": since})

    k = 0
    for r in range(4):
        root = "p:1%d" % r
        node(root, "tern", STATES[r % 6], 10 * r)
        for o in range(2):
            tab = "p:2%d%d" % (r, o)
            node(tab, "orchestrated", STATES[(r + o + 1) % 6], 60 - 7 * o - r)
            lines.append({"from": root, "to": tab, "kind": "opened"})
            nxt = "n:0:seq-%d-%d" % (r, o)
            node(nxt, "next", "next", None)
            lines.append({"from": tab, "to": nxt, "kind": "follows"})
            helper = "a:%08d" % k
            k += 1
            node(helper, "helper", "working", 90 - o)
            lines.append({"from": tab, "to": helper, "kind": "called"})
        # one helper chain under the first tab reaches depth 3
        grandchild = "a:%08d" % k
        k += 1
        node(grandchild, "helper", "done", 95)
        lines.append({"from": "a:%08d" % (k - 3), "to": grandchild, "kind": "called"})
    while len(nodes) < 40:  # unlinked sessions, no parent
        node("p:9%02d" % len(nodes), "other", STATES[len(nodes) % 6], len(nodes))
    assert len(nodes) == 40
    return {"version": "x", "generated_at": "2025-01-01T21:00:00+00:00", "nodes": nodes,
            "lines": lines, "problems_n": 0}


def varied_heights(snapshot):
    return {n["id"]: 80 + (i * 37) % 110 for i, n in enumerate(snapshot["nodes"])}


# ---------- checkers (each one is shown to bite in CheckersBite) ----------

def overlaps(placed):
    boxes = sorted(placed["nodes"].items())
    bad = []
    for i, (a, p) in enumerate(boxes):
        for b, q in boxes[i + 1:]:
            if p["x"] < q["x"] + q["w"] and q["x"] < p["x"] + p["w"] and p["y"] < q["y"] + q["h"] and q["y"] < p["y"] + p["h"]:
                bad.append((a, b))
    return bad


def out_of_order(placed):
    """Wide: child starts right of the parent's right edge. Narrow: child starts
    below the parent's bottom and is indented past the parent's left edge."""
    bad = []
    for child, parent in placed["parent"].items():
        c, p = placed["nodes"][child], placed["nodes"][parent]
        if placed["mode"] == "columns":
            ok = c["x"] >= p["x"] + p["w"]
        else:
            ok = c["y"] >= p["y"] + p["h"] and c["x"] > p["x"]
        if not ok:
            bad.append((parent, child))
    return bad


def _blocks(css):
    """Yields (prelude, body) for each top-level block of css."""
    i, n = 0, len(css)
    while i < n:
        a = css.find("{", i)
        if a < 0:
            return
        depth, j = 1, a + 1
        while j < n and depth:
            if css[j] == "{":
                depth += 1
            elif css[j] == "}":
                depth -= 1
            j += 1
        yield css[i:a].strip(), css[a + 1:j - 1]
        i = j


def _declarations(body):
    for d in body.split(";"):
        if ":" in d:
            k, v = d.split(":", 1)
            yield k.strip().lower(), v.strip()


def forbidden_animations(css):
    """Every `animation` declaration must sit in a selector of working, be the
    300 ms fade of a new card, or be `none` inside the reduced-motion block; every
    keyframe may move only transform (the spin) or opacity (the fade)."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    bad = []

    def scan(text, media):
        for pre, body in _blocks(text):
            if pre.startswith("@media"):
                scan(body, pre)
                continue
            if pre.startswith("@keyframes"):
                for _sel, frame in _blocks(body):
                    for k, _v in _declarations(frame):
                        if k not in ("transform", "opacity"):
                            bad.append((pre, k))
                continue
            for k, v in _declarations(body):
                if not k.startswith("animation"):
                    continue
                selectors = [s.strip() for s in pre.split(",")]
                if "prefers-reduced-motion" in media:
                    ok = v == "none"
                elif all("working" in s for s in selectors):
                    ok = True
                else:
                    ok = selectors == [".node.new"] and re.fullmatch(r"fade-in 300ms \S+( both)?", v) is not None
                if not ok:
                    bad.append((pre, k + ": " + v))

    scan(css, "")
    return bad


def css_of(html):
    return "\n".join(re.findall(r"<style>(.*?)</style>", html, re.S))


def embedded_problems(html, snapshot):
    """Empty when the data script parses back to snapshot and the page keeps
    exactly its three scripts (data, logic, the unnamed runtime)."""
    bad = []
    sc = scripts_of(html)
    ids = [a.get("id") for a, _t in sc]
    if ids != ["data", "logic", None]:
        bad.append("scripts: %r" % ids)
    data = [t for a, t in sc if a.get("id") == "data"]
    if len(data) == 1:
        if "\u2028" in data[0] or "\u2029" in data[0] or "<" in data[0]:
            bad.append("raw < or U+2028/2029 inside data")
        try:
            if json.loads(data[0]) != snapshot:
                bad.append("data differ")
        except ValueError as e:
            bad.append("data not JSON: %s" % e)
    return bad


# ---------- the tests ----------

@unittest.skipUnless(NODE, "node not found on PATH; the layout tests need it")
class Layout(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = PAGE.read_text(encoding="utf-8")
        cls.example = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        cls.s40 = snapshot_40()
        cases, cls.names = [], []
        for name, s in (("example", cls.example), ("40", cls.s40)):
            for width in WIDTHS:
                for heights in (None, varied_heights(s)):
                    cases.append({"snapshot": s, "width": width, "heights": heights})
                    cls.names.append("%s @%d %s" % (name, width, "heights" if heights else "default"))
        cls.placed = run_node(cls.html, cases)["placed"]

    def test_mode_by_width(self):
        for name, d in zip(self.names, self.placed):
            expected = "columns" if "@1440" in name else "list"
            self.assertEqual(d["mode"], expected, name)

    def test_every_node_is_placed(self):
        for name, d in zip(self.names, self.placed):
            s = self.example if name.startswith("example") else self.s40
            self.assertEqual(sorted(d["nodes"]), sorted(n["id"] for n in s["nodes"]), name)

    def test_no_overlap(self):
        for name, d in zip(self.names, self.placed):
            self.assertEqual(overlaps(d), [], name)

    def test_child_after_parent(self):
        for name, d in zip(self.names, self.placed):
            self.assertTrue(d["parent"], name)
            self.assertEqual(out_of_order(d), [], name)

    def test_wide_fits_the_width(self):
        for name, d in zip(self.names, self.placed):
            if d["mode"] == "columns":
                self.assertLessEqual(d["width"], 1440, name)

    def test_one_curve_per_parent_from_exit_to_entry(self):
        for name, d in zip(self.names, self.placed):
            self.assertEqual(len(d["curves"]), len(d["parent"]), name)
            for c in d["curves"]:
                a, b = d["nodes"][c["from"]]["exit"], d["nodes"][c["to"]]["entry"]
                self.assertTrue(c["d"].startswith("M%s,%s " % (_n(a["x"]), _n(a["y"]))), (name, c))
                self.assertTrue(c["d"].endswith(" %s,%s" % (_n(b["x"]), _n(b["y"]))), (name, c))

    def test_state_class(self):
        out = run_node(self.html, [], STATES + ["invented", None])["classes"]
        self.assertEqual(out, ["state-" + s for s in STATES] + ["state-stopped", "state-stopped"])
        css = css_of(self.html)
        for s in STATES:
            self.assertIn(".state-" + s + " ", css, s)


def _n(v):
    return str(int(v)) if float(v).is_integer() else repr(float(v))


class Motion(unittest.TestCase):
    def test_animation_only_on_working(self):
        self.assertEqual(forbidden_animations(css_of(PAGE.read_text(encoding="utf-8"))), [])

    def test_reduced_motion_stops_the_spin(self):
        css = css_of(PAGE.read_text(encoding="utf-8"))
        block = [c for p, c in _blocks(css) if "prefers-reduced-motion: reduce" in p]
        self.assertEqual(len(block), 1)
        self.assertRegex(block[0], r"\.state-working \.frame::before\s*\{[^}]*animation:\s*none")


class Embedded(unittest.TestCase):
    def setUp(self):
        self.html = PAGE.read_text(encoding="utf-8")

    def test_data_exactly_once(self):
        self.assertEqual(self.html.count(DATA_TAG), 1)
        self.assertEqual(self.html.count('id="data"'), 1)

    def test_empty_page_has_the_three_scripts(self):
        self.assertEqual(embedded_problems(self.html, None), [])

    def test_hostile_title_does_not_break(self):
        s = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        s["nodes"][0]["title"] = 'a</script><script>alert(1)</script>\u2028b\u2029c <!-- d'
        self.assertEqual(embedded_problems(embed(self.html, s), s), [])

    def test_package_embed_carries_the_example(self):
        package_embed = snapshot_embed()
        if package_embed is None or "SESSION_MAP_PAGE" in os.environ:
            self.skipTest("session_map.snapshot.embed not importable yet (or the page is swapped)")
        s = json.loads(EXAMPLE.read_text(encoding="utf-8"))
        self.assertEqual(embedded_problems(package_embed(self.html, s), s), [])


def fetch_elsewhere(html):
    """Every fetch( whose first argument is not the literal "/state" or "/focus"."""
    bad = []
    for m in re.finditer(r"\bfetch\s*\(\s*([^,)]*)", html):
        if m.group(1).strip() not in ('"/state"', "'/state'", '"/focus"', "'/focus'"):
            bad.append(m.group(0))
    return bad


class Live(unittest.TestCase):
    """The live page: polling targets, the focus button, the hidden-sessions footer, reconcile."""

    def setUp(self):
        self.html = PAGE.read_text(encoding="utf-8")

    def test_fetch_only_state_and_focus(self):
        self.assertEqual(fetch_elsewhere(self.html), [])
        self.assertIn('fetch("/state"', self.html)
        self.assertIn('fetch("/focus"', self.html)

    def test_no_outside_resource(self):
        self.assertNotRegex(self.html, r"<script[^>]*\bsrc=")
        self.assertNotRegex(self.html, r"<link\b")

    def test_focus_button_sends_only_the_id_as_json(self):
        self.assertIn('"go to session"', self.html)
        m = re.search(r'fetch\("/focus",\s*\{(.*?)\n    \}\)', self.html, re.S)
        self.assertTrue(m)
        self.assertIn('method: "POST"', m.group(1))
        self.assertIn('"Content-Type": "application/json"', m.group(1))
        self.assertIn("body: JSON.stringify({ id: id })", m.group(1))

    def test_button_only_with_a_target_and_never_embedded(self):
        m = re.search(r"function canFocus\(node\) \{(.*?)\n  \}", self.html, re.S)
        self.assertTrue(m)
        self.assertIn("if (embedded", m.group(1))
        self.assertIn('node.kind === "tern" && typeof node.pane === "number"', m.group(1))
        self.assertIn('node.kind === "orchestrated"', m.group(1))
        # No orchestrator is hard-coded: an orchestrated card shows its node's `where`.
        self.assertNotIn("orca", self.html.lower())

    def test_hidden_sessions_footer(self):
        self.assertIn("snapshot.hidden_n", self.html)
        self.assertIn("sessions stopped for more than 8 h hidden", self.html)

    def test_reconcile_by_id(self):
        m = re.search(r"function draw\(snap\) \{(.*?)\n  \}", self.html, re.S)
        self.assertTrue(m)
        self.assertIn("c.dataset.signature !== signature", m.group(1))
        self.assertIn("cards[node.id]", m.group(1))

    def test_304_does_not_draw(self):
        m = re.search(r"if \(r\.status === 304\) \{([^}]*)\}", self.html)
        self.assertTrue(m)
        self.assertNotIn("draw", m.group(1))

    def test_server_off_says_how_to_open(self):
        """A dead server after a pause gets plain words, not 'trying again' forever."""
        self.assertIn(
            "The map was turned off after 10 minutes without use. To open it again run: session-map open",
            self.html)
        m = re.search(r"function poll\(\) \{(.*?)\n  \}", self.html, re.S)
        self.assertTrue(m)
        self.assertIn("turnedOff()", m.group(1))

    def test_failure_backs_off(self):
        """Failures back off: the next poll waits longer each time, capped, never a tight 2 s loop."""
        m = re.search(r"function nextWait\(\) \{(.*?)\n  \}", self.html, re.S)
        self.assertTrue(m)
        self.assertIn("failures", m.group(1))
        self.assertIn("BACKOFF_MAX_MS", m.group(1))
        self.assertRegex(self.html, r"setTimeout\(poll, nextWait\(\)\)")
        self.assertNotRegex(self.html, r"setTimeout\(poll, POLL_MS\)")

    def test_unknown_format_footer(self):
        self.assertIn("snapshot.unknown_format", self.html)
        self.assertIn("session registry not in the known format", self.html)

    def test_since_label_not_opened_at(self):
        """`since` is the last state change, not when the session opened."""
        self.assertNotIn("opened at", self.html)
        self.assertNotRegex(self.html, r'row\(dl, "opened"')
        self.assertRegex(self.html, r'row\(dl, "since"')

    def test_fetch_checker_bites(self):
        for bad in ('fetch("https://example.com/x")', "fetch(url)", 'fetch("/state2")', "fetch('/../snapshot.py')"):
            self.assertTrue(fetch_elsewhere(bad), bad)
        self.assertEqual(fetch_elsewhere('fetch("/state", {}); fetch("/focus", {})'), [])


@unittest.skipUnless(NODE, "node not found on PATH; the layout tests need it")
class CheckersBite(unittest.TestCase):
    """Each checker flags a planted bad input, so a green run is not a blind one."""

    def test_overlap_bites(self):
        d = {"nodes": {"a": {"x": 0, "y": 0, "w": 100, "h": 50}, "b": {"x": 90, "y": 40, "w": 100, "h": 50}}}
        self.assertEqual(overlaps(d), [("a", "b")])
        d["nodes"]["b"]["x"] = 100
        self.assertEqual(overlaps(d), [])

    def test_order_bites(self):
        box = lambda x, y: {"x": x, "y": y, "w": 100, "h": 50}
        wide = {"mode": "columns", "parent": {"b": "a"}, "nodes": {"a": box(200, 0), "b": box(250, 0)}}
        self.assertEqual(out_of_order(wide), [("a", "b")])
        narrow = {"mode": "list", "parent": {"b": "a"}, "nodes": {"a": box(24, 0), "b": box(24, 70)}}
        self.assertEqual(out_of_order(narrow), [("a", "b")])
        narrow["nodes"]["b"]["x"] = 52
        self.assertEqual(out_of_order(narrow), [])

    def test_animation_bites(self):
        bad = [
            ".state-waiting .frame { animation: spin 3s linear infinite; }",
            ".state-working .x, .state-waiting .x { animation: spin 3s; }",
            ".node.new { animation: fade-in 900ms ease-out both; }",
            "@media (prefers-reduced-motion: reduce) { .x { animation: spin 3s; } }",
            "@keyframes spin { to { width: 10px; } }",
            ".head { animation-name: spin; }",
        ]
        for css in bad:
            self.assertTrue(forbidden_animations(css), css)

    def test_embedded_bites(self):
        html = PAGE.read_text(encoding="utf-8")
        s = {"nodes": [{"title": "a</script><script>x()</script>\u2028"}]}
        self.assertTrue(embedded_problems(embed_unescaped(html, s), s))
        self.assertTrue(embedded_problems(html.replace(DATA_TAG, DATA_TAG * 2), None))

    def test_class_bites_on_a_swapped_state(self):
        bad = PAGE.read_text(encoding="utf-8").replace('"waiting", "done"', '"done"')
        out = run_node(bad, [], ["waiting"])["classes"]
        self.assertNotEqual(out, ["state-waiting"])


if __name__ == "__main__":
    unittest.main()
