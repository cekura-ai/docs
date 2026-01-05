"""Integration tests for MCP server core functionality"""
import pytest
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch, AsyncMock


class TestMCPServerInitialization:
    """Test suite for MCP server initialization"""

    @pytest.mark.asyncio
    async def test_server_initialization_success(self, tmp_path, monkeypatch):
        """Test successful server initialization"""
        # Setup test environment
        spec_file = tmp_path / "openapi.json"
        spec_file.write_text('{"openapi": "3.0.0", "paths": {}}')

        whitelist_file = tmp_path / "data" / "documented_apis.json"
        whitelist_file.parent.mkdir(parents=True, exist_ok=True)
        whitelist_file.write_text('{"total": 0, "endpoints": []}')

        monkeypatch.setenv("CEKURA_BASE_URL", "https://test-api.com")
        monkeypatch.setenv("CEKURA_OPENAPI_SPEC", str(spec_file))

        # Import after setting env vars
        from openapi_mcp_server import initialize_server

        # Should not raise any exceptions
        await initialize_server()

    @pytest.mark.asyncio
    async def test_server_initialization_missing_config(self, monkeypatch):
        """Test that server initialization fails with missing config"""
        monkeypatch.delenv("CEKURA_BASE_URL", raising=False)
        monkeypatch.delenv("CEKURA_OPENAPI_SPEC", raising=False)

        from openapi_mcp_server import initialize_server

        with pytest.raises(SystemExit):
            await initialize_server()

    @pytest.mark.asyncio
    async def test_server_loads_whitelist(self, tmp_path, monkeypatch):
        """Test that server loads whitelist correctly"""
        # Create test files
        spec_file = tmp_path / "openapi.json"
        spec_data = {
            "openapi": "3.0.0",
            "paths": {
                "/api/v1/test": {
                    "get": {
                        "operationId": "get_test",
                        "summary": "Test endpoint",
                        "responses": {"200": {"description": "Success"}}
                    }
                }
            }
        }
        import json
        spec_file.write_text(json.dumps(spec_data))

        # Mock the whitelist file location
        with patch('tool_generator.Path') as mock_path:
            mock_whitelist = tmp_path / "documented_apis.json"
            mock_whitelist.write_text('{"total": 1, "endpoints": [{"method": "GET", "path": "/api/v1/test"}]}')

            mock_path.return_value = tmp_path

            monkeypatch.setenv("CEKURA_BASE_URL", "https://test-api.com")
            monkeypatch.setenv("CEKURA_OPENAPI_SPEC", str(spec_file))

            from openapi_mcp_server import initialize_server
            await initialize_server()


class TestToolRegistration:
    """Test suite for tool registration"""

    def test_tool_name_generation_from_operation_id(self):
        """Test tool name is generated correctly from operation ID"""
        from openapi_parser import Operation
        from tool_generator import generate_tool_name

        operation = Operation(
            path="/api/v1/users",
            method="GET",
            operation_id="list-users",
            summary="List users",
            description=None,
            parameters=[],
            request_body=None,
            responses={},
            tags=["users"]
        )

        tool_name = generate_tool_name(operation)
        assert tool_name == "list_users"
        assert tool_name.replace("_", "").isalnum()

    def test_tool_name_generation_from_path(self):
        """Test tool name is generated from path when no operation ID"""
        from openapi_parser import Operation
        from tool_generator import generate_tool_name

        operation = Operation(
            path="/api/v1/users",
            method="POST",
            operation_id=None,
            summary="Create user",
            description=None,
            parameters=[],
            request_body=None,
            responses={},
            tags=["users"]
        )

        tool_name = generate_tool_name(operation)
        assert "post" in tool_name.lower()
        assert "users" in tool_name.lower()

    def test_tool_description_length_limit(self):
        """Test that tool descriptions are limited to 200 characters"""
        from openapi_parser import Operation
        from tool_generator import generate_tool_description

        long_desc = "A" * 300
        operation = Operation(
            path="/api/v1/test",
            method="GET",
            operation_id="test",
            summary=None,
            description=long_desc,
            parameters=[],
            request_body=None,
            responses={},
            tags=[]
        )

        description = generate_tool_description(operation)
        assert len(description) <= 200
        if len(long_desc) > 200:
            assert description.endswith("...")


