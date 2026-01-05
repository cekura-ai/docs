import pytest
from config import load_config
from openapi_parser import load_openapi_spec
from tool_generator import (
    should_include_operation,
    load_documented_apis_whitelist,
)


class TestWhitelistIntegration:
    @pytest.mark.asyncio
    async def test_whitelist_filtering_integration(self):
        server_config = load_config()
        assert server_config.base_url == "https://api.cekura.ai"

        openapi_parser = load_openapi_spec(server_config.openapi_spec_path)
        operations = openapi_parser.extract_operations()
        assert len(operations) == 622

        whitelist = load_documented_apis_whitelist()
        assert whitelist is not None
        assert len(whitelist) >= 74

        tools_to_register = []
        for operation in operations:
            if should_include_operation(
                operation,
                filter_tags=server_config.filter_tags,
                exclude_ops=server_config.exclude_operations,
                whitelist=whitelist
            ):
                tools_to_register.append(operation)

        assert len(tools_to_register) >= 74
        assert len(tools_to_register) < len(operations)

        reduction = len(operations) - len(tools_to_register)
        assert reduction > 500

    def test_whitelist_file_exists(self):
        from pathlib import Path
        whitelist_file = Path(__file__).parent.parent / 'documented_apis.json'
        assert whitelist_file.exists()

    def test_whitelist_loads_correctly(self):
        whitelist = load_documented_apis_whitelist()
        assert whitelist is not None
        assert isinstance(whitelist, set)
        assert len(whitelist) > 0

        sample_endpoint = next(iter(whitelist))
        assert isinstance(sample_endpoint, tuple)
        assert len(sample_endpoint) == 2
        method, path = sample_endpoint
        assert isinstance(method, str)
        assert isinstance(path, str)
        assert method.isupper()
        assert path.startswith('/')
