"""Tests for tool generator module"""
import pytest
from tool_generator import (
    generate_tool_name,
    generate_tool_description,
    should_include_operation,
    maybe_append_org_project_hint,
    ORG_PROJECT_HINT,
)
from openapi_parser import Operation


class TestGenerateToolName:
    """Test suite for tool name generation"""

    def test_generate_tool_name_with_operation_id(self):
        """Test generating tool name from operation_id"""
        operation = Operation(
            path="/api/v1/test",
            method="GET",
            operation_id="get-test-endpoint",
            summary=None,
            description=None,
            parameters=[],
            request_body=None,
            responses={},
            tags=[]
        )

        result = generate_tool_name(operation)
        assert result == "get_test_endpoint"

    def test_generate_tool_name_without_operation_id(self):
        """Test generating tool name from path and method"""
        operation = Operation(
            path="/api/v1/test",
            method="POST",
            operation_id=None,
            summary=None,
            description=None,
            parameters=[],
            request_body=None,
            responses={},
            tags=[]
        )

        result = generate_tool_name(operation)
        assert result == "post_api_v1_test"

    def test_generate_tool_name_with_path_params(self):
        """Test generating tool name with path parameters"""
        operation = Operation(
            path="/api/v1/users/{id}/posts/{post_id}",
            method="GET",
            operation_id=None,
            summary=None,
            description=None,
            parameters=[],
            request_body=None,
            responses={},
            tags=[]
        )

        result = generate_tool_name(operation)
        assert result == "get_api_v1_users_id_posts_post_id"


class TestGenerateToolDescription:
    """Test suite for tool description generation"""

    def test_generate_description_from_description_field(self):
        """Test generating description from description field"""
        operation = Operation(
            path="/api/v1/test",
            method="GET",
            operation_id="test",
            summary="Test summary",
            description="This is a detailed description",
            parameters=[],
            request_body=None,
            responses={},
            tags=[]
        )

        result = generate_tool_description(operation)
        assert result == "This is a detailed description"

    def test_generate_description_from_summary_field(self):
        """Test generating description from summary when description missing"""
        operation = Operation(
            path="/api/v1/test",
            method="GET",
            operation_id="test",
            summary="Test summary",
            description=None,
            parameters=[],
            request_body=None,
            responses={},
            tags=[]
        )

        result = generate_tool_description(operation)
        assert result == "Test summary"

    def test_generate_description_fallback(self):
        """Test fallback description generation"""
        operation = Operation(
            path="/api/v1/test",
            method="POST",
            operation_id="test",
            summary=None,
            description=None,
            parameters=[],
            request_body=None,
            responses={},
            tags=[]
        )

        result = generate_tool_description(operation)
        assert result == "POST /api/v1/test"

    def test_generate_description_truncation(self):
        """Test that long descriptions are truncated"""
        long_desc = "A" * 300
        operation = Operation(
            path="/api/v1/test",
            method="GET",
            operation_id="test",
            summary=None,
            description=long_desc,
            parameters=[],
            request_body=None,
            responses={},
            tags=[]
        )

        result = generate_tool_description(operation)
        assert len(result) <= 200
        assert result.endswith("...")


class TestShouldIncludeOperation:
    """Test suite for operation filtering"""

    def _op(self, path="/api/v1/test", method="GET", extensions=None):
        return Operation(
            path=path, method=method, operation_id="test",
            summary=None, description=None, parameters=[],
            request_body=None, responses={}, tags=[],
            extensions=extensions or {},
        )

    def test_excluded_when_no_marker(self):
        assert should_include_operation(self._op()) is False

    def test_included_when_marker_present(self):
        assert should_include_operation(self._op(extensions={"x-mcp-expose": True})) is True

    def test_excluded_when_deprecated(self):
        op = self._op(extensions={"x-mcp-expose": True})
        op.deprecated = True
        assert should_include_operation(op) is False



class TestMaybeAppendOrgProjectHint:
    """Tests for the auto-injected org/project hint."""

    def test_appended_when_organization_id_in_schema(self):
        schema = {"properties": {"organization_id": {"type": "integer"}}}
        result = maybe_append_org_project_hint("aiagents_list", schema, "Base description.")
        assert result.startswith("Base description.")
        assert ORG_PROJECT_HINT in result

    def test_appended_when_project_id_in_schema(self):
        schema = {"properties": {"project_id": {"type": "integer"}}}
        result = maybe_append_org_project_hint("metrics_list", schema, "Base description.")
        assert ORG_PROJECT_HINT in result

    def test_appended_for_django_fk_names_in_body(self):
        # POST /aiagents/ uses serializer FK fields named `project`/`organization`
        schema = {"properties": {"project": {"type": "integer"}, "agent_name": {"type": "string"}}}
        result = maybe_append_org_project_hint("aiagents_create", schema, "Base description.")
        assert ORG_PROJECT_HINT in result

    def test_not_appended_when_neither_present(self):
        schema = {"properties": {"agent_id": {"type": "integer"}}}
        result = maybe_append_org_project_hint("aiagents_retrieve", schema, "Base description.")
        assert result == "Base description."

    def test_suppressed_by_overlay_flag(self, monkeypatch):
        monkeypatch.setattr(
            "tool_generator.load_tool_overlays",
            lambda: {"some_tool": {"suppress_org_project_hint": True}},
        )
        schema = {"properties": {"organization_id": {"type": "integer"}}}
        result = maybe_append_org_project_hint("some_tool", schema, "Base description.")
        assert result == "Base description."

    def test_handles_missing_properties_key(self):
        schema = {}
        result = maybe_append_org_project_hint("anything", schema, "Base description.")
        assert result == "Base description."
