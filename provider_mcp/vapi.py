import json
from urllib.parse import quote

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

_vapi_mcp_url = "https://mcp.vapi.ai/mcp"
_vapi_rest_url = "https://api.vapi.ai"


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


routes = [Route("/mcp", mcp, methods=["POST", "GET", "DELETE"])]
