"""Tests for the dispatch-layer classifier that routes tool args into
path / query / body buckets based on the OpenAPI operation shape."""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pytest

from openapi_mcp_server import _dispatch_args


@dataclass
class FakeOp:
    path: str
    parameters: Optional[List[Dict[str, Any]]] = None
    request_body: Optional[Dict[str, Any]] = None


JSON_OBJECT_BODY = {
    "content": {
        "application/json": {
            "schema": {"$ref": "#/components/schemas/PatchedAgent"}
        }
    }
}

JSON_ARRAY_BODY = {
    "content": {
        "application/json": {
            "schema": {"type": "array", "items": {"type": "object"}}
        }
    }
}


class TestDispatchArgs:
    def test_get_routes_everything_to_query(self):
        op = FakeOp(
            path="/aiagents/",
            parameters=[{"name": "page", "in": "query"}, {"name": "project_id", "in": "query"}],
        )
        path, query, body = _dispatch_args(op, {"page": 1, "project_id": 5})
        assert path == "/aiagents/"
        assert query == {"page": 1, "project_id": 5}
        assert body is None

    def test_patch_routes_body_fields_to_body_not_query(self):
        # The actual bug: PATCH with a $ref body schema. `description` must NOT
        # land in the URL — only path params do.
        op = FakeOp(
            path="/aiagents/{id}/",
            parameters=[{"name": "id", "in": "path"}],
            request_body=JSON_OBJECT_BODY,
        )
        path, query, body = _dispatch_args(
            op, {"id": 16959, "description": "A" * 8000, "agent_name": "Bot"}
        )
        assert path == "/aiagents/16959/"
        assert query == {}
        assert body == {"description": "A" * 8000, "agent_name": "Bot"}

    def test_patch_with_explicit_query_param_preserved(self):
        # POST/PATCH endpoints can declare query params (e.g. `project_id` on
        # create). Those still belong in the URL.
        op = FakeOp(
            path="/aiagents/",
            parameters=[{"name": "project_id", "in": "query"}],
            request_body=JSON_OBJECT_BODY,
        )
        path, query, body = _dispatch_args(
            op, {"project_id": 99, "description": "agent description"}
        )
        assert path == "/aiagents/"
        assert query == {"project_id": 99}
        assert body == {"description": "agent description"}

    def test_top_level_array_body_unwrapped(self):
        op = FakeOp(
            path="/metrics/bulk_create/",
            request_body=JSON_ARRAY_BODY,
        )
        path, query, body = _dispatch_args(
            op, {"items": [{"name": "m1"}, {"name": "m2"}]}
        )
        assert path == "/metrics/bulk_create/"
        assert query == {}
        assert body == [{"name": "m1"}, {"name": "m2"}]

    def test_path_substitution_multiple_params(self):
        op = FakeOp(path="/aiagents/{agent_id}/tool/{tool_name}/")
        path, _, _ = _dispatch_args(op, {"agent_id": 1, "tool_name": "search"})
        assert path == "/aiagents/1/tool/search/"

    def test_none_values_dropped(self):
        op = FakeOp(
            path="/aiagents/{id}/",
            parameters=[{"name": "id", "in": "path"}],
            request_body=JSON_OBJECT_BODY,
        )
        _, _, body = _dispatch_args(op, {"id": 1, "description": "x", "agent_name": None})
        assert body == {"description": "x"}

    def test_no_op_parameters_defaults_to_empty(self):
        # op.parameters may be None or missing — should not crash.
        op = FakeOp(path="/aiagents/{id}/", parameters=None, request_body=JSON_OBJECT_BODY)
        path, query, body = _dispatch_args(op, {"id": 1, "description": "x"})
        assert path == "/aiagents/1/"
        assert query == {}
        assert body == {"description": "x"}

    def test_get_with_unknown_param_still_goes_to_query(self):
        # If the caller passes a param not declared in `parameters` and there's
        # no body, fall through to query (legacy permissive behaviour).
        op = FakeOp(path="/aiagents/", parameters=[])
        _, query, body = _dispatch_args(op, {"some_filter": "x"})
        assert query == {"some_filter": "x"}
        assert body is None

    def test_large_payload_keeps_url_small(self):
        # The regression test for the nginx 4094-byte Request-Line cap.
        op = FakeOp(
            path="/aiagents/{id}/",
            parameters=[{"name": "id", "in": "path"}],
            request_body=JSON_OBJECT_BODY,
        )
        path, query, _ = _dispatch_args(op, {"id": 1, "description": "X" * 100_000})
        # URL contains only path; no query params.
        assert path == "/aiagents/1/"
        assert query == {}
