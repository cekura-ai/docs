import asyncio
import json
import logging
import os
import sys
from contextvars import ContextVar
from typing import Any, Dict, List

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

# Load secrets from AWS Secrets Manager before any env var is read
if os.getenv("AWS_SECRET_NAME"):
    import boto3
    _sm_client = boto3.client("secretsmanager")
    _secret_value = _sm_client.get_secret_value(SecretId=os.getenv("AWS_SECRET_NAME"))
    _config = json.loads(_secret_value["SecretString"])
    os.environ.update({key: str(value) for key, value in _config.items()})

from config import load_config
from http_client import create_client
from openapi_parser import load_openapi_spec
from tool_generator import (
    apply_overlay_to_description,
    apply_overlay_to_schema,
    build_input_schema,
    compute_annotations,
    generate_tool_description,
    generate_tool_name,
    load_documented_apis_whitelist,
    maybe_append_org_project_hint,
    should_include_operation,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger(__name__)


class HealthCheckFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        return not any(path in message for path in ['/mcp/health', '/mcp/healthz', '/favicon.ico'])

request_api_key: ContextVar[str] = ContextVar('request_api_key', default=None)
request_bearer_token: ContextVar[str] = ContextVar('request_bearer_token', default=None)
request_base_url: ContextVar[str] = ContextVar('request_base_url', default=None)

# X-CEKURA-BASE-URL override is only allowed when explicitly enabled (dev/staging only)
_ALLOW_BASE_URL_OVERRIDE = os.environ.get("ALLOW_BASE_URL_OVERRIDE", "").lower() in ("1", "true", "yes")

MCP_ISSUER_URL = os.environ.get("MCP_ISSUER_URL", "https://api.cekura.ai")
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "https://api.cekura.ai/mcp")

# Derive allowed hosts from MCP_ISSUER_URL and MCP_SERVER_URL (covers prod, ngrok, local).
from urllib.parse import urlparse as _urlparse

_issuer_host = _urlparse(MCP_ISSUER_URL).netloc
_server_host = _urlparse(MCP_SERVER_URL).netloc
_allowed_hosts = [
    "api.cekura.ai",
    "test.cekura.ai",
    "localhost",
    "localhost:8000",
    "localhost:8001",
    "localhost:8002",
    "127.0.0.1",
    "127.0.0.1:8001",
    "0.0.0.0",
    "0.0.0.0:8001",
]
if _issuer_host and _issuer_host not in _allowed_hosts:
    _allowed_hosts.append(_issuer_host)
if _server_host and _server_host not in _allowed_hosts:
    _allowed_hosts.append(_server_host)

transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=_allowed_hosts,
)

mcp = FastMCP("Cekura API", transport_security=transport_security)

server_config = None
openapi_parser = None
operations_registry = {}

MINTLIFY_MCP_URL = "https://docs.cekura.ai/mcp"
MINTLIFY_SEARCH_TIMEOUT = 15.0
MINTLIFY_MAX_RETRIES = 2
MINTLIFY_TOOL_NAME = "search_cekura"  # Fallback, will be dynamically fetched


async def fetch_mintlify_tool_name():
    """Fetch the search tool name from Mintlify's MCP server dynamically. """
    global MINTLIFY_TOOL_NAME

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0)) as client:
            response = await client.post(
                MINTLIFY_MCP_URL,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream"
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list"
                }
            )
            response.raise_for_status()

            # Parse SSE response
            for line in response.text.split('\n'):
                if line.startswith('data: '):
                    data = json.loads(line[6:])

                    if 'result' in data and 'tools' in data['result']:
                        # Find search tool (contains "search" and "cekura")
                        for tool in data['result']['tools']:
                            name = tool.get('name', '').lower()
                            if 'search' in name and 'cekura' in name:
                                MINTLIFY_TOOL_NAME = tool['name']
                                logger.info(f"Discovered Mintlify tool name: {MINTLIFY_TOOL_NAME}")
                                return

            logger.warning(f"Mintlify search tool not found in response, using fallback: {MINTLIFY_TOOL_NAME}")

    except Exception as e:
        logger.warning(f"Failed to fetch Mintlify tool name (using fallback '{MINTLIFY_TOOL_NAME}'): {e}")


