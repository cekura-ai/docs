import sys
import asyncio
import logging
from typing import Any, Dict
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
        "127.0.0.1",
        "127.0.0.1:8000",
        "0.0.0.0",
        "0.0.0.0:8000"
    ]
)

mcp = FastMCP("Cekura API", transport_security=transport_security)

server_config = None
openapi_parser = None
operations_registry = {}


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

        setup_dynamic_tool_handlers()

    except Exception as e:
        logger.error(f"Failed to initialize server: {e}", exc_info=True)
        sys.exit(1)


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
                # Get API key from request context (raises ValueError if not found)
                api_key = get_request_api_key()

                tool_data = operations_registry[name]
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
            "status": "healthy like a Thor !!!",
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
