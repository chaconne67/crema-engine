"""The memory tool had no way to edit part of an entry: changing one clause of a packed multi-fact
entry could only be written as a whole-entry ``replace``, which silently dropped the sibling facts.
``patch`` replaces ``old_text`` with the new text inside the matched entry and keeps the rest."""

import json

import pytest

from tools.memory_tool import memory_tool
from tools.memory_tool_store import MemoryStore

PACKED = "todo: item A; item B; item C"


@pytest.fixture
def packed_store(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    s = MemoryStore(memory_char_limit=500, user_char_limit=300)
    s.add("memory", PACKED)
    return s


class TestPatchSingleOp:
    def test_patch_changes_only_the_matched_span_and_reports_before_and_after(self, packed_store):
        result = json.loads(memory_tool(
            action="patch", old_text="item B", new_text="item B done", store=packed_store))
        assert result["success"] is True
        assert packed_store._entries_for("memory") == ["todo: item A; item B done; item C"]
        assert result["patched_entry_before"] == PACKED
        assert result["patched_entry_after"] == "todo: item A; item B done; item C"

    def test_patch_requires_old_text(self, packed_store):
        result = json.loads(memory_tool(action="patch", new_text="x", store=packed_store))
        assert result["success"] is False
        assert "old_text" in result["error"]

    def test_patch_requires_new_text(self, packed_store):
        result = json.loads(memory_tool(action="patch", old_text="item B", store=packed_store))
        assert result["success"] is False
        # Deleting the whole entry would drop the sibling facts; point at a longer span instead.
        assert "patch a longer span" in result["error"]

    def test_patch_unmatched_old_text_fails_without_write(self, packed_store):
        result = json.loads(memory_tool(
            action="patch", old_text="item Z", new_text="whatever", store=packed_store))
        assert result["success"] is False
        assert packed_store._entries_for("memory") == [PACKED]

    def test_patch_multi_entry_match_is_rejected(self, packed_store):
        packed_store.add("memory", "also mentions item B elsewhere")
        result = json.loads(memory_tool(
            action="patch", old_text="item B", new_text="x", store=packed_store))
        assert result["success"] is False
        assert packed_store._entries_for("memory") == [PACKED, "also mentions item B elsewhere"]

    def test_patch_of_the_whole_entry_rewrites_it(self, packed_store):
        result = json.loads(memory_tool(action="patch", old_text=PACKED, content="todo: nothing", store=packed_store))
        assert result["success"] is True
        assert packed_store._entries_for("memory") == ["todo: nothing"]


class TestPatchStoreLevel:
    def test_patch_rejects_a_span_repeated_within_the_entry(self, tmp_path, monkeypatch):
        # Replacing every occurrence rewrote sibling facts that shared the span.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
        s = MemoryStore(memory_char_limit=500)
        entry = "Jeff drinks tea in the morning; his wife drinks tea at night"
        s.add("memory", entry)
        single = s.patch("memory", "tea", "coffee")
        batch = s.apply_batch("memory", [{"action": "patch", "old_text": "tea", "content": "coffee"}])
        for result in (single, batch):
            assert result["success"] is False
            assert "appears 2 times" in result["error"]
        assert s._entries_for("memory") == [entry]
        assert s.patch("memory", "drinks tea in the morning", "drinks coffee in the morning")["success"] is True
        assert s._entries_for("memory") == ["Jeff drinks coffee in the morning; his wife drinks tea at night"]


class TestPatchBatch:
    def test_patch_op_in_batch(self, packed_store):
        result = packed_store.apply_batch("memory", [
            {"action": "patch", "old_text": "item B", "new_text": "item B done"},
        ])
        assert result["success"] is True
        assert packed_store._entries_for("memory") == ["todo: item A; item B done; item C"]
        assert result["patched_entries"] == {1: PACKED}
        assert result["patched_entries_after"] == {1: "todo: item A; item B done; item C"}

    def test_patch_op_without_content_points_at_a_longer_span(self, packed_store):
        result = packed_store.apply_batch("memory", [{"action": "patch", "old_text": "item B", "content": ""}])
        assert result["success"] is False
        assert "patch a longer span" in result["error"]
        assert packed_store._entries_for("memory") == [PACKED]
