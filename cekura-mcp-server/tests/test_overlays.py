"""Overlay drift tests — picked up by the existing CI pytest jobs.

Fails fast if `tool_overlays.json` has drifted from the live `openapi.json`:
orphan entries, stale required-field lists, stale example keys, or DELETE tools
missing the destructive marker.

Run manually: pytest tests/test_overlays.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
ROOT = HERE.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from validate_overlays import run_checks, Finding


@pytest.fixture(scope="module")
def findings():
    return run_checks()


def _errors(findings):
    return [f for f in findings if f.level == "error"]


def _warnings(findings):
    return [f for f in findings if f.level == "warning"]


def test_no_error_level_drift(findings):
    errors = _errors(findings)
    if errors:
        msg = "\n".join(f"[{f.category}] {f.tool}: {f.message}" for f in errors)
        pytest.fail(f"Overlay drift detected:\n{msg}")


def test_no_orphan_overlays(findings):
    orphans = [f for f in findings if f.category == "orphan"]
    assert not orphans, (
        "Orphaned overlay entries (tool was renamed or removed upstream): "
        + ", ".join(f.tool for f in orphans)
    )


def test_required_fields_match_schema(findings):
    broken = [f for f in findings if f.category == "missing_required_field"]
    assert not broken, (
        "Overlay 'required' lists reference fields not in the tool schema: "
        + "; ".join(f"{f.tool} -> {f.message}" for f in broken)
    )


def test_every_delete_tool_marked_destructive(findings):
    broken = [f for f in findings if f.category == "destructive_missing"]
    assert not broken, (
        "DELETE / *_destroy tools missing 'destructive: true' in the overlay: "
        + ", ".join(f.tool for f in broken)
    )


def test_example_request_fields_exist(findings):
    # stale examples are warnings (not fatal) but still worth surfacing in tests
    stale = [f for f in findings if f.category == "stale_example_field"]
    if stale:
        pytest.fail(
            "Overlay example_request keys reference fields removed upstream: "
            + "; ".join(f"{f.tool} -> {f.message}" for f in stale)
        )
