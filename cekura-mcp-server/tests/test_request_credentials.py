"""Tests for per-request credential resolution in tool handlers.

Regression coverage for the session-staleness bug: in stateful streamable-HTTP
mode the session task's contextvars are snapshotted when the session is
created, so a bearer token refreshed mid-session never reaches tool handlers
via the contextvar — handlers must read credentials from the HTTP request
that delivered the current MCP message.
"""
import pytest

import openapi_mcp_server as srv
from mcp.server.lowlevel.server import request_ctx


class _FakeRequest:
    def __init__(self, headers):
        self.headers = headers


class _FakeRequestContext:
    def __init__(self, request):
        self.request = request


@pytest.fixture
def mcp_request(monkeypatch):
    """Install a fake MCP request context carrying the given HTTP headers."""
    tokens = []

    def _install(headers):
        ctx = _FakeRequestContext(_FakeRequest(headers) if headers is not None else None)
        tokens.append(request_ctx.set(ctx))
        return ctx

    yield _install
    for token in tokens:
        request_ctx.reset(token)


class TestGetRequestCredential:
    def test_uses_current_request_token_over_session_snapshot(self, mcp_request):
        # Session task captured the initialize-time token in the contextvar;
        # the POST that delivered this tool call carries a refreshed token.
        srv.request_bearer_token.set("stale-initialize-token")
        mcp_request({"authorization": "Bearer fresh-refreshed-token"})

        credential, credential_type = srv.get_request_credential()

        assert credential_type == "bearer"
        assert credential == "fresh-refreshed-token"

    def test_uses_current_request_api_key_over_session_snapshot(self, mcp_request):
        srv.request_api_key.set("stale-api-key")
        srv.request_bearer_token.set(None)
        mcp_request({"x-cekura-api-key": "fresh-api-key"})

        credential, credential_type = srv.get_request_credential()

        assert credential_type == "api_key"
        assert credential == "fresh-api-key"

    def test_falls_back_to_contextvar_without_mcp_request_context(self):
        # Outside an MCP message (no request context set) the contextvar
        # fallback must keep working.
        srv.request_bearer_token.set("contextvar-token")

        credential, credential_type = srv.get_request_credential()

        assert credential_type == "bearer"
        assert credential == "contextvar-token"

    def test_falls_back_to_contextvar_when_request_has_no_credentials(self, mcp_request):
        # e.g. a transport that doesn't attach an HTTP request to the message.
        srv.request_bearer_token.set("contextvar-token")
        mcp_request(None)

        credential, credential_type = srv.get_request_credential()

        assert credential_type == "bearer"
        assert credential == "contextvar-token"

    def test_raises_without_any_credential(self, mcp_request):
        srv.request_bearer_token.set(None)
        srv.request_api_key.set(None)
        mcp_request({})

        with pytest.raises(ValueError):
            srv.get_request_credential()
