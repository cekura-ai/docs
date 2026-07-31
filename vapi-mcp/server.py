import json
import os
from urllib.parse import quote

if os.getenv("AWS_SECRET_NAME"):
    import boto3

    secret = boto3.client("secretsmanager").get_secret_value(
        SecretId=os.environ["AWS_SECRET_NAME"]
    )
    os.environ.update(json.loads(secret["SecretString"]))

import httpx
import jwt
import uvicorn
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

_oauth_secret = os.getenv("VAPI_MCP_OAUTH_TOKEN_SECRET")
_oauth_audience = os.getenv("VAPI_MCP_CEKURA_OAUTH_AUDIENCE", "https://api.cekura.ai")
_mcp_server_url = os.getenv("VAPI_MCP_SERVER_URL", "http://localhost:8080/mcp").rstrip("/")
_mcp_issuer_url = os.getenv("VAPI_MCP_ISSUER_URL", "https://api.cekura.ai").rstrip("/")
_vapi_mcp_url = "https://mcp.vapi.ai/mcp"
_vapi_rest_url = "https://api.vapi.ai"

if not _oauth_secret:
    raise RuntimeError("VAPI_MCP_OAUTH_TOKEN_SECRET is required")


def _unauthorized(description):
    return JSONResponse(
        {"error": "unauthorized", "error_description": description},
        status_code=401,
        headers={
            "WWW-Authenticate": (
                f'Bearer resource_metadata="{_mcp_server_url}/.well-known/oauth-protected-resource", '
                'error="invalid_token"'
            )
        },
    )


def _sse_response(payload, status_code=200):
    return Response(
        f"event: message\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n",
        status_code=status_code,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


def _jsonrpc_error(request_id, code, message):
    return _sse_response(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
    )


class CredentialMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path.rstrip("/") or "/"
        public_paths = {
            "/mcp/health",
            "/mcp/healthz",
            "/mcp/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource",
        }
        if path in public_paths:
            return await call_next(request)

        if path != "/mcp" and not path.startswith("/mcp/"):
            return await call_next(request)

        authorization = request.headers.get("authorization", "")
        if not authorization.lower().startswith("bearer "):
            return _unauthorized("authenticate with a Cekura OAuth Bearer token")

        try:
            claims = jwt.decode(
                authorization[7:].strip(),
                _oauth_secret,
                algorithms=["HS256"],
                audience=_oauth_audience,
                issuer=_mcp_issuer_url,
            )
        except jwt.PyJWTError:
            return _unauthorized("invalid Cekura OAuth token")

        if claims.get("type") != "oauth_access" or not claims.get("sub"):
            return _unauthorized("invalid Cekura OAuth token")

        if not request.headers.get("x-vapi-api-key"):
            return _unauthorized("pass your Vapi key via the x-vapi-api-key header")

        return await call_next(request)


async def health(request):
    return JSONResponse({"status": "healthy", "service": "vapi-mcp"})


async def oauth_protected_resource(request):
    return JSONResponse(
        {"resource": _mcp_server_url, "authorization_servers": [_mcp_issuer_url]}
    )


def _upstream_headers(request):
    headers = {
        "Authorization": f"Bearer {request.headers['x-vapi-api-key']}",
        "Content-Type": request.headers.get("content-type", "application/json"),
        "Accept": request.headers.get("accept", "application/json, text/event-stream"),
    }
    for name in ("mcp-protocol-version", "mcp-session-id"):
        if value := request.headers.get(name):
            headers[name] = value
    return headers


async def _get_assistant(request, payload):
    request_id = payload.get("id")
    assistant_id = payload.get("params", {}).get("arguments", {}).get("assistantId")
    if not isinstance(assistant_id, str) or not assistant_id:
        return _jsonrpc_error(request_id, -32602, "assistantId is required")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(
                f"{_vapi_rest_url}/assistant/{quote(assistant_id, safe='')}",
                headers={"Authorization": f"Bearer {request.headers['x-vapi-api-key']}"},
            )
    except httpx.HTTPError:
        return _jsonrpc_error(request_id, -32000, "Vapi REST request failed")

    if response.is_error:
        return _jsonrpc_error(request_id, -32000, f"Vapi REST returned {response.status_code}")

    try:
        assistant = response.json()
    except ValueError:
        return _jsonrpc_error(request_id, -32000, "Vapi REST returned invalid JSON")

    return _sse_response(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "content": [
                    {"type": "text", "text": json.dumps(assistant, separators=(",", ":"))}
                ]
            },
        }
    )


async def _proxy(request, body):
    client = httpx.AsyncClient(timeout=httpx.Timeout(30, read=None))
    try:
        upstream_request = client.build_request(
            request.method,
            _vapi_mcp_url,
            content=body,
            headers=_upstream_headers(request),
        )
        response = await client.send(upstream_request, stream=True)
    except httpx.HTTPError:
        await client.aclose()
        return JSONResponse({"error": "Vapi MCP request failed"}, status_code=502)

    headers = {
        name: value
        for name, value in response.headers.items()
        if name.lower() in {"content-type", "cache-control", "mcp-session-id", "mcp-protocol-version"}
    }

    async def body_iterator():
        try:
            async for chunk in response.aiter_raw():
                yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return StreamingResponse(body_iterator(), status_code=response.status_code, headers=headers)


async def mcp(request: Request):
    body = await request.body()
    if request.method == "POST":
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = None

        if (
            isinstance(payload, dict)
            and payload.get("method") == "tools/call"
            and payload.get("params", {}).get("name") == "get_assistant"
        ):
            return await _get_assistant(request, payload)

    return await _proxy(request, body)


app = Starlette(
    routes=[
        Route("/mcp", mcp, methods=["POST", "GET", "DELETE"]),
        Route("/mcp/health", health),
        Route("/mcp/healthz", health),
        Route("/mcp/.well-known/oauth-protected-resource", oauth_protected_resource),
        Route("/.well-known/oauth-protected-resource", oauth_protected_resource),
    ]
)
app.add_middleware(CredentialMiddleware)


if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8080")))
