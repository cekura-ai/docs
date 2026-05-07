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


# ---------------------------------------------------------------------------
# Generate/improve preference suffixes — keep agents off raw create/patch
# ---------------------------------------------------------------------------

import json as _json
from pathlib import Path as _Path

_OVERLAYS_FILE = _Path(__file__).parent.parent / "tool_overlays.json"


def _load_overlays():
    with open(_OVERLAYS_FILE, "r") as fh:
        return _json.load(fh)


# Tools where reaching for a raw create/patch is almost always wrong when an
# AI-assisted generate/improve counterpart exists. Each must carry a
# `description_suffix` calling that out (see plan A4).
_RAW_TOOLS_THAT_NEED_PREFERENCE_SUFFIX = [
    "scenarios_create",
    "scenarios_partial_update",
    "metrics_create",
    "metrics_partial_update",
    "aiagents_create",
    "aiagents_partial_update",
]

# AI-assisted tools that need clarifying suffixes so agents pick them up
# correctly (e.g. `scenarios_agent_create` is misleading without the note that
# it's the unified clarify+improve endpoint).
_AI_TOOLS_THAT_NEED_CLARIFYING_SUFFIX = [
    "scenarios_agent_create",
    "scenarios_generate_bg",
    "scenarios_improve_instructions_create",
]

_PREFERENCE_KEYWORDS = ("Prefer", "preferred", "prefer")


@pytest.mark.parametrize("tool", _RAW_TOOLS_THAT_NEED_PREFERENCE_SUFFIX)
def test_raw_create_patch_tools_have_preference_suffix(tool):
    overlays = _load_overlays()
    assert tool in overlays, (
        f"{tool} must have an overlay entry steering agents to its "
        f"generate/improve counterpart."
    )
    suffix = overlays[tool].get("description_suffix", "")
    assert suffix, f"{tool} overlay must define a non-empty description_suffix."
    assert any(kw in suffix for kw in _PREFERENCE_KEYWORDS), (
        f"{tool} description_suffix must contain a 'Prefer ...' steering line; "
        f"got: {suffix[:120]!r}"
    )


@pytest.mark.parametrize("tool", _AI_TOOLS_THAT_NEED_CLARIFYING_SUFFIX)
def test_generate_improve_tools_have_clarifying_suffix(tool):
    overlays = _load_overlays()
    assert tool in overlays, (
        f"{tool} must have an overlay entry clarifying when to pick it over "
        f"raw create/patch."
    )
    suffix = overlays[tool].get("description_suffix", "")
    assert suffix, f"{tool} overlay must define a non-empty description_suffix."
