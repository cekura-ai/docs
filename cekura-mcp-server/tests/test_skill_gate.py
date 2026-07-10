"""Tests for the client-cooperative skill gate (through the `warn` mode).

Covers the family/tool table, the mode-by-mode decision, sandbox bypass,
schema injection, the append-only manifest + baked fallback, and the hard
no-regression invariant: `skill_ack` is stripped before dispatch so it never
reaches the backend.
"""
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pytest

import skill_gate
from openapi_mcp_server import _dispatch_args, _upgrade_skills_reliable, MCP_INSTRUCTIONS


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
        # the nudge must instruct the model to surface the install path to the user
        assert "docs.cekura.ai/mcp/overview" in d.nudge
        assert "user" in d.nudge

    def test_initialize_instructions_direct_the_model_to_tell_the_user(self):
        # the session-start instructions must push the model to surface the
        # install path to the user, not just note it, and carry the docs link
        assert "tell the user" in MCP_INSTRUCTIONS.lower()
        assert "docs.cekura.ai/mcp/overview" in MCP_INSTRUCTIONS
        assert "once per session" in MCP_INSTRUCTIONS.lower()

    def test_initialize_instructions_name_no_tools_or_mechanism(self):
        # instructions stay high-level: no tool names, no gate mechanism leaked
        for token in ("cekura_load_skill", "aiagents_retrieve", "skill_ack",
                      "metrics_create", "scenarios_create", "proceed-without-skill"):
            assert token not in MCP_INSTRUCTIONS, f"instructions should not mention {token}"

    def test_upgrade_skills_reliable_threshold(self):
        # /upgrade-skills reliably moves the version pin only from 0.8.1 on;
        # older or unknown versions must reinstall instead.
        assert _upgrade_skills_reliable("0.8.1") is True
        assert _upgrade_skills_reliable("0.9.0") is True
        assert _upgrade_skills_reliable("1.0.0") is True
        assert _upgrade_skills_reliable("0.8.0") is False
        assert _upgrade_skills_reliable("0.4.2") is False
        assert _upgrade_skills_reliable("0.1.1") is False
        assert _upgrade_skills_reliable(None) is False
        assert _upgrade_skills_reliable("") is False

    def test_valid_ack_allows(self):
        d = skill_gate.evaluate("scenarios_create", VALID_TAG, "warn")
        assert d.action == "allow" and d.ack_valid is True

    def test_ack_from_sibling_family_member_allows(self):
        # a command tag in the same family satisfies the write
        d = skill_gate.evaluate("scenarios_create", "ack:manual-create-update-eval:5m4p7c", "enforce")
        assert d.action == "allow" and d.ack_valid is True

    def test_wrong_family_ack_allows_but_is_flagged(self):
        # a recognized tag from another family still proves a playbook is in
        # context: allow (never deny an installed user), but mark the mismatch
        for mode in ("warn", "enforce", "shadow"):
            d = skill_gate.evaluate("scenarios_create", METRIC_TAG, mode)
            assert d.action == "allow" and d.reason == "ack_wrong_family"
            assert d.ack_valid is False and d.nudge is None

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
        # both recovery paths must tell the model to keep passing the value on later calls
        assert "subsequent write call" in d.nudge
        # already-installed users get a recovery path that doesn't say "install"
        assert "already installed" in d.nudge
        # a choice made earlier in the conversation must not trigger a re-ask
        assert "without asking again" in d.nudge

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
        for slug in skill_gate.ALL_FAMILY_SLUGS:
            assert slug in m

    def test_loadable_skills_derived_from_families(self):
        # loadable = the skill-backed subset of the family slugs (commands ship
        # only inside the plugin and have no SKILL.md to deliver)
        assert set(skill_gate.LOADABLE_SKILLS) <= skill_gate.ALL_FAMILY_SLUGS
        assert len(skill_gate.LOADABLE_SKILLS) == 7


# ── apply_gate: the complete handler-side step ───────────────────────────────

