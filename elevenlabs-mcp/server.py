import contextvars
import json
import os

os.environ.setdefault("ELEVENLABS_API_KEY", "per-request")
os.environ.setdefault("ELEVENLABS_MCP_OUTPUT_MODE", "resources")

if os.getenv("AWS_SECRET_NAME"):
    import boto3

    secret = boto3.client("secretsmanager").get_secret_value(
        SecretId=os.environ["AWS_SECRET_NAME"]
    )
    os.environ.update(json.loads(secret["SecretString"]))

import jwt
import uvicorn
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from elevenlabs_mcp import server as elevenlabs

_request_key = contextvars.ContextVar("elevenlabs_api_key", default="")
_oauth_secret = os.getenv("OAUTH_TOKEN_SECRET")
_oauth_audience = os.getenv("CEKURA_OAUTH_AUDIENCE", "https://api.cekura.ai")
_mcp_server_url = os.getenv("MCP_SERVER_URL", "http://localhost:8080/mcp").rstrip("/")
_mcp_issuer_url = os.getenv("MCP_ISSUER_URL", "https://api.cekura.ai").rstrip("/")

if not _oauth_secret:
    raise RuntimeError("OAUTH_TOKEN_SECRET is required")


def _inject_key(request):
    key = _request_key.get()
    if key:
        request.headers["xi-api-key"] = key


elevenlabs.custom_client.event_hooks = {"request": [_inject_key], "response": []}
elevenlabs.mcp.settings.stateless_http = True


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

        requires_auth = path == "/mcp" or path.startswith("/mcp/")
        if not requires_auth:
            return await call_next(request)

        authorization = request.headers.get("authorization", "")
        if not authorization.lower().startswith("bearer "):
            return _unauthorized("authenticate with a Cekura OAuth Bearer token")

        token = authorization[7:].strip()
        if not token:
            return _unauthorized("authenticate with a Cekura OAuth Bearer token")

        try:
            claims = jwt.decode(
                token,
                _oauth_secret,
                algorithms=["HS256"],
                audience=_oauth_audience,
                issuer=_mcp_issuer_url,
            )
        except jwt.PyJWTError:
            return _unauthorized("invalid Cekura OAuth token")

        if claims.get("type") != "oauth_access" or not claims.get("sub"):
            return _unauthorized("invalid Cekura OAuth token")

        key = request.headers.get("xi-elevenlabs-api-key", "")
        if not key:
            return _unauthorized(
                "pass your ElevenLabs key via the xi-elevenlabs-api-key header"
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


async def oauth_protected_resource(request):
    return JSONResponse(
        {
            "resource": _mcp_server_url,
            "authorization_servers": [_mcp_issuer_url],
        }
    )


app = elevenlabs.mcp.streamable_http_app()
app.router.routes.insert(0, Route("/mcp/.well-known/oauth-protected-resource", oauth_protected_resource))
app.router.routes.insert(1, Route("/.well-known/oauth-protected-resource", oauth_protected_resource))
app.router.routes.insert(2, Route("/mcp/health", health))
app.router.routes.insert(3, Route("/mcp/healthz", health))
app.add_middleware(CredentialMiddleware)


if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8080")))