class TestWhitelistFiltering:
    """Test suite for whitelist-based filtering"""

    def test_whitelist_filters_operations_correctly(self):
        """Test that whitelist correctly filters operations"""
        from openapi_parser import Operation
        from tool_generator import should_include_operation

        # Operation in whitelist
        whitelisted_op = Operation(
            path="/api/v1/allowed",
            method="GET",
            operation_id="allowed_op",
            summary=None,
            description=None,
            parameters=[],
            request_body=None,
            responses={},
            tags=[]
        )

        # Operation not in whitelist
        non_whitelisted_op = Operation(
            path="/api/v1/blocked",
            method="GET",
            operation_id="blocked_op",
            summary=None,
            description=None,
            parameters=[],
            request_body=None,
            responses={},
            tags=[]
        )

        whitelist = {("GET", "/api/v1/allowed")}

        assert should_include_operation(whitelisted_op, whitelist=whitelist) is True
        assert should_include_operation(non_whitelisted_op, whitelist=whitelist) is False

    def test_whitelist_handles_trailing_slashes(self):
        """Test whitelist handles trailing slashes correctly"""
        from openapi_parser import Operation
        from tool_generator import should_include_operation

        operation_with_slash = Operation(
            path="/api/v1/test/",
            method="GET",
            operation_id="test",
            summary=None,
            description=None,
            parameters=[],
            request_body=None,
            responses={},
            tags=[]
        )

        # Whitelist without trailing slash
        whitelist = {("GET", "/api/v1/test")}

        # Should still match
        assert should_include_operation(operation_with_slash, whitelist=whitelist) is True

    def test_external_operations_always_excluded(self):
        """Test that operations with 'external' in path are always excluded"""
        from openapi_parser import Operation
        from tool_generator import should_include_operation

        external_op = Operation(
            path="/api/external/test",
            method="GET",
            operation_id="external_test",
            summary=None,
            description=None,
            parameters=[],
            request_body=None,
            responses={},
            tags=[]
        )

        # Even without whitelist, external should be excluded
        assert should_include_operation(external_op) is False

        # Even if in whitelist, should be excluded (without whitelist parameter)
        assert should_include_operation(external_op, whitelist=None) is False


class TestAPIKeyHandling:
    """Test suite for API key handling"""

    def test_api_key_required_from_header(self):
        """Test that API key must come from header"""
        from openapi_mcp_server import get_session_api_key, session_api_keys

        # Clear session keys
        session_api_keys.clear()

        # Should raise ValueError when no API key
        with pytest.raises(ValueError, match="No API key found"):
            get_session_api_key()

    def test_api_key_stored_per_session(self):
        """Test that API keys are stored per session"""
        from openapi_mcp_server import session_api_keys

        # Simulate storing API keys for different sessions
        session_api_keys.clear()
        session_api_keys["session_1"] = "key_1"
        session_api_keys["session_2"] = "key_2"

        assert len(session_api_keys) == 2
        assert session_api_keys["session_1"] == "key_1"
        assert session_api_keys["session_2"] == "key_2"

    def test_api_key_retrieval(self):
        """Test API key retrieval from session"""
        from openapi_mcp_server import get_session_api_key, session_api_keys

        session_api_keys.clear()
        session_api_keys["test_session"] = "test_key_123"

        retrieved_key = get_session_api_key()
        assert retrieved_key == "test_key_123"


class TestOpenAPISpecParsing:
    """Test suite for OpenAPI spec parsing"""

    def test_parse_minimal_spec(self, tmp_path):
        """Test parsing minimal valid OpenAPI spec"""
        from openapi_parser import load_openapi_spec

        spec_file = tmp_path / "openapi.json"
        spec_data = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {}
        }
        import json
        spec_file.write_text(json.dumps(spec_data))

        parser = load_openapi_spec(str(spec_file))
        operations = parser.extract_operations()

        assert operations == []

    def test_parse_spec_with_operations(self, tmp_path):
        """Test parsing spec with operations"""
        from openapi_parser import load_openapi_spec

        spec_file = tmp_path / "openapi.json"
        spec_data = {
            "openapi": "3.0.0",
            "info": {"title": "Test API", "version": "1.0.0"},
            "paths": {
                "/users": {
                    "get": {
                        "operationId": "list_users",
                        "summary": "List all users",
                        "responses": {"200": {"description": "Success"}}
                    },
                    "post": {
                        "operationId": "create_user",
                        "summary": "Create a user",
                        "responses": {"201": {"description": "Created"}}
                    }
                }
            }
        }
        import json
        spec_file.write_text(json.dumps(spec_data))

        parser = load_openapi_spec(str(spec_file))
        operations = parser.extract_operations()

        assert len(operations) == 2
        operation_ids = [op.operation_id for op in operations]
        assert "list_users" in operation_ids
        assert "create_user" in operation_ids

    def test_parse_spec_with_parameters(self, tmp_path):
        """Test parsing spec with path parameters"""
        from openapi_parser import load_openapi_spec

        spec_file = tmp_path / "openapi.json"
        spec_data = {
            "openapi": "3.0.0",
            "paths": {
                "/users/{id}": {
                    "get": {
                        "operationId": "get_user",
                        "parameters": [
                            {
                                "name": "id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "string"}
                            }
                        ],
                        "responses": {"200": {"description": "Success"}}
                    }
                }
            }
        }
        import json
        spec_file.write_text(json.dumps(spec_data))

        parser = load_openapi_spec(str(spec_file))
        operations = parser.extract_operations()

        assert len(operations) == 1
        assert len(operations[0].parameters) == 1
        assert operations[0].parameters[0]["name"] == "id"