async def initialize_server():
    global server_config, openapi_parser, operations_registry

    try:
        # Fetch Mintlify's actual tool name
        await fetch_mintlify_tool_name()

        server_config = load_config()
        logger.info(f"Loaded config: Base URL={server_config.base_url}")

        openapi_parser = load_openapi_spec(server_config.openapi_spec_path)
        logger.info(f"Loaded OpenAPI spec from {server_config.openapi_spec_path}")

        operations = openapi_parser.extract_operations()
        logger.info(f"Found {len(operations)} operations in OpenAPI spec")

        # Load whitelist of documented APIs
        whitelist = load_documented_apis_whitelist()
        if whitelist:
            logger.info(f"Using documented APIs whitelist with {len(whitelist)} endpoints")
        else:
            logger.warning("No whitelist found - using all operations (filtered by tags/excludes)")

        blocked_tools = server_config.resolve_blocked_tools()

        tools_registered = 0
        blocked_hits = []
        for operation in operations:
            if not should_include_operation(
                operation,
                filter_tags=server_config.filter_tags,
                exclude_ops=server_config.exclude_operations,
                whitelist=whitelist
            ):
                continue

            if server_config.max_tools and tools_registered >= server_config.max_tools:
                logger.warning(f"Reached max_tools limit ({server_config.max_tools}), stopping registration")
                break

            try:
                tool_name = generate_tool_name(operation)

                if tool_name in blocked_tools:
                    blocked_hits.append(tool_name)
                    continue

                tool_description = generate_tool_description(operation)
                input_schema = build_input_schema(operation, openapi_parser)

                tool_description = maybe_append_org_project_hint(tool_name, input_schema, tool_description)
                tool_description = apply_overlay_to_description(tool_name, tool_description)
                input_schema = apply_overlay_to_schema(tool_name, input_schema)

                annotations = compute_annotations(operation)
                register_tool(tool_name, tool_description, input_schema, operation, annotations=annotations)
                tools_registered += 1
            except Exception as e:
                logger.error(f"Error registering tool for {operation.path}: {e}", exc_info=True)
                continue

        if blocked_hits:
            logger.info(f"Registered {tools_registered} MCP tools (blocked: {sorted(blocked_hits)})")
        else:
            logger.info(f"Registered {tools_registered} MCP tools")

        # Non-fatal drift check: log a warning for each overlay that has diverged
        # from the live openapi.json + whitelist. Keeps production booting while
        # making divergence immediately visible in logs / dashboards.
        try:
            from validate_overlays import run_checks as _overlay_checks
            drift = _overlay_checks()
            if drift:
                errs = [f for f in drift if f.level == "error"]
                warns = [f for f in drift if f.level == "warning"]
                if errs:
                    logger.warning(
                        f"Overlay drift: {len(errs)} error(s), {len(warns)} warning(s) — "
                        "run `python3 validate_overlays.py` for details. Overlays are still "
                        "applied; these tools may render with stale or inaccurate descriptions."
                    )
                    for f in errs[:5]:
                        logger.warning(f"  overlay[{f.category}] {f.tool}: {f.message[:200]}")
                elif warns:
                    logger.info(f"Overlay drift: {len(warns)} warning(s) — non-blocking.")
        except Exception as e:
            logger.warning(f"Overlay drift check skipped: {e}")

        # Register Mintlify documentation search tool
        register_mintlify_search_tool()
        logger.info("Registered Mintlify documentation search tool")

        setup_dynamic_tool_handlers()

    except Exception as e:
        logger.error(f"Failed to initialize server: {e}", exc_info=True)
        sys.exit(1)


def register_mintlify_search_tool():
    """Register Mintlify documentation search tool as a proxy."""
    # Use the dynamically fetched tool name
    operations_registry[MINTLIFY_TOOL_NAME] = {
        'operation': None,
        'schema': {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query"
                }
            },
            "required": ["query"],
            "additionalProperties": False,
            "$schema": "http://json-schema.org/draft-07/schema#"
        },
        'description': "Search across the Cekura knowledge base to find relevant information, code examples, API references, and guides. Use this tool when you need to answer questions about Cekura, find specific documentation, understand how features work, or locate implementation details. The search returns contextual content with titles and direct links to the documentation pages.",
        'is_proxy': True,
        'annotations': ToolAnnotations(readOnlyHint=True),
    }


