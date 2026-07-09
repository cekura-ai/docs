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
  stripped from tool args so it never reaches the backend.)
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
import logging
import os

import httpx

logger = logging.getLogger(__name__)

# ── Families (mirror of the server-side design gate) ─────────────────────────
# One entry per gated tool-family. ``skill_slugs`` are skill-backed playbooks the
# server can deliver via ``cekura_load_skill``; ``command_slugs`` are slash-command
# playbooks that ship only inside the installed plugin. Tags from either satisfy
# the family's gate.


def _family(name, write_tools, skill_slugs, command_slugs, load_hint):
    return {
        "name": name,
        "write_tools": frozenset(write_tools),
        "skill_slugs": frozenset(skill_slugs),
        "slugs": frozenset(skill_slugs) | frozenset(command_slugs),
        "load_hint": load_hint,
    }


_FAMILIES = (
    _family(
        "eval-design",
        write_tools={
            "scenarios_create",
            "scenarios_bulk_update",
            "scenarios_partial_update",
            "scenarios_create_from_transcript",
            "scenarios_create_from_transcript_bg",
            "scenarios_update_scenario_with_transcript_create",
            "test_profiles_create",
            "test_profiles_partial_update",
        },
        skill_slugs={
            "cekura-eval-design",
            "cekura-generate-scenarios",
            "cekura-self-improving-agent",
            "cekura-infra-test-suite",
        },
        command_slugs={"manual-create-update-eval", "autogen-eval"},
        load_hint='cekura_load_skill(skill_name="cekura-eval-design")',
    ),
    _family(
        "metric-design",
        write_tools={
            "metrics_create",
            "metrics_bulk_create",
            "metrics_partial_update",
        },
        skill_slugs={
            "cekura-metric-design",
            "cekura-metric-improvement",
            "cekura-predefined-metrics",
        },
        command_slugs={"create-metric", "improve-metric"},
        load_hint='cekura_load_skill(skill_name="cekura-metric-design")',
    ),
)

# Every tool the gate can act on. Read/list/run and server-side generation tools
# are intentionally absent — the model does not author their payload content.
GATED_TOOLS = frozenset().union(*(f["write_tools"] for f in _FAMILIES))

# Every slug that can carry a recognized tag.
ALL_FAMILY_SLUGS = frozenset().union(*(f["slugs"] for f in _FAMILIES))

# Slugs `cekura_load_skill` can deliver (skill-backed; command playbooks exist
# only in the installed plugin).
LOADABLE_SKILLS = tuple(sorted(frozenset().union(*(f["skill_slugs"] for f in _FAMILIES))))

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
_family_tags = {}       # family name -> frozenset of every recognized tag


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


def _rebuild_family_tags():
    """Precompute each family's recognized-tag set; the manifest only changes at
    load time, so `evaluate` does a plain membership test per call."""
    global _family_tags
    _family_tags = {
        f["name"]: frozenset(tag for slug in f["slugs"] for tag in _manifest.get(slug, []))
        for f in _FAMILIES
    }


def set_manifest(mapping):
    """Directly set the in-memory manifest (used by tests)."""
    global _manifest, _manifest_source
    _manifest = _parse_manifest(mapping)
    _manifest_source = "explicit"
    _rebuild_family_tags()


def get_manifest():
    return dict(_manifest)


def _load_baked():
    global _manifest, _manifest_source
    try:
        with open(_BAKED_MANIFEST_PATH) as fh:
            _manifest = _parse_manifest(json.load(fh))
            _manifest_source = "baked"
    except Exception:
        _manifest = {}
        _manifest_source = "empty"
    _rebuild_family_tags()
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
                _rebuild_family_tags()
                return _manifest_source
        logger.warning(
            "ack-tag manifest at %s fetched but empty/unparseable — falling back to baked snapshot",
            _REMOTE_MANIFEST_URL,
        )
    except Exception as e:
        logger.warning(
            "ack-tag manifest fetch failed (%s): %s — falling back to baked snapshot",
            _REMOTE_MANIFEST_URL, e,
        )
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


# ── Decision ─────────────────────────────────────────────────────────────────


