"""Tests for the Codex marks on page.html: the agent tag on a card, the
`Codex app` kind, and the panel note of an `inferred` wait.

Run from the repository root:
  python3 -m unittest discover -s tests -p "test_page_codex.py"

The page's runtime script talks to the DOM, so node runs it here against a
small fake DOM, with the snapshot embedded the same way the package embeds it
(test_page.embed). Clicking a card opens the panel; the test reads back what
the page drew. SESSION_MAP_PAGE swaps the page under test, as in test_page.
"""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.test_page import NODE, PAGE, css_of, embed, logic_of, scripts_of

PERMISSION_NOTE = (
    "Waiting on a permission, inferred: a command that needs your approval has no result yet. "
    "Codex does not write approval requests to disk, so trust this a little less."
)
GUESSED_NOTE = (
    "Color guessed from the last sentence: no sure signal gave the state, so the color comes "
    "from how the last message ends. Trust it a little less."
)

SNAPSHOT = {
    "version": "c0dex",
    "generated_at": "2025-01-01T21:12:00+00:00",
    "nodes": [
        {"id": "p:48213", "kind": "tern", "title": "api · review", "where": "Tern · tab 1",
         "state": "waiting", "since": "2025-01-01T21:09:41+00:00", "pane": 3,
         "waiting_for": "permission request"},
        {"id": "p:39102", "kind": "background", "title": "nightly report", "where": "in the background",
         "state": "done", "since": "2025-01-01T20:31:15+00:00", "guessed": True},
        {"id": "x:0123456789ab", "agent": "codex", "kind": "app", "title": "landing copy",
         "where": "Codex app", "state": "working", "since": "2025-01-01T21:05:00+00:00"},
        {"id": "x:abcdef012345", "agent": "codex", "kind": "tern", "pane": 4, "title": "migrate db",
         "where": "Codex · Tern · tab 3", "state": "waiting", "since": "2025-01-01T21:10:00+00:00",
         "waiting_for": "permission request", "inferred": True},
        {"id": "x:fedcba987654", "agent": "codex", "kind": "other", "title": "fix tests",
         "where": "Codex · other terminal", "state": "done", "since": "2025-01-01T20:50:00+00:00",
         "guessed": True},
    ],
    "lines": [],
    "problems_n": 0,
    "hidden_n": 0,
}

_FAKE_DOM = r"""
const fs = require("fs");
const html = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
function E(tag, text) {
  this.tagName = tag; this.children = []; this.parent = null; this.dataset = {}; this.style = {};
  this.attrs = {}; this.listeners = {}; this.className = ""; this.hidden = false; this.own = text || "";
  this.offsetHeight = 100; const self = this;
  const has = c => self.className.split(/\s+/).indexOf(c) >= 0;
  this.classList = {
    contains: has,
    add: c => { if (!has(c)) self.className = (self.className + " " + c).trim(); },
    remove: c => { self.className = self.className.split(/\s+/).filter(x => x !== c).join(" "); },
  };
}
Object.defineProperty(E.prototype, "textContent", {
  get() { return this.own + this.children.map(c => c.textContent).join(""); },
  set(v) { this.children = []; this.own = String(v); },
});
E.prototype.appendChild = function (c) { c.parent = this; this.children.push(c); return c; };
E.prototype.remove = function () { if (this.parent) this.parent.children.splice(this.parent.children.indexOf(this), 1); };
E.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
E.prototype.addEventListener = function (t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); };
E.prototype.contains = function (x) { for (; x; x = x.parent) if (x === this) return true; return false; };
E.prototype.closest = function (sel) {
  for (let x = this; x; x = x.parent) if (x.classList.contains(sel.slice(1))) return x;
  return null;
};
E.prototype.all = function (cls) {
  let out = this.classList.contains(cls) ? [this] : [];
  this.children.forEach(c => { out = out.concat(c.all(cls)); });
  return out;
};
E.prototype.querySelector = function (sel) { return this.all(sel.slice(1))[0] || null; };
const byId = {};
["map", "curves", "ports", "empty", "panel", "footer", "data"].forEach(id => { byId[id] = new E("div"); });
byId.data.own = html.data;
global.document = {
  hidden: false, documentElement: { clientWidth: Number(process.argv[2]) },
  getElementById: id => byId[id], createElement: t => new E(t), createElementNS: (_n, t) => new E(t),
  createTextNode: t => new E("#text", t), addEventListener: () => {},
};
global.window = { addEventListener: () => {} };
global.getComputedStyle = () => ({ getPropertyValue: () => "6" });
global.fetch = () => new Promise(() => {});
eval(html.logic + "\n" + html.runtime);
const map = byId.map, panel = byId.panel, out = {};
map.children.filter(c => c.dataset.id).forEach(card => {
  const head = card.querySelector(".head"), badge = card.querySelector(".badge");
  map.listeners.click.forEach(f => f({ target: head, stopPropagation: () => {} }));
  out[card.dataset.id] = {
    head: head.textContent,
    tags: card.all("agent").map(t => t.textContent),
    label: card.attrs["aria-label"],
    badge: badge ? badge.title || null : null,
    kind: panel.querySelector(".kind").textContent,
    notes: panel.all("note").map(n => n.textContent),
    buttons: panel.all("button").map(b => b.textContent),
  };
});
process.stdout.write(JSON.stringify(out));
"""


