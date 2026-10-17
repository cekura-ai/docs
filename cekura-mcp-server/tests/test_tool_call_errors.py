"""A failed upstream call reaches MCP clients flagged as an error."""
import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import httpx
import pytest
from mcp.types import CallToolRequest, CallToolRequestParams

import openapi_mcp_server as server
from http_client import CekuraAPIClient


@dataclass
class FakeOp:
    method: str
    path: str
    parameters: Optional[List[Dict[str, Any]]] = None
    request_body: Optional[Dict[str, Any]] = None


@pytest.fixture
def call_tool(monkeypatch):
    def setup(status_code, body):
        def create_client(base_url, credential, *args, **kwargs):
            client = CekuraAPIClient(base_url=base_url, credential=credential)
            client.client = httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(status_code, json=body)
                )
            )
            return client

        monkeypatch.setattr(server, "create_client", create_client)
        monkeypatch.setattr(
            server, "server_config", SimpleNamespace(base_url="http://example.invalid", skill_gate_mode="off")
        )
        monkeypatch.setitem(
            server.operations_registry,
            "widgets_create",
            {
                "operation": FakeOp(
                    method="POST",
                    path="/widgets/",
                    request_body={"content": {"application/json": {"schema": {"type": "object"}}}},
                ),
                "schema": {"type": "object", "properties": {"name": {"type": "string"}}},
                "title": "Create a widget",
                "description": "Create a widget",
            },
        )
        monkeypatch.setattr(server, "get_request_credential", lambda: ("test-key", "api_key"))
        server.setup_dynamic_tool_handlers()
        return server.mcp._mcp_server.request_handlers[CallToolRequest]

    async def run(status_code, body):
        handler = setup(status_code, body)
        response = await handler(
            CallToolRequest(
                method="tools/call",
                params=CallToolRequestParams(name="widgets_create", arguments={"name": "w"}),
            )
        )
        return response.root

    return run


async def test_upstream_400_is_returned_as_error(call_tool):
    result = await call_tool(400, {"name": ["This field is invalid."]})

    assert result.isError is True
    assert result.content[0].text.startswith("Error: ")
    assert "This field is invalid." in result.content[0].text


async def test_upstream_success_is_not_an_error(call_tool):
    result = await call_tool(201, {"id": 7, "name": "w"})

    assert result.isError is False
    assert json.loads(result.content[0].text.split("\n\n[cekura_mcp_call_id")[0]) == {"id": 7, "name": "w"}
