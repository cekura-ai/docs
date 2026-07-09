"""Tests for the client-cooperative skill gate (through the `warn` mode).

Covers the family/tool table, the mode-by-mode decision, sandbox bypass,
schema injection, the append-only manifest + baked fallback, and the hard
no-regression invariant: `skill_ack` is stripped before dispatch so it never
reaches the backend.
"""
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pytest

import skill_gate
from openapi_mcp_server import _dispatch_args


# ── fixtures / helpers ───────────────────────────────────────────────────────

VALID_TAG = "ack:cekura-eval-design:7k3m4q"
METRIC_TAG = "ack:cekura-metric-design:6n2q5r"


@pytest.fixture(autouse=True)
def _manifest():
    """Give every test a known manifest; restore nothing (each test sets its own)."""
    skill_gate.set_manifest({
        "cekura-eval-design": [VALID_TAG],
        "manual-create-update-eval": ["ack:manual-create-update-eval:5m4p7c"],
        "cekura-metric-design": [METRIC_TAG],
    })
    yield


@dataclass
class FakeOp:
    path: str
    parameters: Optional[List[Dict[str, Any]]] = None
    request_body: Optional[Dict[str, Any]] = None


JSON_OBJECT_BODY = {"content": {"application/json": {"schema": {"$ref": "#/x"}}}}
JSON_ARRAY_BODY = {"content": {"application/json": {"schema": {"type": "array", "items": {"type": "object"}}}}}


# ── family / tool table ──────────────────────────────────────────────────────

class TestFamilyTable:
    def test_exactly_eleven_gated_tools(self):
        assert len(skill_gate.GATED_TOOLS) == 11

    def test_every_gated_tool_maps_to_a_family(self):
        for tool in skill_gate.GATED_TOOLS:
            assert skill_gate._family_for_tool(tool) is not None

    def test_generation_and_read_tools_are_not_gated(self):
        for tool in (
            "metrics_generate",
            "scenarios_generate_bg",
            "scenarios_agent_create",
            "metric_failure_mode_insights_generate_scenario_create",
            "predefined_metrics_copy_create",
            "scenarios_list",
            "metrics_retrieve",
        ):
            assert tool not in skill_gate.GATED_TOOLS


# ── decision by mode ─────────────────────────────────────────────────────────

class TestEvaluate:
    def test_non_gated_tool_always_allowed(self):
        d = skill_gate.evaluate("scenarios_list", None, "warn")
        assert d.action == "allow" and d.reason == "not_gated"

    def test_off_is_inert_even_for_gated_tool(self):
        d = skill_gate.evaluate("scenarios_create", None, "off")
        assert d.action == "allow" and d.reason == "mode_off"

    def test_shadow_missing_ack_would_block(self):
        d = skill_gate.evaluate("scenarios_create", None, "shadow")
        assert d.action == "shadow_block" and d.family == "eval-design"

    def test_warn_missing_ack_proceeds_with_nudge(self):
        d = skill_gate.evaluate("scenarios_create", None, "warn")
        assert d.action == "warn"
        assert d.nudge and "cekura_load_skill" in d.nudge

    def test_valid_ack_allows(self):
        d = skill_gate.evaluate("scenarios_create", VALID_TAG, "warn")
        assert d.action == "allow" and d.ack_valid is True

    def test_ack_from_sibling_family_member_allows(self):
        # a command tag in the same family satisfies the write
        d = skill_gate.evaluate("scenarios_create", "ack:manual-create-update-eval:5m4p7c", "enforce")
        assert d.action == "allow" and d.ack_valid is True

    def test_wrong_family_ack_does_not_satisfy(self):
        # a metric-family tag must NOT satisfy an eval-family write
        d = skill_gate.evaluate("scenarios_create", METRIC_TAG, "warn")
        assert d.action == "warn" and d.ack_valid is False

    def test_unknown_ack_shadow_would_block(self):
        d = skill_gate.evaluate("metrics_create", "ack:bogus:zzzzzz", "shadow")
        assert d.action == "shadow_block"

    def test_enforce_and_strict_deny_without_ack(self):
        for mode in ("enforce", "strict"):
            d = skill_gate.evaluate("metrics_create", None, mode)
            assert d.action == "deny"

    def test_enforce_valid_ack_allows(self):
        d = skill_gate.evaluate("scenarios_create", VALID_TAG, "enforce")
        assert d.action == "allow" and d.ack_valid is True

    def test_enforce_user_override_allows(self):
        d = skill_gate.evaluate("scenarios_create", skill_gate.OVERRIDE_ACK, "enforce")
        assert d.action == "allow" and d.reason == "user_override"

    def test_override_sentinel_is_not_special_outside_blocking_modes(self):
        # the override only applies in enforce/strict; in warn everything proceeds
        d = skill_gate.evaluate("scenarios_create", skill_gate.OVERRIDE_ACK, "warn")
        assert d.action == "warn" and d.reason == "warn"

    def test_deny_text_offers_both_paths(self):
        d = skill_gate.evaluate("metrics_create", None, "enforce")
        assert "Ask the user" in d.nudge
        assert "cekura_load_skill" in d.nudge
        assert "docs.cekura.ai/mcp/overview" in d.nudge
        assert 'skill_ack="proceed-without-skill"' in d.nudge
        assert "NOT executed" in d.nudge

    def test_sandbox_bypass(self):
        d = skill_gate.evaluate("scenarios_create", None, "enforce", is_sandbox=True)
        assert d.action == "allow" and d.reason == "sandbox_bypass"

    def test_does_not_raise_on_odd_input(self):
        # fail-open safety: pure function must not raise on empty manifest / junk
        skill_gate.set_manifest({})
        assert skill_gate.evaluate("scenarios_create", "", "warn").action == "warn"
        assert skill_gate.evaluate("not_a_tool", None, "warn").action == "allow"


