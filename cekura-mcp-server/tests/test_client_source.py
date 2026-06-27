"""X-Client-Source forwarding + spoof-guard (CEK-7815).

The gateway forwards a self-declared X-Client-Source only for the trusted
bearer-authed Cekura sandbox; every other caller (and all API-key callers)
is recorded as plain "mcp", so an external MCP client cannot spoof a
dashboard/cekura_ai_agent origin.
"""
import pytest

from http_client import CekuraAPIClient
from openapi_mcp_server import _resolve_client_source, request_client_source


@pytest.fixture
def incoming():
    """Set the connection-level X-Client-Source context var for one test."""
    tokens = []

    def _set(value):
        tokens.append(request_client_source.set(value))

    yield _set
    for tok in reversed(tokens):
        request_client_source.reset(tok)


def test_default_outbound_header_is_mcp():
    client = CekuraAPIClient("http://example.invalid", "tok")
    assert client.client.headers["X-Client-Source"] == "mcp"


def test_explicit_client_source_is_forwarded():
    client = CekuraAPIClient("http://example.invalid", "tok", client_source="cekura_ai_agent")
    assert client.client.headers["X-Client-Source"] == "cekura_ai_agent"


def test_bearer_caller_may_declare_cekura_ai_agent(incoming):
    incoming("cekura_ai_agent")
    assert _resolve_client_source("bearer") == "cekura_ai_agent"


def test_api_key_caller_cannot_spoof_cekura_ai_agent(incoming):
    incoming("cekura_ai_agent")
    assert _resolve_client_source("api_key") == "mcp"


def test_non_self_declarable_source_is_forced_to_mcp(incoming):
    # Even a bearer caller can only declare the allow-listed source.
    incoming("dashboard")
    assert _resolve_client_source("bearer") == "mcp"


def test_declared_source_is_case_insensitive(incoming):
    incoming("CEKURA_AI_AGENT")
    assert _resolve_client_source("bearer") == "cekura_ai_agent"


def test_no_incoming_header_defaults_to_mcp():
    assert _resolve_client_source("bearer") == "mcp"
    assert _resolve_client_source("api_key") == "mcp"
