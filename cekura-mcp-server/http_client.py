import httpx
from typing import Dict, Any, Optional
import json


class CekuraAPIClient:
    def __init__(self, base_url: str, credential: str, credential_type: str = "api_key", timeout: int = 30):
        self.base_url = base_url
        auth_header = (
            {"Authorization": f"Bearer {credential}"}
            if credential_type == "bearer"
            else {"X-CEKURA-API-KEY": credential}
        )
        self.client = httpx.AsyncClient(
            headers={**auth_header, "Content-Type": "application/json", "X-Client-Source": "mcp"},
            timeout=timeout,
        )

    async def close(self):
        await self.client.aclose()

    async def execute_request(
        self,
        method: str,
        path: str,
        query_params: Optional[Dict[str, Any]] = None,
        body: Any = None,
        property_types: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        request_body = self._coerce_body(body, property_types) if body is not None else None

        try:
            response = await self.client.request(
                method=method,
                url=url,
                params=self._serialize_query(query_params or {}),
                json=request_body,
            )
            return self._handle_response(response)
        except httpx.TimeoutException:
            raise Exception(f"Request timeout: {method} {url}")
        except httpx.RequestError as e:
            raise Exception(f"Request failed: {method} {url} - {str(e)}")

    @staticmethod
    def _serialize_query(params: Dict[str, Any]) -> Dict[str, Any]:
        out = {}
        for k, v in params.items():
            if v is None:
                continue
            if isinstance(v, list):
                out[k] = ",".join(str(x) for x in v)
            elif isinstance(v, dict):
                out[k] = json.dumps(v)
            else:
                out[k] = v
        return out

    def _coerce_body(self, body: Any, property_types: Optional[Dict[str, str]]) -> Any:
        # Claude occasionally serializes dict/list arguments as strings; recover
        # them based on the field's declared schema type. Strings declared as
        # `type: string` are passed through verbatim (e.g. scenarios.instructions
        # stores a stringified JSON payload that the backend reads literally).
        types = property_types or {}
        if isinstance(body, dict):
            return {k: self._parse_json_field(k, v, types.get(k)) for k, v in body.items()}
        if isinstance(body, str):
            return self._parse_json_field("items", body, "array")
        return body

    # Schemas with a declared primitive type must never have their value coerced —
    # the caller may legitimately send a JSON-looking literal string (e.g.
    # scenarios.instructions, which is `type: string` and stores stringified JSON
    # verbatim).
    _PRIMITIVE_TYPES = ("string", "integer", "number", "boolean")

    # Body fields whose schemas are too loose to recognise by type alone
    # (oneOf with mixed types, allOf, untyped JSONField). Recovery uses name
    # heuristics here.
    _LEGACY_JSON_FIELD_PATTERNS = (
        '_json', 'metadata', 'dynamic_variables', 'context', '_data', 'information',
    )

    def _parse_json_field(self, key: str, value: Any, target_type: Optional[str] = None) -> Any:
        if not isinstance(value, str):
            return value

        if target_type in self._PRIMITIVE_TYPES:
            return value

        # Auto-recover when the value looks like a JSON array/object. Claude sometimes
        # serializes container args as strings even when the schema says
        # type:array/object. Only accept the parse when it actually produces the
        # shape the schema asked for — otherwise the original string is safer.
        if value.startswith(('[', '{')):
            try:
                parsed = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return value
            if target_type == "array" and not isinstance(parsed, list):
                return value
            if target_type == "object" and not isinstance(parsed, dict):
                return value
            return parsed

        if any(pattern in key.lower() for pattern in self._LEGACY_JSON_FIELD_PATTERNS):
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return value

        return value

    def _handle_response(self, response: httpx.Response) -> Dict[str, Any]:
        if 200 <= response.status_code < 300:
            # 204 No Content (common for DELETE) and other empty 2xx bodies.
            if response.status_code == 204 or not response.content:
                return {"status": "ok", "status_code": response.status_code}
            try:
                return response.json()
            except Exception:
                return {"result": response.text}

        if response.status_code == 401:
            raise Exception("Authentication failed (401). Check your CEKURA_API_KEY.")

        if response.status_code == 403:
            raise Exception("Access forbidden (403). You may not have permission for this endpoint.")

        if response.status_code == 404:
            raise Exception(f"Resource not found (404): {response.url}")

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "unknown")
            raise Exception(f"Rate limit exceeded (429). Retry after: {retry_after}")

        if response.status_code >= 500:
            raise Exception(f"Server error ({response.status_code}): {response.text[:200]}")

        try:
            error_detail = response.json()
            raise Exception(f"Request failed ({response.status_code}): {error_detail}")
        except Exception:
            raise Exception(f"Request failed ({response.status_code}): {response.text[:200]}")


def create_client(base_url: str, credential: str, credential_type: str = "api_key", timeout: int = 30) -> CekuraAPIClient:
    return CekuraAPIClient(base_url, credential, credential_type, timeout)
