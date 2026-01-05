import asyncio
import httpx

async def test_health():
    from openapi_mcp_server import health_check
    from starlette.requests import Request
    
    class MockRequest:
        pass
    
    request = MockRequest()
    response = await health_check(request)
    print(f"Status Code: {response.status_code}")
    print(f"Body: {response.body.decode()}")

asyncio.run(test_health())
