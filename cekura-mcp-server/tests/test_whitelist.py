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

    @pytest.mark.parametrize("method,path", [
        # Scenario instruction generate / improve
        ("POST", "/test_framework/v1/scenarios/improve_instructions"),
        ("GET", "/test_framework/v1/scenarios/instructions-progress"),
        ("POST", "/test_framework/v1/scenarios/generate-instructions"),
        ("POST", "/test_framework/v1/scenarios/{id}/update_scenario_with_transcript"),
        # Metric generate / simplify
        ("POST", "/test_framework/v1/metrics/generate_clean_description"),
        ("POST", "/test_framework/v1/metrics/generate_evaluation_trigger"),
        ("POST", "/test_framework/v1/metrics/simplify_prompt"),
        ("GET", "/test_framework/v1/metrics/simplify_prompt_progress"),
        # Run improve_prompt full workflow
        ("POST", "/test_framework/v1/runs/improve_prompt_bg"),
        ("GET", "/test_framework/v1/runs/improve_prompt_progress"),
        ("POST", "/test_framework/v1/runs/improve_prompt_issues"),
        # Call-logs improve_prompt full workflow
        ("POST", "/observability/v1/call-logs/improve_prompt"),
        ("POST", "/observability/v1/call-logs/improve_prompt_bg"),
        ("GET", "/observability/v1/call-logs/improve_prompt_progress"),
        ("POST", "/observability/v1/call-logs/improve_prompt_issues"),
        # Result triage
        ("POST", "/test_framework/v1/results/{id}/ask_about_failures"),
        # Scenario-improvement-sessions CRUD
        ("GET", "/test_framework/v1/scenario-improvement-sessions"),
        ("POST", "/test_framework/v1/scenario-improvement-sessions"),
        ("GET", "/test_framework/v1/scenario-improvement-sessions/{id}"),
        ("PATCH", "/test_framework/v1/scenario-improvement-sessions/{id}"),
    ])
    def test_improve_generate_endpoints_in_whitelist(self, method, path):
        """Assert every improve/generate endpoint we intend to expose via MCP is on
        the documented-API allow-list (i.e. an MDX page exists + mint.json references it)."""
        whitelist = load_documented_apis_whitelist()
        assert (method, path) in whitelist, (
            f"{method} {path} missing from documented_apis.json — "
            f"its MDX page or mint.json entry is likely absent. "
            f"Re-run extract_documented_apis.py."
        )