class TestApplyGate:
    def test_off_mode_still_strips(self):
        args = {"name": "x", "skill_ack": VALID_TAG}
        deny, nudge = skill_gate.apply_gate("scenarios_create", args, "off")
        assert deny is None and nudge is None
        assert "skill_ack" not in args

    def test_non_gated_tool_strips_and_allows_even_in_enforce(self):
        args = {"query": "q", "skill_ack": "whatever"}
        deny, nudge = skill_gate.apply_gate("scenarios_list", args, "enforce")
        assert deny is None and nudge is None
        assert "skill_ack" not in args

    def test_enforce_denies_without_ack(self):
        args = {"name": "x"}
        deny, nudge = skill_gate.apply_gate("metrics_create", args, "enforce")
        assert deny is not None and "NOT executed" in deny
        assert nudge is None
        assert args == {"name": "x"}  # other args untouched

    def test_enforce_allows_valid_ack(self):
        args = {"name": "x", "skill_ack": VALID_TAG}
        deny, nudge = skill_gate.apply_gate("scenarios_create", args, "enforce")
        assert deny is None and nudge is None
        assert "skill_ack" not in args

    def test_enforce_allows_user_override(self):
        args = {"name": "x", "skill_ack": skill_gate.OVERRIDE_ACK}
        deny, nudge = skill_gate.apply_gate("scenarios_create", args, "enforce")
        assert deny is None and nudge is None

    def test_warn_returns_nudge_and_proceeds(self):
        deny, nudge = skill_gate.apply_gate("metrics_create", {}, "warn")
        assert deny is None
        assert nudge and "cekura_load_skill" in nudge

    def test_sandbox_bypasses_enforce(self):
        deny, nudge = skill_gate.apply_gate("scenarios_create", {}, "enforce", is_sandbox=True)
        assert deny is None and nudge is None

    def test_fails_open_on_internal_error(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(skill_gate, "evaluate", boom)
        args = {"name": "x", "skill_ack": "junk"}
        deny, nudge = skill_gate.apply_gate("scenarios_create", args, "enforce")
        assert deny is None and nudge is None  # write proceeds
        assert "skill_ack" not in args         # strip happened before the failure

    def test_none_arguments_tolerated(self):
        deny, nudge = skill_gate.apply_gate("scenarios_create", None, "enforce")
        assert deny is not None  # gated, no ack -> deny; and no crash on None args


# ── log-event contract (Datadog dashboards key off these exact shapes) ───────

class TestGateLogEvents:
    def _events(self, caplog):
        out = []
        for record in caplog.records:
            msg = record.getMessage()
            if msg.startswith("{"):
                out.append(json.loads(msg))
        return out

    def _run(self, caplog, tool, args, mode, **kw):
        kw.setdefault("client_id", "test-client/1.0")
        kw.setdefault("call_id", "call_test123")
        kw.setdefault("cred_hash_fn", lambda: "cafecafecafecafe")
        with caplog.at_level(logging.INFO, logger="skill_gate"):
            skill_gate.apply_gate(tool, args, mode, **kw)
        return self._events(caplog)

    def test_deny_event_shape(self, caplog):
        (e,) = self._run(caplog, "metrics_create", {}, "enforce")
        assert e == {
            "event": "skill_gate_blocked",
            "tool": "metrics_create",
            "family": "metric-design",
            "call_id": "call_test123",
            "mode": "enforce",
            "client_id": "test-client/1.0",
            "cred_hash": "cafecafecafecafe",
            "ack_present": False,
            "blocked": True,
        }

    def test_shadow_event_shape(self, caplog):
        (e,) = self._run(caplog, "scenarios_create", {"skill_ack": "junk"}, "shadow")
        assert e["event"] == "skill_gate_blocked"
        assert e["blocked"] is False and e["ack_present"] is True
        assert e["mode"] == "shadow" and e["family"] == "eval-design"

    def test_override_event_shape(self, caplog):
        (e,) = self._run(
            caplog, "scenarios_create", {"skill_ack": skill_gate.OVERRIDE_ACK}, "enforce"
        )
        assert e["event"] == "skill_gate_user_override"
        assert e["client_id"] == "test-client/1.0" and e["cred_hash"] == "cafecafecafecafe"
        # override events do not carry the ack_present/blocked pair
        assert "ack_present" not in e and "blocked" not in e

    def test_ack_ok_event_shape(self, caplog):
        (e,) = self._run(caplog, "scenarios_create", {"skill_ack": VALID_TAG}, "warn")
        assert e["event"] == "skill_gate_ack_ok" and e["mode"] == "warn"
        # thread-rate signal is anonymous: no identity fields
        assert "client_id" not in e and "cred_hash" not in e

    def test_wrong_family_event_shape(self, caplog):
        (e,) = self._run(caplog, "scenarios_create", {"skill_ack": METRIC_TAG}, "enforce")
        assert e["event"] == "skill_gate_ack_wrong_family"
        assert e["family"] == "eval-design" and e["mode"] == "enforce"
        assert "client_id" not in e and "cred_hash" not in e

    def test_sandbox_event_shape(self, caplog):
        (e,) = self._run(caplog, "scenarios_create", {}, "enforce", is_sandbox=True)
        assert e["event"] == "skill_gate_sandbox_bypass"
        assert "mode" not in e  # bypass is mode-independent

    def test_off_and_non_gated_log_nothing(self, caplog):
        assert self._run(caplog, "metrics_create", {}, "off") == []
        caplog.clear()
        assert self._run(caplog, "scenarios_list", {}, "enforce") == []


# ── no-regression: skill_ack never reaches the backend ───────────────────────

class TestSkillAckStripped:
    def test_stripped_before_object_body_dispatch(self):
        op = FakeOp(path="/scenarios/", request_body=JSON_OBJECT_BODY)
        args = {"name": "x", "project": 5, "skill_ack": VALID_TAG}
        skill_gate.apply_gate("scenarios_create", args, "warn")
        path, query, body = _dispatch_args(op, args)
        assert "skill_ack" not in (body or {})
        assert "skill_ack" not in query
        assert body == {"name": "x", "project": 5}

    def test_stripped_before_array_body_dispatch(self):
        # metrics_bulk_create: top-level array body via `items`
        op = FakeOp(path="/metrics/bulk/", request_body=JSON_ARRAY_BODY)
        args = {"items": [{"name": "m1"}], "skill_ack": METRIC_TAG}
        skill_gate.apply_gate("metrics_bulk_create", args, "shadow")
        path, query, body = _dispatch_args(op, args)
        assert body == [{"name": "m1"}]  # bare array, no skill_ack anywhere

    def test_stripped_when_no_body_routes_to_query(self):
        op = FakeOp(path="/scenarios/", parameters=[{"name": "project_id", "in": "query"}])
        args = {"project_id": 5, "skill_ack": VALID_TAG}
        skill_gate.apply_gate("scenarios_create", args, "off")
        path, query, body = _dispatch_args(op, args)
        assert "skill_ack" not in query and query == {"project_id": 5}
