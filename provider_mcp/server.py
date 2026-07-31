import json
import os

if os.getenv("AWS_SECRET_NAME"):
    import boto3

    secret = boto3.client("secretsmanager").get_secret_value(
        SecretId=os.environ["AWS_SECRET_NAME"]
    )
    os.environ.update(json.loads(secret["SecretString"]))

import jwt
import uvicorn
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

import elevenlabs_bridge as elevenlabs
import vapi

_oauth_secret = os.getenv("PROVIDER_MCP_OAUTH_TOKEN_SECRET")
_oauth_audience = os.getenv("PROVIDER_MCP_CEKURA_OAUTH_AUDIENCE", "https://api.cekura.ai")
_issuer_url = os.getenv("PROVIDER_MCP_ISSUER_URL", "https://api.cekura.ai").rstrip("/")
_base_url = os.getenv("PROVIDER_MCP_BASE_URL", "http://localhost:8080").rstrip("/")
_providers = {
    "elevenlabs": "xi-elevenlabs-api-key",
    "vapi": "x-vapi-api-key",
}

if not _oauth_secret:
    raise RuntimeError("PROVIDER_MCP_OAUTH_TOKEN_SECRET is required")


def _resource_url(provider):
    return f"{_base_url}/{provider}/mcp"


def _unauthorized(provider, description):
    return JSONResponse(
        {"error": "unauthorized", "error_description": description},
        status_code=401,
        headers={
            "WWW-Authenticate": (
                f'Bearer resource_metadata="{_resource_url(provider)}/.well-known/oauth-protected-resource", '
                'error="invalid_token"'
            )
        },
    )


class CredentialMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        parts = [part for part in request.url.path.split("/") if part]
        provider = None
        public_path = False
        if len(parts) >= 2 and parts[0] in _providers and parts[1] == "mcp":
            provider = parts[0]
            public_path = len(parts) > 2 and parts[2] in {"health", "healthz", ".well-known"}

        if provider:
            if public_path:
                return await call_next(request)

            authorization = request.headers.get("authorization", "")
            if not authorization.lower().startswith("bearer "):
                return _unauthorized(provider, "authenticate with a Cekura OAuth Bearer token")

            try:
                claims = jwt.decode(
                    authorization[7:].strip(),
                    _oauth_secret,
                    algorithms=["HS256"],
                    audience=_oauth_audience,
                    issuer=_issuer_url,
                )
            except jwt.PyJWTError:
                return _unauthorized(provider, "invalid Cekura OAuth token")

            if claims.get("type") != "oauth_access" or not claims.get("sub"):
                return _unauthorized(provider, "invalid Cekura OAuth token")

            provider_key = _providers[provider]
            key = request.headers.get(provider_key)
            if not key:
                return _unauthorized(provider, f"pass your provider key via the {provider_key} header")

            if provider == "elevenlabs":
                token = elevenlabs.set_api_key(key)
                try:
                    return await call_next(request)
                finally:
                    elevenlabs.reset_api_key(token)

        return await call_next(request)


async def health(request):
    provider = request.path_params["provider"]
    if provider not in _providers:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"status": "healthy", "service": "provider-mcp", "provider": provider})


async def oauth_protected_resource(request: Request):
    provider = request.path_params["provider"]
    if provider not in _providers:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"resource": _resource_url(provider), "authorization_servers": [_issuer_url]})


app = Starlette(
    routes=[
        Route("/{provider}/mcp/health", health),
        Route("/{provider}/mcp/healthz", health),
        Route("/{provider}/mcp/.well-known/oauth-protected-resource", oauth_protected_resource),
        Mount("/elevenlabs", app=elevenlabs.app),
        Mount("/vapi", routes=vapi.routes),
    ],
    lifespan=elevenlabs.lifespan,
)
app.add_middleware(CredentialMiddleware)


if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "0.0.0.0"), port=int(os.getenv("PORT", "8080")))
