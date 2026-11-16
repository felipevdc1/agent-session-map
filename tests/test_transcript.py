from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from session_map import transcript as t  # noqa: E402


def user(content, **extra):
    rec = {"type": "user", "message": {"role": "user", "content": content}}
    rec.update(extra)
    return rec


def assistant(content, **extra):
    rec = {"type": "assistant", "message": {"role": "assistant", "content": content}}
    rec.update(extra)
    return rec


def tool_result(content):
    return {"type": "tool_result", "tool_use_id": "toolu_1", "content": content}


def queued(prompt):
    return {"type": "attachment", "attachment": {"type": "queued_command", "prompt": prompt}}


def notice(task_id):
    return f"<task-notification>\n<task-id>{task_id}</task-id>\n<status>completed</status></task-notification>"


class BlocksAndText(unittest.TestCase):
    def test_string_content_is_one_text_block(self):
        self.assertEqual(t.blocks(user("hi")), [{"type": "text", "text": "hi"}])
        self.assertEqual(t.text(user("hi")), "hi")

    def test_list_content_is_kept_and_text_joins_text_blocks_only(self):
        content = [{"type": "text", "text": "a"}, {"type": "tool_use", "name": "Bash"}, "junk",
                   {"type": "text", "text": "b"}, {"type": "text"}]
        rec = assistant(content)
        self.assertIs(t.blocks(rec), content)
        self.assertEqual(t.text(rec), "a\nb\n")

    def test_missing_or_odd_message_reads_as_nothing(self):
        self.assertEqual(t.blocks({"type": "user"}), [])
        self.assertEqual(t.blocks({"message": "x"}), [])
        self.assertEqual(t.blocks({"message": {"content": 7}}), [])
        self.assertEqual(t.text({"message": {"content": None}}), "")


class Compaction(unittest.TestCase):
    def test_idle_compaction_mark(self):
        self.assertTrue(t.is_idle_compaction_mark(
            {"type": "system", "subtype": "informational", "content": "Compacted while idle (summary kept)"}))
        self.assertFalse(t.is_idle_compaction_mark(
            {"type": "system", "subtype": "informational", "content": "Something else"}))
        self.assertFalse(t.is_idle_compaction_mark(
            {"type": "system", "subtype": "compact_boundary", "content": "Compacted while idle"}))
        self.assertFalse(t.is_idle_compaction_mark({"type": "system", "subtype": "informational"}))

    def test_compaction_block(self):
        self.assertTrue(t.is_compaction_block(user("summary", isCompactSummary=True)))
        self.assertTrue(t.is_compaction_block({"type": "system", "subtype": "compact_boundary"}))
        self.assertFalse(t.is_compaction_block(user("hi")))
        self.assertFalse(t.is_compaction_block(assistant("x", isCompactSummary=True)))


class ToolResultTexts(unittest.TestCase):
    def test_shapes(self):
        self.assertEqual(t.tool_result_texts(tool_result("out")), ["out"])
        self.assertEqual(t.tool_result_texts(tool_result(
            [{"type": "text", "text": "a"}, {"type": "image"}, {"type": "text"}, 3])), ["a", ""])
        self.assertEqual(t.tool_result_texts(tool_result(None)), [])
        self.assertEqual(t.tool_result_texts({"type": "tool_result"}), [])


class ParseIso(unittest.TestCase):
    def test_z_suffix(self):
        self.assertEqual(t.parse_iso("2024-01-02T03:04:05Z"),
                         dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc))

    def test_z_with_milliseconds(self):
        self.assertEqual(t.parse_iso("2024-01-02T03:04:05.123Z"),
                         dt.datetime(2024, 1, 2, 3, 4, 5, 123000, tzinfo=dt.timezone.utc))

    def test_offset_is_kept(self):
        got = t.parse_iso("2024-01-02T08:34:05+05:30")
        self.assertEqual(got.utcoffset(), dt.timedelta(hours=5, minutes=30))
        self.assertEqual(got, dt.datetime(2024, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc))

    def test_naive_is_utc(self):
        self.assertEqual(t.parse_iso("2024-01-02T03:04:05").tzinfo, dt.timezone.utc)

    def test_bad_input(self):
        for bad in (None, 5, "", "yesterday", "2024-13-01T00:00:00Z"):
            self.assertIsNone(t.parse_iso(bad), bad)


