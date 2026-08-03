import contextvars
import os

os.environ.setdefault("ELEVENLABS_API_KEY", "per-request")
os.environ.setdefault("ELEVENLABS_MCP_OUTPUT_MODE", "resources")

from elevenlabs_mcp import server as elevenlabs

_request_key = contextvars.ContextVar("elevenlabs_api_key", default="")


def _inject_key(request):
    key = _request_key.get()
    if key:
        request.headers["xi-api-key"] = key


elevenlabs.custom_client.event_hooks = {"request": [_inject_key], "response": []}
elevenlabs.mcp.settings.stateless_http = True
app = elevenlabs.mcp.streamable_http_app()


def set_api_key(key):
    return _request_key.set(key)


def reset_api_key(token):
    _request_key.reset(token)


def lifespan(app):
    return elevenlabs.mcp.session_manager.run()
