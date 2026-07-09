"""Client-cooperative skill gate for quality-critical authoring tools.

The Cekura design playbooks (scenario / test-profile / metric authoring) produce
markedly better results than raw create calls. This gate steers callers toward
them: the quality-critical write tools accept an OPTIONAL ``skill_ack`` argument
whose value is a short verification tag carried inside each design playbook.
Presenting the tag is direct evidence the playbook is in the model's context.

Two families and their write-tool sets mirror the server-side design gate; keep
them in sync by hand (families change rarely). This is a quality nudge, not a
security boundary: the tag is copyable from a public repo, so the point is to
raise the bar for honest clients, not to be uncircumventable.

Rollout ladder (env ``CEKURA_SKILL_GATE_MODE``):

* ``off``     — inert. No schema change; no evaluation. (``skill_ack`` is still
  stripped from tool args by the caller so it never reaches the backend.)
* ``shadow``  — log-only. Emits ``skill_gate_blocked`` when a gated write lacks a
  valid ack, but the call proceeds unchanged.
* ``warn``    — the call proceeds and a short nudge is appended to the response.
* ``enforce`` — the write is HELD (denied) when no valid ack is present; the deny
  instructs the model to ask the user to install/load the skills OR to explicitly
  proceed without them (by retrying with ``skill_ack="proceed-without-skill"``).
  A caller that already threaded a valid tag passes silently, unaffected.
* ``strict``  — currently uses ``enforce`` behavior; strict-specific hardening
  (tag redaction) is deferred.

Everything fails OPEN: any unexpected error should leave the write allowed.
"""

import json
import os

import httpx

# ── Families (mirror of the server-side design gate) ─────────────────────────

EVAL_DESIGN_TOOLS = frozenset({
    "scenarios_create",
    "scenarios_bulk_update",
    "scenarios_partial_update",
    "scenarios_create_from_transcript",
    "scenarios_create_from_transcript_bg",
    "scenarios_update_scenario_with_transcript_create",
    "test_profiles_create",
    "test_profiles_partial_update",
})
EVAL_DESIGN_SLUGS = frozenset({
    "cekura-eval-design",
    "manual-create-update-eval",
    "autogen-eval",
    "cekura-generate-scenarios",
    "cekura-self-improving-agent",
    "cekura-infra-test-suite",
})

METRIC_DESIGN_TOOLS = frozenset({
    "metrics_create",
    "metrics_bulk_create",
    "metrics_partial_update",
})
METRIC_DESIGN_SLUGS = frozenset({
    "cekura-metric-design",
    "create-metric",
    "improve-metric",
    "cekura-metric-improvement",
    "cekura-predefined-metrics",
})

_FAMILIES = (
    {
        "name": "eval-design",
        "write_tools": EVAL_DESIGN_TOOLS,
        "slugs": EVAL_DESIGN_SLUGS,
        "load_hint": 'cekura_load_skill(skill_name="cekura-eval-design")',
    },
    {
        "name": "metric-design",
        "write_tools": METRIC_DESIGN_TOOLS,
        "slugs": METRIC_DESIGN_SLUGS,
        "load_hint": 'cekura_load_skill(skill_name="cekura-metric-design")',
    },
)

# Every tool the gate can act on. Read/list/run and server-side generation tools
# are intentionally absent — the model does not author their payload content.
GATED_TOOLS = frozenset(EVAL_DESIGN_TOOLS | METRIC_DESIGN_TOOLS)

# Sentinel a caller passes as `skill_ack` to proceed without the skills, AFTER the
# user has explicitly chosen to (enforce/strict only). Recorded as an override.
OVERRIDE_ACK = "proceed-without-skill"

# ── Tag manifest (append-only; historical tags stay valid forever) ───────────

_BAKED_MANIFEST_PATH = os.path.join(os.path.dirname(__file__), "ack_tags.snapshot.json")
_REMOTE_MANIFEST_URL = os.environ.get(
    "CEKURA_ACK_TAGS_URL",
    "https://raw.githubusercontent.com/cekura-ai/cekura-skills/main/cekura/ack-tags.json",
)

_manifest = {}          # slug -> [tags]
_manifest_source = "unloaded"


def _parse_manifest(data):
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, value in data.items():
        if key.startswith("_"):
            continue
        if isinstance(value, list):
            out[key] = [t for t in value if isinstance(t, str)]
    return out


def set_manifest(mapping):
    """Directly set the in-memory manifest (used by tests)."""
    global _manifest, _manifest_source
    _manifest = _parse_manifest(mapping)
    _manifest_source = "explicit"


def get_manifest():
    return dict(_manifest)


def manifest_source():
    return _manifest_source


def _load_baked():
    global _manifest, _manifest_source
    try:
        with open(_BAKED_MANIFEST_PATH) as fh:
            _manifest = _parse_manifest(json.load(fh))
            _manifest_source = "baked"
    except Exception:
        _manifest = {}
        _manifest_source = "empty"
    return _manifest_source


