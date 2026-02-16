import sys
import asyncio
import logging
import httpx
import json
from typing import Any, Dict, List
from contextvars import ContextVar
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from config import load_config
from openapi_parser import load_openapi_spec
from http_client import create_client
from tool_generator import (
    generate_tool_name,
    generate_tool_description,
    build_input_schema,
    should_include_operation,
    load_documented_apis_whitelist,
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

# Configure transport security to allow api.cekura.ai as Host header
transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[
        "api.cekura.ai",
        "localhost",
        "localhost:8000",
        "localhost:8001",
        "localhost:8002",
        "127.0.0.1",
        "127.0.0.1:8000",
        "127.0.0.1:8001",
        "127.0.0.1:8002",
        "0.0.0.0",
        "0.0.0.0:8000",
        "0.0.0.0:8001",
        "0.0.0.0:8002"
    ]
)

mcp = FastMCP("Cekura API", transport_security=transport_security)

server_config = None
openapi_parser = None
operations_registry = {}

MINTLIFY_MCP_URL = "https://docs.cekura.ai/mcp"
MINTLIFY_SEARCH_TIMEOUT = 15.0
MINTLIFY_MAX_RETRIES = 2


async def initialize_server():
    global server_config, openapi_parser, operations_registry

    try:
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

        tools_registered = 0
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
                tool_description = generate_tool_description(operation)
                input_schema = build_input_schema(operation, openapi_parser)

                register_tool(tool_name, tool_description, input_schema, operation)
                tools_registered += 1
            except Exception as e:
                logger.error(f"Error registering tool for {operation.path}: {e}", exc_info=True)
                continue

        logger.info(f"Registered {tools_registered} MCP tools")

        # Register Mintlify documentation search tool
        register_mintlify_search_tool()
        logger.info("Registered Mintlify documentation search tool")

        setup_dynamic_tool_handlers()

    except Exception as e:
        logger.error(f"Failed to initialize server: {e}", exc_info=True)
        sys.exit(1)


def register_mintlify_search_tool():
    """Register Mintlify documentation search tool as a proxy."""
    operations_registry['SearchCekura'] = {
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
        'is_proxy': True
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
                            "name": "SearchCekura",
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


def register_tool(name: str, description: str, input_schema: Dict[str, Any], operation):
    operations_registry[name] = {
        'operation': operation,
        'schema': input_schema,
        'description': description
    }


@mcp.tool(name="list_available_tools", description="List all available Cekura API tools")
async def list_available_tools() -> str:
    tools = sorted(operations_registry.keys())
    return f"Available tools ({len(tools)}):\n" + "\n".join(f"- {tool}" for tool in tools)


@mcp.tool(name="test_simple_tool", description="A simple test tool to verify MCP registration")
async def test_simple_tool(message: str) -> str:
    return f"Hello from Cekura MCP Server! You said: {message}"


def get_request_api_key():
    """Get API key from current request context."""
    api_key = request_api_key.get()
    if not api_key:
        raise ValueError(
            "No API key found. Please provide API key via X-CEKURA-API-KEY header when connecting to the MCP server."
        )
    return api_key

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
                inputSchema=data['schema']
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

                api_key = get_request_api_key()
                op = tool_data['operation']

                user_api_client = create_client(server_config.base_url, api_key)

                result = await user_api_client.execute_request(
                    method=op.method,
                    path=op.path,
                    params=arguments,
                    body=op.request_body
                )

                await user_api_client.close()
                return [{"type": "text", "text": str(result)}]

            except ValueError as e:
                # API key not found error
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
    import os
    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware

    parser = argparse.ArgumentParser(description="Cekura OpenAPI MCP Server")
    parser.add_argument("--port", type=int, default=8001, help="Port to run the HTTP server on (default: 8001)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)")
    args = parser.parse_args()

    logger.info("Starting Cekura OpenAPI MCP Server...")

    asyncio.run(initialize_server())

    logger.info(f"Server initialized successfully. Running on http://{args.host}:{args.port}/mcp")

    class APIKeyMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.url.path in ["/mcp/health", "/mcp/healthz"]:
                response = await call_next(request)
                return response

            api_key = request.headers.get('X-CEKURA-API-KEY') or request.headers.get('x-cekura-api-key')

            if api_key:
                request_api_key.set(api_key)
                logger.debug(f"API key set for request: {api_key[:20]}...")

            response = await call_next(request)
            return response

    from starlette.responses import JSONResponse
    from starlette.routing import Route

    async def health_check(request):
        return JSONResponse({
            "status": "healthy",
            "service": "cekura-mcp-server",
            "tools_registered": len(operations_registry)
        })

    app = mcp.streamable_http_app()

    app.router.routes.insert(0, Route("/mcp/health", health_check))
    app.router.routes.insert(1, Route("/mcp/healthz", health_check))

    app.add_middleware(APIKeyMiddleware)
    logger.info("API Key middleware added")
    logger.info("Health check endpoints: /mcp/health, /mcp/healthz")

    logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
