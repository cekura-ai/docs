"""Tests for retired-tool-name aliases (`alias_of` overlay entries).

Tool names are derived from spec operation ids, so renaming an operation
renames its MCP tool and breaks callers that hardcoded the old name. An alias
keeps the old name resolving to the new tool.
"""
import pytest

from openapi_mcp_server import operations_registry, register_tool, register_tool_aliases
from tool_generator import load_tool_aliases


class FakeOp:
    """Stand-in for an Operation — aliases only ever pass it through."""

    def __init__(self, path="/api/v1/thing/"):
        self.path = path
        self.method = "POST"


@pytest.fixture
def clean_registry():
    """Swap in an empty registry so tests never disturb real registrations."""
    saved = dict(operations_registry)
    operations_registry.clear()
    yield operations_registry
    operations_registry.clear()
    operations_registry.update(saved)


def _overlays(monkeypatch, overlays):
    monkeypatch.setattr("tool_generator.load_tool_overlays", lambda: overlays)


class TestLoadToolAliases:
    """Only entries carrying `alias_of` are aliases."""

    def test_extracts_alias_entries(self, monkeypatch):
        _overlays(monkeypatch, {
            "old_name": {"alias_of": "new_name"},
            "unrelated_tool": {"description_suffix": "hi"},
        })
        assert load_tool_aliases() == {"old_name": "new_name"}

    def test_no_aliases_when_none_declared(self, monkeypatch):
        _overlays(monkeypatch, {"some_tool": {"destructive": True}})
        assert load_tool_aliases() == {}

    def test_ignores_non_dict_entries(self, monkeypatch):
        _overlays(monkeypatch, {"weird": "not-an-object"})
        assert load_tool_aliases() == {}


class TestRegisterToolAliases:
    """Registration copies the target's operation, schema, and annotations."""

    def test_alias_resolves_to_target_operation(self, monkeypatch, clean_registry):
        op = FakeOp()
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        register_tool("new_name", "Target description.", schema, op, annotations="ann")
        _overlays(monkeypatch, {"old_name": {"alias_of": "new_name"}})

        assert register_tool_aliases(set()) == ["old_name"]
        alias = operations_registry["old_name"]
        assert alias["operation"] is op
        assert alias["schema"] is schema
        assert alias["annotations"] == "ann"

    def test_alias_description_carries_its_own_prefix(self, monkeypatch, clean_registry):
        register_tool("new_name", "Target description.", {}, FakeOp())
        _overlays(monkeypatch, {
            "old_name": {"alias_of": "new_name", "description_prefix": "RETIRED NAME."},
        })

        register_tool_aliases(set())
        description = operations_registry["old_name"]["description"]
        assert description.startswith("RETIRED NAME.")
        assert "Target description." in description

    def test_missing_target_is_skipped_not_fatal(self, monkeypatch, clean_registry):
        # An overlay may declare the alias before the rename that creates its
        # target reaches the spec.
        _overlays(monkeypatch, {"old_name": {"alias_of": "not_registered_yet"}})

        assert register_tool_aliases(set()) == []
        assert "old_name" not in operations_registry

    def test_alias_never_shadows_a_real_tool(self, monkeypatch, clean_registry):
        register_tool("new_name", "New.", {}, FakeOp())
        register_tool("old_name", "A real tool that still exists.", {}, FakeOp())
        _overlays(monkeypatch, {"old_name": {"alias_of": "new_name"}})

        assert register_tool_aliases(set()) == []
        assert operations_registry["old_name"]["description"] == "A real tool that still exists."

    def test_blocked_alias_is_not_registered(self, monkeypatch, clean_registry):
        register_tool("new_name", "New.", {}, FakeOp())
        _overlays(monkeypatch, {"old_name": {"alias_of": "new_name"}})

        assert register_tool_aliases({"old_name"}) == []
        assert "old_name" not in operations_registry

    def test_alias_is_dispatchable_like_any_tool(self, monkeypatch, clean_registry):
        # The dispatch layer looks tools up in this registry by name only, so an
        # alias entry is indistinguishable from a directly registered tool.
        op = FakeOp()
        register_tool("new_name", "New.", {"type": "object"}, op)
        _overlays(monkeypatch, {"old_name": {"alias_of": "new_name"}})
        register_tool_aliases(set())

        assert operations_registry["old_name"]["operation"] is operations_registry["new_name"]["operation"]