class GateDecision:
    __slots__ = ("action", "family", "ack_present", "ack_valid", "reason", "nudge")

    def __init__(self, action, family, ack_present, ack_valid, reason, nudge=None):
        self.action = action          # "allow" | "shadow_block" | "warn" | "deny"
        self.family = family          # family name or None
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
        f"[cekura skills] '{tool_name}' works much better with the Cekura {family['name']} "
        "playbook loaded, which isn't loaded here. This write was NOT executed.\n\n"
        "Ask the user, then:\n"
        "  • Use the skills (recommended): suggest they install or update the Cekura skills "
        f"(https://docs.cekura.ai/mcp/overview), call {family['load_hint']}, and pass the "
        "returned tag as `skill_ack` on this and every subsequent write call.\n"
        f'  • Proceed without: pass skill_ack="{OVERRIDE_ACK}" on this and every subsequent '
        "write call.\n\n"
        "Do not proceed until the user has chosen."
    )


def evaluate(tool_name, skill_ack, mode, is_sandbox=False):
    """Pure gate decision for one tool call. Does not raise on ordinary inputs."""
    ack_present = bool(skill_ack)
    family = _family_for_tool(tool_name)
    if family is None:
        return GateDecision("allow", None, ack_present, False, "not_gated")
    if mode == "off":
        return GateDecision("allow", family["name"], ack_present, False, "mode_off")
    if is_sandbox:
        return GateDecision("allow", family["name"], ack_present, False, "sandbox_bypass")

    # Explicit user override: proceed without skills (blocking modes only).
    if skill_ack == OVERRIDE_ACK and mode in ("enforce", "strict"):
        return GateDecision("allow", family["name"], ack_present, False, "user_override")

    ack_valid = ack_present and skill_ack in _family_tags.get(family["name"], frozenset())
    if ack_valid:
        return GateDecision("allow", family["name"], True, True, "ack_ok")

    if mode == "shadow":
        return GateDecision("shadow_block", family["name"], ack_present, False, "would_block")
    if mode in ("enforce", "strict"):
        # Hold the write; the model must ask the user (install/load or proceed).
        return GateDecision(
            "deny", family["name"], ack_present, False, "deny",
            nudge=_deny_text(family, tool_name),
        )
    # warn -> proceed with a nudge
    return GateDecision(
        "warn", family["name"], ack_present, False, "warn", nudge=_nudge_text(family)
    )


# ── Handler-side application ─────────────────────────────────────────────────

# decision.reason -> (log event, blocked flag). Allows with reason
# mode_off/not_gated log nothing. `blocked` None means the event doesn't carry
# the ack_present/blocked pair.
_GATE_LOG_EVENTS = {
    "deny": ("skill_gate_blocked", True),
    "would_block": ("skill_gate_blocked", False),
    "warn": ("skill_gate_blocked", False),
    "user_override": ("skill_gate_user_override", None),
    "sandbox_bypass": ("skill_gate_sandbox_bypass", None),
    "ack_ok": ("skill_gate_ack_ok", None),
}


def _log_decision(decision, mode, tool_name, call_id, client_id, cred_hash_fn):
    entry = _GATE_LOG_EVENTS.get(decision.reason)
    if entry is None:
        return
    event, blocked = entry
    payload = {
        "event": event,
        "tool": tool_name,
        "family": decision.family,
        "call_id": call_id,
    }
    if event != "skill_gate_sandbox_bypass":
        payload["mode"] = mode
    if event in ("skill_gate_blocked", "skill_gate_user_override"):
        payload["client_id"] = client_id
        payload["cred_hash"] = cred_hash_fn()
    if blocked is not None:
        payload["ack_present"] = decision.ack_present
        payload["blocked"] = blocked
    logger.info(json.dumps(payload))


def apply_gate(tool_name, arguments, mode, *, is_sandbox=False, client_id=None,
               call_id=None, cred_hash_fn=lambda: None):
    """The complete handler-side gate step for one tool call.

    Returns ``(deny_text, nudge)``: ``deny_text`` set means the write must NOT
    be executed (enforce/strict deny); ``nudge`` set means proceed and append it
    to the response (warn).

    Always strips ``skill_ack`` from ``arguments`` in place — in EVERY mode,
    including ``off`` — so the backend request stays byte-identical to a call
    that never carried it. Evaluation and logging run only for gated tools with
    the gate on; any internal error fails open (write allowed).
    """
    skill_ack = None
    if arguments and "skill_ack" in arguments:
        skill_ack = arguments.pop("skill_ack")

    if mode == "off" or tool_name not in GATED_TOOLS:
        return None, None

    try:
        decision = evaluate(tool_name, skill_ack, mode, is_sandbox=is_sandbox)
        _log_decision(decision, mode, tool_name, call_id, client_id, cred_hash_fn)
        if decision.action == "deny":
            return decision.nudge, None
        if decision.action == "warn":
            return None, decision.nudge
        return None, None
    except Exception:
        logger.exception("skill_gate_error tool=%s (failing open)", tool_name)
        return None, None


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