async def call_mintlify_search(query: str) -> List[Dict[str, str]]:
    """Proxy search requests to Mintlify's MCP server with retry logic."""
    if not query or not query.strip():
        return [{"type": "text", "text": "Please provide a search query."}]

    for attempt in range(MINTLIFY_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(MINTLIFY_SEARCH_TIMEOUT, connect=5.0),
                follow_redirects=True
            ) as client:
                response = await client.post(
                    MINTLIFY_MCP_URL,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream"
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": MINTLIFY_TOOL_NAME,
                            "arguments": {"query": query.strip()}
                        }
                    }
                )

                response.raise_for_status()

                for line in response.text.split('\n'):
                    if line.startswith('data: '):
                        try:
                            data = json.loads(line[6:])
                            if 'result' in data and 'content' in data['result']:
                                content = data['result']['content']
                                if content:
                                    return content
                        except json.JSONDecodeError as e:
                            logger.warning(f"Failed to parse SSE data: {e}")
                            continue

                return [{"type": "text", "text": "No results found for your query."}]

        except httpx.TimeoutException:
            logger.warning(f"Timeout calling Mintlify search (attempt {attempt + 1}/{MINTLIFY_MAX_RETRIES})")
            if attempt < MINTLIFY_MAX_RETRIES - 1:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            return [{"type": "text", "text": "Search request timed out. Please try again."}]

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error from Mintlify: {e.response.status_code}")
            return [{"type": "text", "text": f"Documentation search temporarily unavailable (HTTP {e.response.status_code})."}]

        except httpx.RequestError as e:
            logger.error(f"Network error calling Mintlify: {e}")
            if attempt < MINTLIFY_MAX_RETRIES - 1:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            return [{"type": "text", "text": "Unable to reach documentation search. Please check your connection."}]

        except Exception as e:
            logger.error(f"Unexpected error in Mintlify search: {e}", exc_info=True)
            return [{"type": "text", "text": f"Search error: {str(e)}"}]

    return [{"type": "text", "text": "Search failed after multiple attempts. Please try again later."}]


def register_tool(
    name: str,
    description: str,
    input_schema: Dict[str, Any],
    operation,
    annotations: ToolAnnotations = None,
):
    operations_registry[name] = {
        'operation': operation,
        'schema': input_schema,
        'description': description,
        'annotations': annotations,
    }


@mcp.tool(
    name="list_available_tools",
    description="List all available Cekura API tools",
    annotations=ToolAnnotations(readOnlyHint=True),
)
async def list_available_tools() -> str:
    tools = sorted(operations_registry.keys())
    return f"Available tools ({len(tools)}):\n" + "\n".join(f"- {tool}" for tool in tools)


@mcp.tool(
    name="test_simple_tool",
    description="A simple test tool to verify MCP registration",
    annotations=ToolAnnotations(readOnlyHint=True),
)
async def test_simple_tool(message: str) -> str:
    return f"Hello from Cekura MCP Server! You said: {message}"


def get_request_credential() -> tuple[str, str]:
    """Return (credential, type) from request context. Type is 'bearer' or 'api_key'."""
    bearer = request_bearer_token.get()
    if bearer:
        return bearer, "bearer"
    api_key = request_api_key.get()
    if api_key:
        return api_key, "api_key"
    raise ValueError(
        "No credential found. Connect via X-CEKURA-API-KEY header, Bearer token, or OAuth."
    )

