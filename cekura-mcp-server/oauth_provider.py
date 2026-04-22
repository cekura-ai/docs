import os
import hashlib
import json
import secrets
import time
import logging
from urllib.parse import urlencode

from joserfc import jwe
from joserfc.jwk import OctKey
from pydantic import AnyUrl
from mcp.server.auth.provider import (
    OAuthAuthorizationServerProvider,
    AuthorizationCode,
    RefreshToken,
    AccessToken,
    AuthorizationParams,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

logger = logging.getLogger(__name__)

OAUTH_JWT_SECRET = os.environ.get("OAUTH_JWT_SECRET", "")
if not OAUTH_JWT_SECRET:
    OAUTH_JWT_SECRET = secrets.token_urlsafe(32)
    logger.warning("OAUTH_JWT_SECRET not set — generated temporary secret; tokens will be invalid after restart")

ACCESS_TOKEN_TTL = 3600              # 1 hour
REFRESH_TOKEN_TTL = 30 * 24 * 3600  # 30 days

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "https://app.cekura.ai")
MCP_ISSUER_URL = os.environ.get("MCP_ISSUER_URL", "https://api.cekura.ai")

# Derive 32-byte AES-256 key from the JWT secret
_raw_key = hashlib.sha256(OAUTH_JWT_SECRET.encode()).digest()
JWE_KEY = OctKey.import_key(_raw_key)
JWE_HEADER = {"alg": "dir", "enc": "A256GCM"}


def _jwe_encode(claims: dict) -> str:
    return jwe.encrypt_compact(JWE_HEADER, json.dumps(claims).encode(), JWE_KEY)


def _jwe_decode(token: str) -> dict | None:
    """Decrypt a JWE token. Returns claims dict or None if invalid/expired."""
    try:
        obj = jwe.decrypt_compact(token, JWE_KEY)
        claims = json.loads(obj.plaintext)
        if claims.get("exp") and claims["exp"] < time.time():
            return None
        return claims
    except Exception:
        return None


class CekuraAuthCode(AuthorizationCode):
    api_key: str


class CekuraAccessToken(AccessToken):
    api_key: str


def decode_bearer_credential(bearer_token: str) -> tuple[str, str]:
    """
    Extract the backend credential from a Bearer token.
    Returns (credential, type) where type is "api_key" or "bearer".
    - Our JWE access token → ("api_key_value", "api_key")
    - Raw Cekura JWT (agent/CLI passthrough) → (token, "bearer")
    """
    claims = _jwe_decode(bearer_token)
    if claims and claims.get("type") == "access" and claims.get("api_key"):
        return claims["api_key"], "api_key"
    return bearer_token, "bearer"


