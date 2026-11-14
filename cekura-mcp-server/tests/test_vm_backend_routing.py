"""Exercise VM backend selection through the real MCP HTTP tool dispatcher."""
import json
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.fastmcp import FastMCP
from starlette.testclient import TestClient

import openapi_mcp_server as srv


@pytest.mark.parametrize(
    "host,override,expected_host",
    [
        ("vm.cekura.ai", None, "vm.cekura.ai"),
        ("vm.cekura.ai:443", "https://api.cekura.ai", "vm.cekura.ai"),
        ("api.cekura.ai", None, "api.cekura.ai"),
        ("api.cekura.ai", "https://test.cekura.ai/", "test.cekura.ai"),
    ],
)
def test_tool_call_selects_backend_from_raw_host(monkeypatch, host, override, expected_host):
    monkeypatch.setattr(srv, "_ALLOW_BASE_URL_OVERRIDE", True)
    monkeypatch.setattr(srv, "server_config", SimpleNamespace(
        base_url="https://api.cekura.ai", skill_gate_mode="off",
    ))
    monkeypatch.setattr(srv, "mcp", FastMCP(
        "Routing test", stateless_http=True, transport_security=srv.transport_security,
    ))
    monkeypatch.setattr(srv, "operations_registry", {
        "routing_test": {
            "title": "Routing test", "description": "List agents", "schema": {},
            "operation": SimpleNamespace(
                method="GET", path="/test_framework/v2/aiagents/",
                parameters=[], request_body=None,
            ),
        },
    })
    srv.setup_dynamic_tool_handlers()

    requests = []

    def backend(request):
        requests.append(request)
        return httpx.Response(200, json={"results": [{"id": 10}]})

    original_async_client = httpx.AsyncClient

    def mock_async_client(*args, **kwargs):
        return original_async_client(*args, **kwargs, transport=httpx.MockTransport(backend))

    monkeypatch.setattr(httpx, "AsyncClient", mock_async_client)
    headers = {
        "Host": host,
        "X-CEKURA-API-KEY": "test-api-key",
        # Forwarded Host must neither disable VM pinning nor enable it on a normal host.
        "X-Forwarded-Host": "api.cekura.ai" if host.startswith("vm.") else "vm.cekura.ai",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-03-26",
    }
    if override:
        headers["X-CEKURA-BASE-URL"] = override

    # An old session/context snapshot must not take precedence over this request.
    token = srv.request_base_url.set("https://stale.example")
    try:
        with TestClient(srv.mcp.streamable_http_app()) as client:
            response = client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "routing_test", "arguments": {}},
            })
    finally:
        srv.request_base_url.reset(token)

    assert response.status_code == 200
    messages = [json.loads(line[6:]) for line in response.text.splitlines()
                if line.startswith("data: ")]
    text = messages[0]["result"]["content"][0]["text"]
    assert json.loads(text.split("\n\n", 1)[0]) == {"results": [{"id": 10}]}
    assert len(requests) == 1
    assert requests[0].url.host == expected_host
    assert requests[0].url.path == "/test_framework/v2/aiagents/"
    assert requests[0].headers["X-CEKURA-API-KEY"] == "test-api-key"


@pytest.mark.parametrize("host", ["VM.CEKURA.AI:443", "vm.cekura.ai."])
def test_vm_host_normalization(host):
    assert srv._vm_backend_base_url({"Host": host}) == "https://vm.cekura.ai"


def test_similar_hostname_does_not_select_vm_backend():
    assert srv._vm_backend_base_url({"Host": "vm.cekura.ai.example"}) is None
