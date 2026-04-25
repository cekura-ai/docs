import httpx
from typing import Dict, Any, Optional
import re
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
        params: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        params = params or {}
        resolved_path, query_params = self._prepare_request(path, params)
        url = f"{self.base_url}{resolved_path}"
        request_body = self._build_request_body(body, params) if body else None

        try:
            response = await self.client.request(
                method=method,
                url=url,
                params=query_params,
                json=request_body,
            )
            return self._handle_response(response)

        except httpx.TimeoutException:
            raise Exception(f"Request timeout: {method} {url}")
        except httpx.RequestError as e:
            raise Exception(f"Request failed: {method} {url} - {str(e)}")

    def _prepare_request(self, path: str, params: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
        path_params = re.findall(r'\{(\w+)\}', path)
        resolved_path = path
        query_params = {}

        for key, value in params.items():
            if key in path_params:
                resolved_path = resolved_path.replace(f"{{{key}}}", str(value))
            else:
                if value is not None:
                    query_params[key] = value

        return resolved_path, query_params

    def _build_request_body(self, body_schema: Optional[Dict[str, Any]], params: Dict[str, Any]) -> Any:
        if not body_schema:
            body = {}
            for k, v in params.items():
                if v is not None:
                    body[k] = self._parse_json_field(k, v)
            return body

        # Detect top-level array schemas (e.g. bulk_create endpoints).
        # The parser exposes these as a single `items` parameter; unwrap it
        # and send the array directly as the JSON body.
        content = body_schema.get("content", {})
        json_schema = content.get("application/json", {}).get("schema", {})
        if json_schema.get("type") == "array" and "items" in params:
            raw = params["items"]
            return self._parse_json_field("items", raw) if isinstance(raw, str) else raw

        body = {}
        for k, v in params.items():
            if v is not None:
                body[k] = self._parse_json_field(k, v)
        return body

    def _parse_json_field(self, key: str, value: Any) -> Any:
        if not isinstance(value, str):
            return value

        # Auto-parse any string that looks like a JSON array or object.
        # Claude sometimes serializes array/object arguments as strings even when the
        # schema says type:array/object. Safely try to recover here.
        if value.startswith(('[', '{')):
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return value

        json_field_patterns = ['_json', 'metadata', 'dynamic_variables', 'context', '_data', 'information']
        if any(pattern in key.lower() for pattern in json_field_patterns):
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