def drawn(html, snapshot, width=960):
    """What the page draws for snapshot: per card id, its head, tags, badge and opened panel."""
    page = embed(html, snapshot)
    runtime = [t for a, t in scripts_of(page) if a.get("id") is None]
    data = [t for a, t in scripts_of(page) if a.get("id") == "data"]
    payload = {"logic": logic_of(page), "runtime": runtime[0], "data": data[0]}
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "page.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        r = subprocess.run([NODE, "-e", _FAKE_DOM, str(path), str(width)],
                           capture_output=True, text=True, timeout=30)
    if r.returncode != 0:
        raise AssertionError("node failed: " + r.stderr[-2000:])
    return json.loads(r.stdout)


@unittest.skipUnless(NODE, "node not found on PATH; the page tests need it")
class CodexMarks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = PAGE.read_text(encoding="utf-8")
        cls.out = drawn(cls.html, SNAPSHOT)

    def test_every_card_drawn_and_opened(self):
        self.assertEqual(sorted(self.out), sorted(n["id"] for n in SNAPSHOT["nodes"]))

    def test_codex_cards_carry_the_tag(self):
        for id_ in ("x:0123456789ab", "x:abcdef012345", "x:fedcba987654"):
            self.assertEqual(self.out[id_]["tags"], ["Codex"], id_)
            self.assertTrue(self.out[id_]["label"].startswith("Codex session "), id_)

    def test_claude_cards_have_no_tag_and_keep_their_head(self):
        for n in SNAPSHOT["nodes"]:
            if "agent" in n:
                continue
            self.assertEqual(self.out[n["id"]]["tags"], [], n["id"])
            self.assertEqual(self.out[n["id"]]["head"], n["title"], n["id"])
            self.assertEqual(self.out[n["id"]]["label"].split(",")[0], n["title"], n["id"])

    def test_tag_sits_before_the_title(self):
        self.assertEqual(self.out["x:0123456789ab"]["head"], "Codexlanding copy")

    def test_app_kind_label(self):
        self.assertEqual(self.out["x:0123456789ab"]["kind"], "Codex app")
        self.assertEqual(self.out["x:abcdef012345"]["kind"], "Tern tab")

    def test_inferred_note_and_mark(self):
        o = self.out["x:abcdef012345"]
        self.assertEqual(o["notes"], [PERMISSION_NOTE])
        self.assertIn("inferred", o["badge"])

    def test_guessed_note_untouched(self):
        for id_ in ("p:39102", "x:fedcba987654"):
            self.assertEqual(self.out[id_]["notes"], [GUESSED_NOTE], id_)
            self.assertEqual(self.out[id_]["badge"], "color guessed from the last sentence", id_)

    def test_no_note_without_a_mark(self):
        for id_ in ("p:48213", "x:0123456789ab"):
            self.assertEqual(self.out[id_]["notes"], [], id_)
            self.assertIsNone(self.out[id_]["badge"], id_)

    def test_inferred_question_says_question_not_permission(self):
        s = json.loads(json.dumps(SNAPSHOT))
        s["nodes"][3]["waiting_for"] = "question or form"
        notes = drawn(self.html, s)["x:abcdef012345"]["notes"]
        self.assertEqual(len(notes), 1)
        self.assertTrue(notes[0].startswith("Waiting on your answer, inferred:"), notes[0])
        self.assertNotIn("permission", notes[0])

    def test_embedded_page_offers_no_jump(self):
        for o in self.out.values():
            self.assertEqual(o["buttons"], ["close"])

    def test_narrow_width_draws_the_same_marks(self):
        out = drawn(self.html, SNAPSHOT, width=390)
        self.assertEqual(out["x:abcdef012345"]["tags"], ["Codex"])
        self.assertEqual(out["x:abcdef012345"]["notes"], [PERMISSION_NOTE])


class CodexStatic(unittest.TestCase):
    def setUp(self):
        self.html = PAGE.read_text(encoding="utf-8")

    def test_codex_cards_never_focusable(self):
        """canFocus only takes a Claude pid id; a Codex `x:` id never gets the jump button."""
        self.assertIn('if (embedded || !/^p:\\d+$/.test(node.id)) return false;', self.html)

    def test_tag_colors_only_from_tokens(self):
        css = css_of(self.html)
        start = css.index(".agent {")
        body = css[start:css.index("}", start)]
        for prop in ("color", "background", "border"):
            decl = [d for d in body.split(";") if d.strip().startswith(prop + ":")]
            self.assertEqual(len(decl), 1, prop)
            self.assertIn("var(--", decl[0], prop)
            self.assertNotRegex(decl[0], r"#[0-9a-fA-F]{3}|rgba?\(", prop)


if __name__ == "__main__":
    unittest.main()
