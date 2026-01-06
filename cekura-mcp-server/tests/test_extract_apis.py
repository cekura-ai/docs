"""Tests for API extraction from mint.json"""
import pytest
import json
from pathlib import Path
import tempfile
from extract_documented_apis import (
    extract_api_paths_from_mint,
    extract_openapi_from_mdx,
    normalize_path,
    _extract_pages_recursive,
)


class TestExtractAPIPathsFromMint:
    """Test suite for extracting API paths from mint.json"""

    def test_extract_simple_structure(self, tmp_path):
        """Test extracting from simple mint.json structure"""
        mint_data = {
            "navigation": [
                {
                    "group": "API Reference",
                    "pages": [
                        "api-reference/users/list-users",
                        "api-reference/users/create-user",
                    ]
                }
            ]
        }

        mint_file = tmp_path / "mint.json"
        mint_file.write_text(json.dumps(mint_data))

        result = extract_api_paths_from_mint(str(mint_file))
        assert len(result) == 2
        assert "api-reference/users/list-users" in result
        assert "api-reference/users/create-user" in result

    def test_extract_nested_structure(self, tmp_path):
        """Test extracting from nested mint.json structure"""
        mint_data = {
            "navigation": [
                {
                    "group": "API Reference",
                    "pages": [
                        {
                            "group": "Users",
                            "pages": [
                                "api-reference/users/list-users",
                                "api-reference/users/create-user",
                            ]
                        },
                        {
                            "group": "Projects",
                            "pages": [
                                "api-reference/projects/list-projects",
                            ]
                        }
                    ]
                }
            ]
        }

        mint_file = tmp_path / "mint.json"
        mint_file.write_text(json.dumps(mint_data))

        result = extract_api_paths_from_mint(str(mint_file))
        assert len(result) == 3

    def test_extract_no_api_reference_section(self, tmp_path):
        """Test when there's no API Reference section"""
        mint_data = {
            "navigation": [
                {
                    "group": "Documentation",
                    "pages": ["docs/intro"]
                }
            ]
        }

        mint_file = tmp_path / "mint.json"
        mint_file.write_text(json.dumps(mint_data))

        result = extract_api_paths_from_mint(str(mint_file))
        assert len(result) == 0


class TestExtractOpenAPIFromMDX:
    """Test suite for extracting OpenAPI info from MDX files"""

    def test_extract_valid_mdx(self, tmp_path):
        """Test extracting from valid MDX file"""
        mdx_content = """---
title: List Users
openapi: get /api/v1/users
slug: list-users
---

# List Users

Description here
"""
        mdx_file = tmp_path / "list-users.mdx"
        mdx_file.write_text(mdx_content)

        result = extract_openapi_from_mdx(mdx_file)
        assert result is not None
        assert result['method'] == 'GET'
        assert result['path'] == '/api/v1/users'

    def test_extract_mdx_with_quotes(self, tmp_path):
        """Test extracting from MDX with quoted openapi field"""
        mdx_content = """---
title: Create User
openapi: "post /api/v1/users"
---
"""
        mdx_file = tmp_path / "create-user.mdx"
        mdx_file.write_text(mdx_content)

        result = extract_openapi_from_mdx(mdx_file)
        assert result is not None
        assert result['method'] == 'POST'
        assert result['path'] == '/api/v1/users'

    def test_extract_mdx_no_frontmatter(self, tmp_path):
        """Test MDX without frontmatter"""
        mdx_content = """# Just a regular markdown file
No frontmatter here
"""
        mdx_file = tmp_path / "no-frontmatter.mdx"
        mdx_file.write_text(mdx_content)

        result = extract_openapi_from_mdx(mdx_file)
        assert result is None

    def test_extract_mdx_no_openapi_field(self, tmp_path):
        """Test MDX without openapi field"""
        mdx_content = """---
title: Some Doc
slug: some-doc
---

Content here
"""
        mdx_file = tmp_path / "no-openapi.mdx"
        mdx_file.write_text(mdx_content)

        result = extract_openapi_from_mdx(mdx_file)
        assert result is None


class TestNormalizePath:
    """Test suite for path normalization"""

    def test_normalize_path_with_trailing_slash(self):
        """Test removing trailing slash"""
        result = normalize_path("/api/v1/users/")
        assert result == "/api/v1/users"

    def test_normalize_path_add_leading_slash(self):
        """Test adding leading slash"""
        result = normalize_path("api/v1/users")
        assert result == "/api/v1/users"

    def test_normalize_path_already_normalized(self):
        """Test path that's already normalized"""
        result = normalize_path("/api/v1/users")
        assert result == "/api/v1/users"

    def test_normalize_root_path(self):
        """Test normalizing root path"""
        result = normalize_path("/")
        assert result == "/"


class TestExtractPagesRecursive:
    """Test suite for recursive page extraction"""

    def test_extract_flat_list(self):
        """Test extracting from flat list"""
        pages = [
            "api-reference/users/list",
            "api-reference/users/create"
        ]

        result = _extract_pages_recursive(pages)
        assert len(result) == 2
        assert "api-reference/users/list" in result

    def test_extract_nested_list(self):
        """Test extracting from nested list"""
        pages = [
            {
                "group": "Users",
                "pages": [
                    "api-reference/users/list",
                    "api-reference/users/create"
                ]
            }
        ]

        result = _extract_pages_recursive(pages)
        assert len(result) == 2

    def test_extract_mixed_list(self):
        """Test extracting from mixed list"""
        pages = [
            "api-reference/auth/login",
            {
                "group": "Users",
                "pages": [
                    "api-reference/users/list"
                ]
            }
        ]

        result = _extract_pages_recursive(pages)
        assert len(result) == 2

    def test_extract_deeply_nested(self):
        """Test extracting from deeply nested structure"""
        pages = [
            {
                "group": "API",
                "pages": [
                    {
                        "group": "Users",
                        "pages": [
                            "api-reference/users/list"
                        ]
                    }
                ]
            }
        ]

        result = _extract_pages_recursive(pages)
        assert len(result) == 1
        assert "api-reference/users/list" in result
