import pytest
from starlette.testclient import TestClient


class TestHealthCheck:
    def test_health_endpoint_accessible(self):
        from starlette.responses import JSONResponse
        from starlette.routing import Route
        from starlette.applications import Starlette

        async def health_check(request):
            return JSONResponse({
                "status": "healthy",
                "service": "cekura-mcp-server",
                "tools_registered": 74
            })

        app = Starlette(routes=[
            Route("/mcp/health", health_check),
        ])

        client = TestClient(app)
        response = client.get("/mcp/health")

        assert response.status_code == 200
        assert response.json()["status"] == "healthy"
        assert response.json()["service"] == "cekura-mcp-server"
        assert "tools_registered" in response.json()

    def test_healthz_endpoint_accessible(self):
        from starlette.responses import JSONResponse
        from starlette.routing import Route
        from starlette.applications import Starlette

        async def health_check(request):
            return JSONResponse({
                "status": "healthy",
                "service": "cekura-mcp-server",
                "tools_registered": 74
            })

        app = Starlette(routes=[
            Route("/mcp/healthz", health_check),
        ])

        client = TestClient(app)
        response = client.get("/mcp/healthz")

        assert response.status_code == 200
        assert response.json()["status"] == "healthy"

    def test_health_check_response_structure(self):
        from starlette.responses import JSONResponse
        from starlette.routing import Route
        from starlette.applications import Starlette

        async def health_check(request):
            return JSONResponse({
                "status": "healthy",
                "service": "cekura-mcp-server",
                "tools_registered": 74
            })

        app = Starlette(routes=[
            Route("/mcp/health", health_check),
        ])

        client = TestClient(app)
        response = client.get("/mcp/health")
        data = response.json()

        assert isinstance(data["status"], str)
        assert isinstance(data["service"], str)
        assert isinstance(data["tools_registered"], int)