# ── schema injection ─────────────────────────────────────────────────────────

class TestInjection:
    def _schema(self):
        return {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}

    def test_off_injects_nothing(self):
        out = skill_gate.maybe_inject_skill_ack("scenarios_create", self._schema(), "off")
        assert "skill_ack" not in out["properties"]

    def test_warn_injects_optional_skill_ack(self):
        original = self._schema()
        out = skill_gate.maybe_inject_skill_ack("scenarios_create", original, "warn")
        assert out["properties"]["skill_ack"]["type"] == "string"
        assert "skill_ack" not in out.get("required", [])
        # original object is not mutated
        assert "skill_ack" not in original["properties"]

    def test_non_gated_tool_not_injected(self):
        out = skill_gate.maybe_inject_skill_ack("aiagents_create", self._schema(), "warn")
        assert "skill_ack" not in out["properties"]

    def test_idempotent(self):
        once = skill_gate.maybe_inject_skill_ack("metrics_create", self._schema(), "warn")
        twice = skill_gate.maybe_inject_skill_ack("metrics_create", once, "warn")
        assert list(twice["properties"]).count("skill_ack") == 1


# ── manifest ─────────────────────────────────────────────────────────────────

class TestManifest:
    def test_current_tag_is_last_appended(self):
        skill_gate.set_manifest({"cekura-eval-design": ["ack:cekura-eval-design:old111", VALID_TAG]})
        assert skill_gate.current_tag_for_slug("cekura-eval-design") == VALID_TAG

    def test_current_tag_unknown_slug(self):
        assert skill_gate.current_tag_for_slug("nope") is None

    def test_baked_snapshot_loads(self):
        source = skill_gate._load_baked()
        assert source == "baked"
        m = skill_gate.get_manifest()
        assert len(m) == 11
        # every family slug in the code table is present in the shipped snapshot
        for slug in (skill_gate.EVAL_DESIGN_SLUGS | skill_gate.METRIC_DESIGN_SLUGS):
            assert slug in m


# ── no-regression: skill_ack never reaches the backend ───────────────────────

class TestSkillAckStripped:
    def test_stripped_before_object_body_dispatch(self):
        op = FakeOp(path="/scenarios/", request_body=JSON_OBJECT_BODY)
        args = {"name": "x", "project": 5, "skill_ack": VALID_TAG}
        args.pop("skill_ack", None)  # mirrors call_tool_with_dynamic
        path, query, body = _dispatch_args(op, args)
        assert "skill_ack" not in (body or {})
        assert "skill_ack" not in query
        assert body == {"name": "x", "project": 5}

    def test_stripped_before_array_body_dispatch(self):
        # metrics_bulk_create: top-level array body via `items`
        op = FakeOp(path="/metrics/bulk/", request_body=JSON_ARRAY_BODY)
        args = {"items": [{"name": "m1"}], "skill_ack": METRIC_TAG}
        args.pop("skill_ack", None)
        path, query, body = _dispatch_args(op, args)
        assert body == [{"name": "m1"}]  # bare array, no skill_ack anywhere

    def test_stripped_when_no_body_routes_to_query(self):
        op = FakeOp(path="/scenarios/", parameters=[{"name": "project_id", "in": "query"}])
        args = {"project_id": 5, "skill_ack": VALID_TAG}
        args.pop("skill_ack", None)
        path, query, body = _dispatch_args(op, args)
        assert "skill_ack" not in query and query == {"project_id": 5}
