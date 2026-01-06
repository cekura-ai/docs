import httpx
from typing import Dict, Any, Optional
import re
import json


class CekuraAPIClient:
    def __init__(self, base_url: str, api_key: str, timeout: int = 30):
        self.base_url = base_url
        self.api_key = api_key
        self.client = httpx.AsyncClient(
            headers={
                "X-CEKURA-API-KEY": api_key,
                "Content-Type": "application/json",
            },
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

    def _build_request_body(self, body_schema: Optional[Dict[str, Any]], params: Dict[str, Any]) -> Dict[str, Any]:
        if not body_schema:
            body = {}
            for k, v in params.items():
                if v is not None:
                    body[k] = self._parse_json_field(k, v)
            return body

        body = {}
        for k, v in params.items():
            if v is not None:
                body[k] = self._parse_json_field(k, v)
        return body

    def _parse_json_field(self, key: str, value: Any) -> Any:
        if not isinstance(value, str):
            return value

        json_field_patterns = ['_json', 'metadata', 'dynamic_variables', 'context', '_data']
        if any(pattern in key.lower() for pattern in json_field_patterns):
            try:
                return json.loads(value)
            except (json.JSONDecodeError, TypeError):
                return value

        return value

    def _handle_response(self, response: httpx.Response) -> Dict[str, Any]:
        if response.status_code == 200 or response.status_code == 201:
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


def create_client(base_url: str, api_key: str, timeout: int = 30) -> CekuraAPIClient:
    return CekuraAPIClient(base_url, api_key, timeout)
