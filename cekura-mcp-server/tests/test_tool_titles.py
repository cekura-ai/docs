"""Tests for human-readable tool titles (MCP `Tool.title`).

Directory reviewers (e.g. the Anthropic Connectors Directory) require every
tool to carry a human-readable title, so the coverage test walks every
exposed operation in the live `openapi.json`.

Run manually: pytest tests/test_tool_titles.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import tool_generator
from openapi_parser import Operation, load_openapi_spec
from tool_generator import (
    generate_tool_name,
    generate_tool_title,
    load_tool_overlays,
    should_include_operation,
)


def _operation(summary):
    return Operation(
        path="/test_framework/v2/scenarios/",
        method="GET",
        operation_id="scenarios-list",
        summary=summary,
        description=None,
        parameters=[],
        request_body=None,
        responses={},
        tags=[],
    )


class TestGenerateToolTitle:
    """Unit cases for the name → title derivation."""

    @pytest.mark.parametrize(
        "name,title",
        [
            ("scenarios_list", "List Scenarios"),
            ("scenarios_create", "Create Scenarios"),
            ("scenarios_retrieve", "Get Scenarios"),
            ("scenarios_partial_update", "Update Scenarios"),
            ("scenarios_destroy", "Delete Scenarios"),
            ("delete_runs", "Delete Runs"),
            ("end_calls", "End Calls"),
        ],
    )
    def test_crud_phrasing(self, name, title):
        assert generate_tool_title(name) == title

    @pytest.mark.parametrize(
        "name,title",
        [
            ("scenarios_generate_progress", "Get Scenarios Generate Progress"),
            (
                "metrics_simplify_prompt_progress_retrieve",
                "Get Metrics Simplify Prompt Progress",
            ),
        ],
    )
    def test_progress_phrasing(self, name, title):
        assert generate_tool_title(name) == title

    @pytest.mark.parametrize(
        "name,title",
        [
            # The trailing REST `_create` yields to the specific verb.
            ("scenarios_duplicate_create", "Duplicate Scenarios"),
            ("predefined_metrics_copy_create", "Copy Predefined Metrics"),
            ("results_rerun_create", "Rerun Results"),
            (
                "deep_research_insights_generate_create",
                "Generate Deep Research Insights",
            ),
        ],
    )
    def test_inner_verb_wins_over_rest_suffix(self, name, title):
        assert generate_tool_title(name) == title

    def test_verb_after_from_is_a_noun(self):
        assert generate_tool_title("test_sets_create_from_run") == (
            "Create Test Sets (From Run)"
        )

    @pytest.mark.parametrize(
        "name,title",
        [
            ("scenarios_bulk_update", "Bulk Update Scenarios"),
            ("metrics_bulk_create", "Bulk Create Metrics"),
            ("runs_bulk_retrieve", "Bulk Get Runs"),
        ],
    )
    def test_bulk_prefix(self, name, title):
        assert generate_tool_title(name) == title

    @pytest.mark.parametrize(
        "name,title",
        [
            ("aiagents_list", "List AI Agents"),
            ("scenarios_run_vapi_webrtc", "Run Scenarios (VAPI WebRTC)"),
            ("scenarios_run_livekit_v2", "Run Scenarios (LiveKit v2)"),
            ("observability_v2_call_logs_list", "List Observability v2 Call Logs"),
        ],
    )
    def test_domain_word_map(self, name, title):
        assert generate_tool_title(name) == title

    def test_adjacent_duplicate_tokens_collapse(self):
        assert generate_tool_title("slack_slack_workspaces_list") == (
            "List Slack Workspaces"
        )

    def test_overlay_title_overrides_derivation(self, monkeypatch):
        # No live mcp_tools.json entry carries `title` anymore — the backend
        # now guarantees a summary for every real tool, so the bridge these
        # overlays existed for has already closed. Inject a synthetic entry
        # to exercise the overlay branch itself.
        monkeypatch.setattr(
            tool_generator, "_OVERLAY_CACHE",
            {"scenarios_json_schema": {"title": "Synthetic Overlay Title"}},
        )
        assert generate_tool_title("scenarios_json_schema") == (
            "Synthetic Overlay Title"
        )

    def test_operation_summary_beats_derivation(self):
        # The backend guarantees a summary on every exposed operation; it is
        # the preferred title source.
        op = _operation("List scenarios in a project")
        assert generate_tool_title("scenarios_list", op) == (
            "List scenarios in a project"
        )

    def test_blank_summary_falls_back_to_derivation(self):
        assert generate_tool_title("scenarios_list", _operation("  ")) == (
            "List Scenarios"
        )

    def test_operation_summary_beats_overlay_title(self, monkeypatch):
        # The backend spec is authoritative; overlay titles are only a bridge
        # for specs that predate the summary guarantee.
        monkeypatch.setattr(
            tool_generator, "_OVERLAY_CACHE",
            {"scenarios_json_schema": {"title": "Synthetic Overlay Title"}},
        )
        op = _operation("Real backend summary")
        assert generate_tool_title("scenarios_json_schema", op) == (
            "Real backend summary"
        )

    def test_no_verb_falls_back_to_title_case(self):
        assert generate_tool_title("scenarios_json_schema") == (
            "Scenarios JSON Schema"
        )


class TestTitleCoverage:
    """Every exposed tool must present a human-readable title."""

    @pytest.fixture(scope="class")
    def generated_tool_names(self):
        parser = load_openapi_spec(str(ROOT.parent / "openapi.json"))
        return sorted(
            generate_tool_name(op)
            for op in parser.extract_operations()
            if should_include_operation(op)
        )

    def test_every_generated_tool_has_a_title(self, generated_tool_names):
        assert generated_tool_names, "no tools generated from openapi.json"
        missing = [
            name
            for name in generated_tool_names
            if not generate_tool_title(name).strip()
        ]
        assert not missing, f"tools without a title: {missing}"

    def test_titles_are_short_and_human_readable(self, generated_tool_names):
        offenders = {
            name: title
            for name in generated_tool_names
            if (title := generate_tool_title(name)) == name
            or len(title) > 80
            or "_" in title
        }
        assert not offenders, f"unreadable titles: {offenders}"

    def test_overlay_titles_key_to_real_tools(self, generated_tool_names):
        known = set(generated_tool_names)
        stale = [
            name
            for name, overlay in load_tool_overlays().items()
            if overlay.get("title") and name not in known
        ]
        assert not stale, f"overlay titles for unknown tools: {stale}"