class CekuraMCPOAuthProvider(OAuthAuthorizationServerProvider):
    def __init__(self):
        self._clients: dict[str, OAuthClientInformationFull] = {}
        self._auth_codes: dict[str, CekuraAuthCode] = {}
        self._pending_sessions: dict[str, dict] = {}

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self._clients.get(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        # Prune expired sessions to prevent unbounded memory growth
        now = time.time()
        expired = [k for k, v in self._pending_sessions.items() if v["expires_at"] < now]
        for k in expired:
            del self._pending_sessions[k]

        session_id = secrets.token_urlsafe(32)
        self._pending_sessions[session_id] = {
            "client_id": client.client_id,
            "code_challenge": params.code_challenge,
            "redirect_uri": str(params.redirect_uri),
            "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
            "scopes": params.scopes or [],
            "state": params.state,
            "resource": params.resource,
            "expires_at": time.time() + 600,
        }
        callback_url = f"{MCP_ISSUER_URL}/oauth/callback"
        params_str = urlencode({"redirect_uri": callback_url, "state": session_id})
        return f"{DASHBOARD_URL}/oauth/mcp-authorize?{params_str}"

    async def handle_callback(self, mcp_exchange_code: str, session_id: str) -> str:
        """
        Invoked by /oauth/callback after dashboard redirects back.
        mcp_exchange_code is a JWE token (exp: 60s, encrypted with OAUTH_JWT_SECRET)
        containing {api_key, type: "mcp_code"} — created by the dashboard server-side.
        Returns the redirect URL back to the OAuth client.
        """
        session = self._pending_sessions.pop(session_id, None)
        if not session or session["expires_at"] < time.time():
            raise ValueError("Invalid or expired OAuth session")

        claims = _jwe_decode(mcp_exchange_code)
        if not claims or claims.get("type") != "mcp_code" or not claims.get("api_key"):
            raise ValueError("Invalid or expired mcp_exchange_code")

        api_key = claims["api_key"]
        code = secrets.token_urlsafe(32)
        self._auth_codes[code] = CekuraAuthCode(
            code=code,
            scopes=session["scopes"],
            expires_at=time.time() + 60,
            client_id=session["client_id"],
            code_challenge=session["code_challenge"],
            redirect_uri=AnyUrl(session["redirect_uri"]),
            redirect_uri_provided_explicitly=session["redirect_uri_provided_explicitly"],
            resource=session["resource"],
            api_key=api_key,
        )
        return construct_redirect_uri(session["redirect_uri"], code=code, state=session["state"])

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> CekuraAuthCode | None:
        code = self._auth_codes.get(authorization_code)
        if not code or code.expires_at < time.time() or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: CekuraAuthCode
    ) -> OAuthToken:
        self._auth_codes.pop(authorization_code.code, None)
        return self._issue_token_pair(
            client.client_id,
            authorization_code.scopes,
            authorization_code.api_key,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        claims = _jwe_decode(refresh_token)
        if not claims or claims.get("type") != "refresh" or claims.get("client_id") != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=client.client_id,
            scopes=claims.get("scopes", []),
            expires_at=claims.get("exp"),
        )

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        claims = _jwe_decode(refresh_token.token)
        if not claims or claims.get("type") != "refresh":
            raise TokenError(error="invalid_grant", error_description="Invalid refresh token")

        api_key = claims.get("api_key")
        if not api_key:
            raise TokenError(error="invalid_grant", error_description="Missing API key in refresh token")

        use_scopes = scopes or claims.get("scopes", [])
        return self._issue_token_pair(client.client_id, use_scopes, api_key)

    async def load_access_token(self, token: str) -> CekuraAccessToken | None:
        claims = _jwe_decode(token)
        if claims and claims.get("type") == "access" and claims.get("api_key"):
            return CekuraAccessToken(
                token=token,
                client_id=claims.get("client_id", "oauth"),
                scopes=claims.get("scopes", []),
                expires_at=claims.get("exp"),
                api_key=claims["api_key"],
            )

        # Not our JWE — treat as raw Cekura JWT (agent/CLI passthrough)
        return CekuraAccessToken(
            token=token,
            client_id="passthrough",
            scopes=[],
            expires_at=None,
            api_key=token,
        )

    async def revoke_token(self, token) -> None:
        pass

    def _issue_token_pair(self, client_id: str, scopes: list[str], api_key: str) -> OAuthToken:
        now = int(time.time())
        access_token = _jwe_encode({
            "type": "access",
            "sub": "cekura_user",
            "api_key": api_key,
            "client_id": client_id,
            "scopes": scopes,
            "iat": now,
            "exp": now + ACCESS_TOKEN_TTL,
        })
        refresh_token = _jwe_encode({
            "type": "refresh",
            "sub": "cekura_user",
            "api_key": api_key,
            "client_id": client_id,
            "scopes": scopes,
            "iat": now,
            "exp": now + REFRESH_TOKEN_TTL,
        })
        return OAuthToken(
            access_token=access_token,
            token_type="bearer",
            expires_in=ACCESS_TOKEN_TTL,
            refresh_token=refresh_token,
            scope=" ".join(scopes),
        )


oauth_provider = CekuraMCPOAuthProvider()
