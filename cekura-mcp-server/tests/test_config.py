"""Tests for configuration module"""
import os
import pytest
import tempfile
from pathlib import Path
from config import load_config, MCPServerConfig, _parse_list_env, _parse_int_env


class TestConfig:
    """Test suite for configuration management"""

    def test_load_config_with_required_fields(self, monkeypatch, tmp_path):
        """Test loading config with all required fields"""
        # Create temporary OpenAPI spec file
        spec_file = tmp_path / "test_openapi.json"
        spec_file.write_text('{"openapi": "3.0.0"}')

        monkeypatch.setenv("CEKURA_BASE_URL", "https://api.test.com")
        monkeypatch.setenv("CEKURA_OPENAPI_SPEC", str(spec_file))

        config = load_config()
        assert config.base_url == "https://api.test.com"
        assert config.openapi_spec_path == str(spec_file)

    def test_load_config_missing_base_url(self, monkeypatch):
        """Test that missing base URL raises error"""
        from pydantic import ValidationError
        monkeypatch.delenv("CEKURA_BASE_URL", raising=False)
        monkeypatch.delenv("CEKURA_OPENAPI_SPEC", raising=False)

        with pytest.raises((RuntimeError, ValidationError)):
            load_config()

    def test_load_config_invalid_url_format(self, monkeypatch, tmp_path):
        """Test that invalid URL format raises error"""
        from pydantic import ValidationError
        spec_file = tmp_path / "test_openapi.json"
        spec_file.write_text('{"openapi": "3.0.0"}')

        # Clear any existing env vars first
        monkeypatch.delenv("CEKURA_BASE_URL", raising=False)
        monkeypatch.delenv("CEKURA_OPENAPI_SPEC", raising=False)

        monkeypatch.setenv("CEKURA_BASE_URL", "invalid-url")
        monkeypatch.setenv("CEKURA_OPENAPI_SPEC", str(spec_file))

        with pytest.raises((RuntimeError, ValidationError)):
            load_config()

    def test_load_config_missing_spec_file(self, monkeypatch):
        """Test that missing spec file raises error"""
        from pydantic import ValidationError

        # Clear any existing env vars first
        monkeypatch.delenv("CEKURA_BASE_URL", raising=False)
        monkeypatch.delenv("CEKURA_OPENAPI_SPEC", raising=False)

        monkeypatch.setenv("CEKURA_BASE_URL", "https://api.test.com")
        monkeypatch.setenv("CEKURA_OPENAPI_SPEC", "/nonexistent/spec.json")

        with pytest.raises((RuntimeError, ValidationError)):
            load_config()

    def test_load_config_with_optional_fields(self, monkeypatch, tmp_path):
        """Test loading config with optional fields"""
        spec_file = tmp_path / "test_openapi.json"
        spec_file.write_text('{"openapi": "3.0.0"}')

        monkeypatch.setenv("CEKURA_BASE_URL", "https://api.test.com")
        monkeypatch.setenv("CEKURA_OPENAPI_SPEC", str(spec_file))
        monkeypatch.setenv("CEKURA_FILTER_TAGS", "tag1,tag2,tag3")
        monkeypatch.setenv("CEKURA_EXCLUDE_OPERATIONS", "op1,op2")
        monkeypatch.setenv("CEKURA_MAX_TOOLS", "50")

        config = load_config()
        assert config.filter_tags == ["tag1", "tag2", "tag3"]
        assert config.exclude_operations == ["op1", "op2"]
        assert config.max_tools == 50

    def test_parse_list_env_valid(self, monkeypatch):
        """Test parsing comma-separated list"""
        monkeypatch.setenv("TEST_LIST", "item1, item2, item3")
        result = _parse_list_env("TEST_LIST")
        assert result == ["item1", "item2", "item3"]

    def test_parse_list_env_empty(self, monkeypatch):
        """Test parsing empty env var"""
        monkeypatch.delenv("TEST_LIST", raising=False)
        result = _parse_list_env("TEST_LIST")
        assert result is None

    def test_parse_int_env_valid(self, monkeypatch):
        """Test parsing integer"""
        monkeypatch.setenv("TEST_INT", "42")
        result = _parse_int_env("TEST_INT")
        assert result == 42

    def test_parse_int_env_invalid(self, monkeypatch):
        """Test parsing invalid integer"""
        monkeypatch.setenv("TEST_INT", "not_a_number")
        with pytest.raises(ValueError):
            _parse_int_env("TEST_INT")

    def test_url_trailing_slash_removed(self, monkeypatch, tmp_path):
        """Test that trailing slash is removed from URL"""
        spec_file = tmp_path / "test_openapi.json"
        spec_file.write_text('{"openapi": "3.0.0"}')

        monkeypatch.setenv("CEKURA_BASE_URL", "https://api.test.com/")
        monkeypatch.setenv("CEKURA_OPENAPI_SPEC", str(spec_file))

        config = load_config()
        assert config.base_url == "https://api.test.com"