def setup_dynamic_tool_handlers():
    from mcp.types import Tool as MCPTool

    original_list_tools = mcp.list_tools
    original_call_tool = mcp.call_tool

    async def list_tools_with_dynamic():
        regular_tools = await original_list_tools()

        dynamic_tools = [
            MCPTool(
                name=name,
                description=data['description'],
                inputSchema=data['schema'],
                annotations=data.get('annotations'),
            )
            for name, data in operations_registry.items()
        ]

        return regular_tools + dynamic_tools

    async def call_tool_with_dynamic(name: str, arguments: dict):
        if name in operations_registry:
            try:
                tool_data = operations_registry[name]

                if tool_data.get('is_proxy'):
                    query = arguments.get('query', '')
                    return await call_mintlify_search(query)

                credential, credential_type = get_request_credential()
                op = tool_data['operation']

                base_url = request_base_url.get() or server_config.base_url
                user_api_client = create_client(base_url, credential, credential_type=credential_type)

                result = await user_api_client.execute_request(
                    method=op.method,
                    path=op.path,
                    params=arguments,
                    body=op.request_body
                )

                await user_api_client.close()
                return [{"type": "text", "text": json.dumps(result, default=str, ensure_ascii=False)}]

            except ValueError as e:
                error_msg = f"Authentication Error: {str(e)}"
                return [{"type": "text", "text": error_msg}]
            except Exception as e:
                import traceback
                error_msg = f"Error: {str(e)}\n{traceback.format_exc()}"
                return [{"type": "text", "text": error_msg}]
        else:
            return await original_call_tool(name=name, arguments=arguments)

    mcp._mcp_server.list_tools()(list_tools_with_dynamic)
    mcp._mcp_server.call_tool(validate_input=False)(call_tool_with_dynamic)

def main():
    import argparse

    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    parser = argparse.ArgumentParser(description="Cekura OpenAPI MCP Server")
    parser.add_argument("--port", type=int, default=8001, help="Port to run the HTTP server on (default: 8001)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)")
    args = parser.parse_args()

    logger.info("Starting Cekura OpenAPI MCP Server...")

    asyncio.run(initialize_server())

    logger.info(f"Server initialized successfully. Running on http://{args.host}:{args.port}/mcp")

    class CredentialMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.url.path in ["/mcp/health", "/mcp/healthz"]:
                return await call_next(request)

            # Bearer token — OAuth web users or agent/CLI JWT passthrough
            auth_header = request.headers.get('Authorization') or request.headers.get('authorization')
            if auth_header and auth_header.lower().startswith('bearer '):
                request_bearer_token.set(auth_header[7:])
                logger.debug("Bearer token credential set for request")

            # API key header — legacy mcp-remote / Claude Desktop
            api_key = request.headers.get('X-CEKURA-API-KEY') or request.headers.get('x-cekura-api-key')
            if api_key:
                request_api_key.set(api_key)
                logger.debug("API key credential set for request")

            # Base URL override — only honoured when ALLOW_BASE_URL_OVERRIDE=true (dev/staging)
            if _ALLOW_BASE_URL_OVERRIDE:
                base_url_override = request.headers.get('X-CEKURA-BASE-URL') or request.headers.get('x-cekura-base-url')
                if base_url_override:
                    request_base_url.set(base_url_override.rstrip("/"))

            return await call_next(request)

    async def health_check(request):
        return JSONResponse({
            "status": "healthy",
            "service": "cekura-mcp-server",
            "tools_registered": len(operations_registry)
        })

    async def oauth_protected_resource(request):
        # RFC 9728 — resource server advertises its authorization server
        return JSONResponse({
            "resource": MCP_SERVER_URL,
            "authorization_servers": [MCP_ISSUER_URL],
        })

    async def oauth_as_metadata(request):
        # Convenience fallback for clients that check AS metadata directly on resource server
        return JSONResponse({
            "issuer": MCP_ISSUER_URL,
            "authorization_endpoint": f"{MCP_ISSUER_URL}/user/oauth/authorize",
            "token_endpoint": f"{MCP_ISSUER_URL}/user/oauth/token",
            "revocation_endpoint": f"{MCP_ISSUER_URL}/user/oauth/revoke",
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
        })

    app = mcp.streamable_http_app()

    app.router.routes.insert(0, Route("/.well-known/oauth-protected-resource", oauth_protected_resource))
    app.router.routes.insert(1, Route("/.well-known/oauth-authorization-server", oauth_as_metadata))
    app.router.routes.insert(2, Route("/mcp/health", health_check))
    app.router.routes.insert(3, Route("/mcp/healthz", health_check))

    app.add_middleware(CredentialMiddleware)
    logger.info("Credential middleware added (API key + Bearer token support)")
    logger.info(f"OAuth discovery: {MCP_SERVER_URL}/.well-known/oauth-protected-resource → {MCP_ISSUER_URL}")
    logger.info("Health check endpoints: /mcp/health, /mcp/healthz")

    logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
