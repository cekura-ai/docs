"""Tests for the plugin-version freshness comparison.

The skill beacon reports whatever precision the installed skills carry
(major.minor from cekura-skills 0.11 on, full semver before that), so the
nudge must compare at the reported precision instead of assuming semver.
"""
import pytest

from openapi_mcp_server import _is_older_version, _latest_plugin_version


def test_latest_plugin_version_uses_current_release_by_default(monkeypatch):
    monkeypatch.delenv("CEKURA_LATEST_PLUGIN_VERSION", raising=False)

    assert _latest_plugin_version() == "0.14.2"


def test_latest_plugin_version_allows_deployment_override(monkeypatch):
    monkeypatch.setenv("CEKURA_LATEST_PLUGIN_VERSION", "0.15.0")

    assert _latest_plugin_version() == "0.15.0"


@pytest.mark.parametrize("reported,latest,expected", [
    # major.minor tags: patch releases must NOT read as an update
    ("0.10", "0.10.7", False),
    ("0.10", "0.10.0", False),
    ("0.10", "0.10", False),
    ("0.10", "0.11.0", True),
    ("0.11", "0.10.7", False),
    # full semver from older installs keeps exact comparison
    ("0.9.0", "0.10.7", True),
    ("0.10.6", "0.10.7", True),
    ("0.10.7", "0.10.7", False),
    ("0.10.8", "0.10.7", False),
    # unparseable input never nudges
    ("", "0.10.7", False),
    ("garbage", "0.10.7", False),
])
def test_is_older_version(reported, latest, expected):
    assert _is_older_version(reported, latest) is expected