class EncodeCwd(unittest.TestCase):
    def test_every_non_alphanumeric_becomes_dash(self):
        self.assertEqual(t.encode_cwd("/home/me/my_proj.v2"), "-home-me-my-proj-v2")
        self.assertEqual(t.encode_cwd(Path("/a b/c")), "-a-b-c")


class AcceptList(unittest.TestCase):
    def test_accepted(self):
        self.assertTrue(t.accepted(user("x")))
        self.assertTrue(t.accepted(assistant("x")))
        self.assertTrue(t.accepted({"type": "system", "subtype": "turn_duration"}))
        self.assertFalse(t.accepted({"type": "system", "subtype": "informational"}))
        self.assertFalse(t.accepted({"type": "attachment"}))
        self.assertFalse(t.accepted(assistant("x", isSidechain=True)))

    def test_has_text(self):
        self.assertTrue(t.has_text(assistant("hello")))
        self.assertTrue(t.has_text(assistant([{"type": "tool_use"}, {"type": "text", "text": ""}])))
        self.assertFalse(t.has_text(assistant([{"type": "tool_use"}])))
        self.assertFalse(t.has_text(assistant("hello", isSidechain=True)))
        self.assertFalse(t.has_text(user("hello")))

    def test_is_turn_end_with_or_without_pending_helpers(self):
        self.assertTrue(t.is_turn_end({"type": "system", "subtype": "turn_duration"}))
        self.assertTrue(t.is_turn_end({"type": "system", "subtype": "turn_duration",
                                       "pendingBackgroundAgentCount": 2}))
        self.assertFalse(t.is_turn_end({"type": "system", "subtype": "informational"}))
        self.assertFalse(t.is_turn_end(None))
        self.assertFalse(t.is_turn_end("turn_duration"))


class BackgroundIds(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dir = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def write(self, records, name="s.jsonl"):
        p = self.dir / name
        lines = [r if isinstance(r, str) else json.dumps(r) for r in records]
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return p

    def test_started_with_both_wordings(self):
        p = self.write([
            user([tool_result("Command running in background with ID: bg_1. Output is being written")]),
            user([tool_result([{"type": "text", "text": "Command was moved to the background (ID: bg-2)."}])]),
        ])
        self.assertEqual(t.background_ids(p), {"bg_1", "bg-2"})

    def test_returned_via_user_text(self):
        p = self.write([
            user([tool_result("running in background with ID: a1")]),
            user([tool_result("running in background with ID: a2")]),
            user(notice("a1")),
        ])
        self.assertEqual(t.background_ids(str(p)), {"a2"})

    def test_returned_via_queued_command(self):
        p = self.write([
            user([tool_result("running in background with ID: q1")]),
            queued(notice("q1")),
        ])
        self.assertEqual(t.background_ids(p), set())

    def test_sidechain_is_skipped(self):
        p = self.write([
            user([tool_result("running in background with ID: side")], isSidechain=True),
            user([tool_result("running in background with ID: main")]),
            user(notice("main"), isSidechain=True),
        ])
        self.assertEqual(t.background_ids(p), {"main"})

    def test_notice_in_a_tool_result_does_not_count_as_returned(self):
        p = self.write([
            user([tool_result("running in background with ID: x1")]),
            user([tool_result(notice("x1"))]),
            assistant(notice("x1")),
        ])
        self.assertEqual(t.background_ids(p), {"x1"})

    def test_bad_lines_are_skipped(self):
        p = self.write(["", "not json", "[1, 2]", json.dumps(user([tool_result("running in background with ID: ok")]))])
        self.assertEqual(t.background_ids(p), {"ok"})

    def test_unreadable_file_is_empty(self):
        self.assertEqual(t.background_ids(self.dir / "missing.jsonl"), set())
        self.assertEqual(t.background_ids(self.dir), set())
        bad = self.dir / "bad.jsonl"
        bad.write_bytes(b"\xff\xfe\x00 not utf-8")
        self.assertEqual(t.background_ids(bad), set())

    @unittest.skipIf(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0), "needs file modes")
    def test_permission_denied_is_empty(self):
        p = self.write([user([tool_result("running in background with ID: z")])])
        p.chmod(0)
        try:
            self.assertEqual(t.background_ids(p), set())
        finally:
            p.chmod(0o600)


if __name__ == "__main__":
    unittest.main()
