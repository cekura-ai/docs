import contextvars
import os

os.environ.setdefault("ELEVENLABS_API_KEY", "per-request")
os.environ.setdefault("ELEVENLABS_MCP_OUTPUT_MODE", "resources")

import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from elevenlabs_mcp import server as elevenlabs

_request_key = contextvars.ContextVar("elevenlabs_api_key", default="")


def _inject_key(request):
    key = _request_key.get()
    if key:
        request.headers["xi-api-key"] = key


elevenlabs.custom_client.event_hooks = {"request": [_inject_key], "response": []}
elevenlabs.mcp.settings.stateless_http = True


class KeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        key = request.headers.get("xi-api-key") or ""
        requires_key = (
            request.url.path.startswith("/mcp")
            and request.url.path.rstrip("/") not in {"/mcp/health", "/mcp/healthz"}
        )
        if requires_key and not key:
            return JSONResponse(
                {
                    "error": "unauthorized",
                    "error_description": "pass your ElevenLabs key via the xi-api-key header",
                },
                status_code=401,
            )
        token = _request_key.set(key)
        try:
            return await call_next(request)
        finally:
            _request_key.reset(token)


async def health(request):
    return JSONResponse(
        {
            "status": "healthy",
            "service": "elevenlabs-mcp",
            "tools_registered": len(elevenlabs.mcp._tool_manager._tools),
        }
    )


app = elevenlabs.mcp.streamable_http_app()
app.router.routes.insert(0, Route("/mcp/health", health))
app.router.routes.insert(1, Route("/mcp/healthz", health))
app.add_middleware(KeyMiddleware)


if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8080")))