class TestEndToEndFunctionality:
    """End-to-end tests for complete workflows"""

    def test_complete_tool_generation_workflow(self, tmp_path, monkeypatch):
        """Test complete workflow from spec to tool generation"""
        # Create test OpenAPI spec
        spec_file = tmp_path / "openapi.json"
        spec_data = {
            "openapi": "3.0.0",
            "paths": {
                "/api/v1/test": {
                    "get": {
                        "operationId": "test-endpoint",
                        "summary": "Test endpoint for testing",
                        "description": "A test endpoint",
                        "parameters": [],
                        "responses": {"200": {"description": "Success"}},
                        "tags": ["test"]
                    }
                }
            }
        }
        import json
        spec_file.write_text(json.dumps(spec_data))

        # Load and parse
        from openapi_parser import load_openapi_spec
        from tool_generator import generate_tool_name, generate_tool_description, should_include_operation

        parser = load_openapi_spec(str(spec_file))
        operations = parser.extract_operations()

        assert len(operations) == 1
        operation = operations[0]

        # Generate tool name
        tool_name = generate_tool_name(operation)
        assert tool_name == "test_endpoint"

        # Generate tool description
        tool_description = generate_tool_description(operation)
        assert tool_description == "A test endpoint"

        # Check filtering
        assert should_include_operation(operation) is True

    def test_whitelist_reduces_registered_tools(self, tmp_path):
        """Test that whitelist reduces number of registered tools"""
        # Create spec with 10 operations
        spec_data = {
            "openapi": "3.0.0",
            "paths": {}
        }

        for i in range(10):
            spec_data["paths"][f"/api/endpoint{i}"] = {
                "get": {
                    "operationId": f"endpoint_{i}",
                    "responses": {"200": {"description": "Success"}}
                }
            }

        spec_file = tmp_path / "openapi.json"
        import json
        spec_file.write_text(json.dumps(spec_data))

        from openapi_parser import load_openapi_spec
        from tool_generator import should_include_operation

        parser = load_openapi_spec(str(spec_file))
        operations = parser.extract_operations()

        # Without whitelist: all 10 should be included
        included_without_whitelist = [op for op in operations if should_include_operation(op)]
        assert len(included_without_whitelist) == 10

        # With whitelist: only whitelisted ones
        whitelist = {
            ("GET", "/api/endpoint0"),
            ("GET", "/api/endpoint1"),
            ("GET", "/api/endpoint2")
        }
        included_with_whitelist = [
            op for op in operations
            if should_include_operation(op, whitelist=whitelist)
        ]
        assert len(included_with_whitelist) == 3


class TestErrorHandling:
    """Test suite for error handling"""

    def test_invalid_openapi_spec_handling(self, tmp_path):
        """Test handling of invalid OpenAPI spec"""
        from openapi_parser import load_openapi_spec

        spec_file = tmp_path / "invalid.json"
        spec_file.write_text("invalid json content {")

        with pytest.raises(Exception):  # Should raise JSON decode error
            load_openapi_spec(str(spec_file))

    def test_missing_required_fields_in_operation(self, tmp_path):
        """Test handling operations with missing required fields"""
        from openapi_parser import load_openapi_spec

        spec_file = tmp_path / "openapi.json"
        spec_data = {
            "openapi": "3.0.0",
            "paths": {
                "/test": {
                    "get": {
                        # Missing operationId, responses, etc
                        "summary": "Test"
                    }
                }
            }
        }
        import json
        spec_file.write_text(json.dumps(spec_data))

        parser = load_openapi_spec(str(spec_file))
        operations = parser.extract_operations()

        # Should still parse, with None/empty values
        assert len(operations) == 1
        assert operations[0].operation_id is None


class TestPerformance:
    """Test suite for performance characteristics"""

    def test_whitelist_filtering_performance(self):
        """Test that whitelist filtering is performant"""
        from openapi_parser import Operation
        from tool_generator import should_include_operation
        import time

        # Create large whitelist
        whitelist = {(f"GET", f"/api/endpoint{i}") for i in range(1000)}

        # Create test operations
        operations = [
            Operation(
                path=f"/api/endpoint{i}",
                method="GET",
                operation_id=f"op_{i}",
                summary=None,
                description=None,
                parameters=[],
                request_body=None,
                responses={},
                tags=[]
            )
            for i in range(1000)
        ]

        # Time the filtering
        start = time.time()
        filtered = [op for op in operations if should_include_operation(op, whitelist=whitelist)]
        duration = time.time() - start

        # Should complete in under 1 second
        assert duration < 1.0
        assert len(filtered) == 1000

    def test_tool_name_generation_performance(self):
        """Test that tool name generation is performant"""
        from openapi_parser import Operation
        from tool_generator import generate_tool_name
        import time

        operations = [
            Operation(
                path=f"/api/v1/users/{i}/posts/{i}/comments",
                method="GET",
                operation_id=f"get-user-{i}-posts-comments",
                summary=None,
                description=None,
                parameters=[],
                request_body=None,
                responses={},
                tags=[]
            )
            for i in range(100)
        ]

        start = time.time()
        tool_names = [generate_tool_name(op) for op in operations]
        duration = time.time() - start

        # Should complete quickly
        assert duration < 0.5
        assert len(tool_names) == 100
        assert all(isinstance(name, str) for name in tool_names)