async def load_manifest(client_factory=httpx.AsyncClient):
    """Populate the manifest once at startup: try the public cekura-skills copy,
    fall back to the baked snapshot. Returns the source used.

    The manifest is append-only, so the remote copy is always a superset of any
    installed plugin's tags — fetching it never invalidates a stale plugin.
    """
    global _manifest, _manifest_source
    try:
        async with client_factory(timeout=httpx.Timeout(5.0, connect=2.0)) as client:
            resp = await client.get(_REMOTE_MANIFEST_URL)
            resp.raise_for_status()
            parsed = _parse_manifest(resp.json())
            if parsed:
                _manifest = parsed
                _manifest_source = "remote"
                return _manifest_source
    except Exception:
        pass
    return _load_baked()


def current_tag_for_slug(slug):
    """Latest (last-appended) tag for a slug, or None."""
    tags = _manifest.get((slug or "").strip())
    return tags[-1] if tags else None


def _family_for_tool(tool_name):
    for family in _FAMILIES:
        if tool_name in family["write_tools"]:
            return family
    return None


def _recognized_tags(family):
    tags = set()
    for slug in family["slugs"]:
        tags.update(_manifest.get(slug, []))
    return tags


# ── Decision ─────────────────────────────────────────────────────────────────


class GateDecision:
    __slots__ = ("action", "family", "tool", "ack_present", "ack_valid", "reason", "nudge")

    def __init__(self, action, family, tool, ack_present, ack_valid, reason, nudge=None):
        self.action = action          # "allow" | "shadow_block" | "warn"
        self.family = family          # family name or None
        self.tool = tool
        self.ack_present = ack_present
        self.ack_valid = ack_valid
        self.reason = reason
        self.nudge = nudge


def _nudge_text(family):
    return (
        "\n\n[cekura skills] For much better results, install or update the Cekura skills: "
        "https://docs.cekura.ai/mcp/overview (one-time setup; makes every future session "
        f"skill-guided). To load this playbook just for now instead, call {family['load_hint']} "
        "and pass its verification tag as `skill_ack`. Proceeding without it."
    )


def _deny_text(family, tool_name):
    return (
        f"[cekura skills] '{tool_name}' produces much better results with the Cekura "
        f"{family['name']} playbook loaded, which isn't loaded in this session. This "
        "write was NOT executed.\n\n"
        "Ask the user whether to use the Cekura design skills here, then act on their answer:\n"
        "  • Yes (recommended): tell the user to install or update the Cekura skills so future "
        "sessions are skill-guided automatically (https://docs.cekura.ai/mcp/overview), AND — to "
        f"complete THIS call now — call {family['load_hint']}, then retry this call with the "
        "returned verification tag as `skill_ack`.\n"
        f'  • No, proceed without the skills: retry this call with skill_ack="{OVERRIDE_ACK}".\n\n'
        "Do not proceed until the user has chosen."
    )


def evaluate(tool_name, skill_ack, mode, is_sandbox=False):
    """Pure gate decision for one tool call. Does not raise on ordinary inputs."""
    ack_present = bool(skill_ack)
    family = _family_for_tool(tool_name)
    if family is None:
        return GateDecision("allow", None, tool_name, ack_present, False, "not_gated")
    if mode == "off":
        return GateDecision("allow", family["name"], tool_name, ack_present, False, "mode_off")
    if is_sandbox:
        return GateDecision("allow", family["name"], tool_name, ack_present, False, "sandbox_bypass")

    # Explicit user override: proceed without skills (blocking modes only).
    if skill_ack == OVERRIDE_ACK and mode in ("enforce", "strict"):
        return GateDecision("allow", family["name"], tool_name, ack_present, False, "user_override")

    ack_valid = ack_present and skill_ack in _recognized_tags(family)
    if ack_valid:
        return GateDecision("allow", family["name"], tool_name, True, True, "ack_ok")

    if mode == "shadow":
        return GateDecision("shadow_block", family["name"], tool_name, ack_present, False, "would_block")
    if mode in ("enforce", "strict"):
        # Hold the write; the model must ask the user (install/load or proceed).
        return GateDecision(
            "deny", family["name"], tool_name, ack_present, False, "deny",
            nudge=_deny_text(family, tool_name),
        )
    # warn -> proceed with a nudge
    return GateDecision(
        "warn", family["name"], tool_name, ack_present, False, "warn", nudge=_nudge_text(family)
    )


# ── Schema injection ─────────────────────────────────────────────────────────

_SKILL_ACK_PROPERTY = {
    "type": "string",
    "description": (
        "Optional. If a Cekura design skill or command is loaded, pass its verification "
        "tag here (shown in the skill as `ack:<slug>:<code>`, or returned by "
        "cekura_load_skill). It confirms the design playbook is in context. Leave unset "
        "if you are not working from a Cekura skill."
    ),
}


def maybe_inject_skill_ack(tool_name, input_schema, mode):
    """Add the optional ``skill_ack`` property to a gated tool's input schema.

    No-op when the gate is ``off`` (so the default deploy makes zero schema
    change), for non-gated tools, or when the property already exists. Never
    added to ``required`` — callers that omit it are unaffected.
    """
    if mode == "off" or tool_name not in GATED_TOOLS:
        return input_schema
    if not isinstance(input_schema, dict):
        return input_schema
    props = input_schema.get("properties")
    if not isinstance(props, dict) or "skill_ack" in props:
        return input_schema
    new_props = dict(props)
    new_props["skill_ack"] = dict(_SKILL_ACK_PROPERTY)
    new_schema = dict(input_schema)
    new_schema["properties"] = new_props
    return new_schema
