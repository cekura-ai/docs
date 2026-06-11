import asyncio
import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
import jwt
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

# Load secrets from AWS Secrets Manager before any env var is read
if os.getenv("AWS_SECRET_NAME"):
    import boto3
    _sm_client = boto3.client("secretsmanager")
    _secret_value = _sm_client.get_secret_value(SecretId=os.getenv("AWS_SECRET_NAME"))
    _config = json.loads(_secret_value["SecretString"])
    os.environ.update({key: str(value) for key, value in _config.items()})

from config import load_config
from http_client import create_client
from openapi_parser import load_openapi_spec
from tool_generator import (
    apply_overlay_to_description,
    apply_overlay_to_schema,
    build_input_schema,
    compute_annotations,
    generate_tool_description,
    generate_tool_name,
    maybe_append_org_project_hint,
    should_include_operation,
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger(__name__)


class HealthCheckFilter(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        return not any(path in message for path in ['/mcp/health', '/mcp/healthz', '/favicon.ico'])

request_api_key: ContextVar[str] = ContextVar('request_api_key', default=None)
request_bearer_token: ContextVar[str] = ContextVar('request_bearer_token', default=None)
request_base_url: ContextVar[str] = ContextVar('request_base_url', default=None)
# Connection-level conversation identifier. Set by hosts that have a stable
# notion of conversation across multiple tool calls (e.g. the Cekura sandbox
# wires its conversation_id here). Per-call `_meta["com.cekura/conversation_id"]`
# wins when both are present.
request_conversation_id: ContextVar[str] = ContextVar('request_conversation_id', default=None)
# MCP streamable-http session id (``Mcp-Session-Id``). Captured so external
# analytics can group an external client's tool calls into a chat session
# without relying on a per-call conversation id.
request_mcp_session_id: ContextVar[str] = ContextVar('request_mcp_session_id', default=None)

# X-CEKURA-BASE-URL override is only allowed when explicitly enabled (dev/staging only)
_ALLOW_BASE_URL_OVERRIDE = os.environ.get("ALLOW_BASE_URL_OVERRIDE", "").lower() in ("1", "true", "yes")

MCP_ISSUER_URL = os.environ.get("MCP_ISSUER_URL", "https://api.cekura.ai")
MCP_SERVER_URL = os.environ.get("MCP_SERVER_URL", "https://api.cekura.ai/mcp")

# Clock-skew grace (seconds) for the local oauth_access JWT expiry check.
OAUTH_EXP_SKEW_SECONDS = 10

# Derive allowed hosts from MCP_ISSUER_URL and MCP_SERVER_URL (covers prod, ngrok, local).
from urllib.parse import urlparse as _urlparse

_issuer_host = _urlparse(MCP_ISSUER_URL).netloc
_server_host = _urlparse(MCP_SERVER_URL).netloc
_allowed_hosts = [
    "api.cekura.ai",
    "test.cekura.ai",
    "localhost",
    "localhost:8000",
    "localhost:8001",
    "localhost:8002",
    "127.0.0.1",
    "127.0.0.1:8001",
    "0.0.0.0",
    "0.0.0.0:8001",
]
if _issuer_host and _issuer_host not in _allowed_hosts:
    _allowed_hosts.append(_issuer_host)
if _server_host and _server_host not in _allowed_hosts:
    _allowed_hosts.append(_server_host)

transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=_allowed_hosts,
)

mcp = FastMCP("Cekura API", transport_security=transport_security)

server_config = None
openapi_parser = None
operations_registry = {}

MINTLIFY_MCP_URL = "https://docs.cekura.ai/mcp"
MINTLIFY_SEARCH_TIMEOUT = 15.0
MINTLIFY_MAX_RETRIES = 2
MINTLIFY_TOOL_NAME = "search_cekura"  # Fallback, will be dynamically fetched


async def fetch_mintlify_tool_name():
    """Fetch the search tool name from Mintlify's MCP server dynamically. """
    global MINTLIFY_TOOL_NAME

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0)) as client:
            response = await client.post(
                MINTLIFY_MCP_URL,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream"
                },
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list"
                }
            )
            response.raise_for_status()

            # Parse SSE response
            for line in response.text.split('\n'):
                if line.startswith('data: '):
                    data = json.loads(line[6:])

                    if 'result' in data and 'tools' in data['result']:
                        # Find search tool (contains "search" and "cekura")
                        for tool in data['result']['tools']:
                            name = tool.get('name', '').lower()
                            if 'search' in name and 'cekura' in name:
                                MINTLIFY_TOOL_NAME = tool['name']
                                logger.info(f"Discovered Mintlify tool name: {MINTLIFY_TOOL_NAME}")
                                return

            logger.warning(f"Mintlify search tool not found in response, using fallback: {MINTLIFY_TOOL_NAME}")

    except Exception as e:
        logger.warning(f"Failed to fetch Mintlify tool name (using fallback '{MINTLIFY_TOOL_NAME}'): {e}")


async def initialize_server():
    global server_config, openapi_parser, operations_registry

    try:
        # Fetch Mintlify's actual tool name
        await fetch_mintlify_tool_name()

        server_config = load_config()
        logger.info(f"Loaded config: Base URL={server_config.base_url}")

        openapi_parser = load_openapi_spec(server_config.openapi_spec_path)
        logger.info(f"Loaded OpenAPI spec from {server_config.openapi_spec_path}")

        operations = openapi_parser.extract_operations()
        logger.info(f"Found {len(operations)} operations in OpenAPI spec")

        blocked_tools = server_config.resolve_blocked_tools()

        tools_registered = 0
        blocked_hits = []
        for operation in operations:
            if not should_include_operation(operation):
                continue

            if server_config.max_tools and tools_registered >= server_config.max_tools:
                logger.warning(f"Reached max_tools limit ({server_config.max_tools}), stopping registration")
                break

            try:
                tool_name = generate_tool_name(operation)

                if tool_name in blocked_tools:
                    blocked_hits.append(tool_name)
                    continue

                tool_description = generate_tool_description(operation)
                input_schema = build_input_schema(operation, openapi_parser)

                tool_description = maybe_append_org_project_hint(tool_name, input_schema, tool_description)
                tool_description = apply_overlay_to_description(tool_name, tool_description)
                input_schema = apply_overlay_to_schema(tool_name, input_schema)

                annotations = compute_annotations(operation)
                register_tool(tool_name, tool_description, input_schema, operation, annotations=annotations)
                tools_registered += 1
            except Exception as e:
                logger.error(f"Error registering tool for {operation.path}: {e}", exc_info=True)
                continue

        if blocked_hits:
            logger.info(f"Registered {tools_registered} MCP tools (blocked: {sorted(blocked_hits)})")
        else:
            logger.info(f"Registered {tools_registered} MCP tools")

        # Non-fatal drift check: log a warning for each overlay that has diverged
        # from the live openapi.json + whitelist. Keeps production booting while
        # making divergence immediately visible in logs / dashboards.
        try:
            from validate_overlays import run_checks as _overlay_checks
            drift = _overlay_checks()
            if drift:
                errs = [f for f in drift if f.level == "error"]
                warns = [f for f in drift if f.level == "warning"]
                if errs:
                    logger.warning(
                        f"Overlay drift: {len(errs)} error(s), {len(warns)} warning(s) — "
                        "run `python3 validate_overlays.py` for details. Overlays are still "
                        "applied; these tools may render with stale or inaccurate descriptions."
                    )
                    for f in errs[:5]:
                        logger.warning(f"  overlay[{f.category}] {f.tool}: {f.message[:200]}")
                elif warns:
                    logger.info(f"Overlay drift: {len(warns)} warning(s) — non-blocking.")
        except Exception as e:
            logger.warning(f"Overlay drift check skipped: {e}")

        # Register Mintlify documentation search tool
        register_mintlify_search_tool()
        logger.info("Registered Mintlify documentation search tool")

        setup_dynamic_tool_handlers()

    except Exception as e:
        logger.error(f"Failed to initialize server: {e}", exc_info=True)
        sys.exit(1)


def register_mintlify_search_tool():
    """Register Mintlify documentation search tool as a proxy."""
    # Use the dynamically fetched tool name
    operations_registry[MINTLIFY_TOOL_NAME] = {
        'operation': None,
        'schema': {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query"
                }
            },
            "required": ["query"],
            "additionalProperties": False,
            "$schema": "http://json-schema.org/draft-07/schema#"
        },
        'description': "Search across the Cekura knowledge base to find relevant information, code examples, API references, and guides. Use this tool when you need to answer questions about Cekura, find specific documentation, understand how features work, or locate implementation details. The search returns contextual content with titles and direct links to the documentation pages.",
        'is_proxy': True,
        'annotations': ToolAnnotations(readOnlyHint=True),
    }


async def call_mintlify_search(query: str) -> List[Dict[str, str]]:
    """Proxy search requests to Mintlify's MCP server with retry logic."""
    if not query or not query.strip():
        return [{"type": "text", "text": "Please provide a search query."}]

    for attempt in range(MINTLIFY_MAX_RETRIES):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(MINTLIFY_SEARCH_TIMEOUT, connect=5.0),
                follow_redirects=True
            ) as client:
                response = await client.post(
                    MINTLIFY_MCP_URL,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream"
                    },
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "tools/call",
                        "params": {
                            "name": MINTLIFY_TOOL_NAME,
                            "arguments": {"query": query.strip()}
                        }
                    }
                )

                response.raise_for_status()

                for line in response.text.split('\n'):
                    if line.startswith('data: '):
                        try:
                            data = json.loads(line[6:])
                            if 'result' in data and 'content' in data['result']:
                                content = data['result']['content']
                                if content:
                                    return content
                        except json.JSONDecodeError as e:
                            logger.warning(f"Failed to parse SSE data: {e}")
                            continue

                return [{"type": "text", "text": "No results found for your query."}]

        except httpx.TimeoutException:
            logger.warning(f"Timeout calling Mintlify search (attempt {attempt + 1}/{MINTLIFY_MAX_RETRIES})")
            if attempt < MINTLIFY_MAX_RETRIES - 1:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            return [{"type": "text", "text": "Search request timed out. Please try again."}]

        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error from Mintlify: {e.response.status_code}")
            return [{"type": "text", "text": f"Documentation search temporarily unavailable (HTTP {e.response.status_code})."}]

        except httpx.RequestError as e:
            logger.error(f"Network error calling Mintlify: {e}")
            if attempt < MINTLIFY_MAX_RETRIES - 1:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            return [{"type": "text", "text": "Unable to reach documentation search. Please check your connection."}]

        except Exception as e:
            logger.error(f"Unexpected error in Mintlify search: {e}", exc_info=True)
            return [{"type": "text", "text": f"Search error: {str(e)}"}]

    return [{"type": "text", "text": "Search failed after multiple attempts. Please try again later."}]


def register_tool(
    name: str,
    description: str,
    input_schema: Dict[str, Any],
    operation,
    annotations: ToolAnnotations = None,
):
    operations_registry[name] = {
        'operation': operation,
        'schema': input_schema,
        'description': description,
        'annotations': annotations,
    }


@mcp.tool(
    name="list_available_tools",
    description="List all available Cekura API tools",
    annotations=ToolAnnotations(readOnlyHint=True),
)
async def list_available_tools() -> str:
    tools = sorted(operations_registry.keys())
    return f"Available tools ({len(tools)}):\n" + "\n".join(f"- {tool}" for tool in tools)


@mcp.tool(
    name="test_simple_tool",
    description="A simple test tool to verify MCP registration",
    annotations=ToolAnnotations(readOnlyHint=True),
)
async def test_simple_tool(message: str) -> str:
    return f"Hello from Cekura MCP Server! You said: {message}"


def _append_call_id_to_text(result: Any, mcp_call_id: str) -> Any:
    """Append ``[cekura_mcp_call_id: …]`` to the trailing text content block.

    Leaves non-list / non-text results untouched so this is safe to apply
    blindly to any tool response shape.
    """
    if isinstance(result, list) and result:
        for block in reversed(result):
            if isinstance(block, dict) and block.get("type") == "text":
                block["text"] = f"{block.get('text', '')}\n\n[cekura_mcp_call_id: {mcp_call_id}]"
                return result
    return result


# -- Agent self-escalation ("scream tool") -----------------------------------

SLACK_ESCALATIONS_WEBHOOK = os.environ.get("SLACK_ESCALATIONS_WEBHOOK")

_ESCALATION_LIMITS = {  # severity -> (max_calls, window_seconds)
    "low": (20, 3600),
    "medium": (20, 3600),
    "high": (5, 3600),
    "critical": (5, 3600),
}
_VALID_SEVERITIES = tuple(_ESCALATION_LIMITS.keys())
_escalation_history: Dict[Tuple[str, str], List[float]] = {}

_PII_PATTERNS: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"\+?\d[\d\s().-]{7,}\d"), "[phone]"),
    (re.compile(r"\b(?:sk|pk|api)[-_][A-Za-z0-9]{8,}\b"), "[token]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"), "[bearer]"),
)


def _redact_pii(text: Optional[str]) -> Optional[str]:
    if not text:
        return text
    redacted = text
    for pattern, replacement in _PII_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _credential_fingerprint() -> str:
    """Stable 16-char fingerprint of the active credential, or ``anon``."""
    try:
        credential, _ = get_request_credential()
    except ValueError:
        return "anon"
    return hashlib.sha256(credential.encode()).hexdigest()[:16]


def _check_escalation_rate_limit(cred_hash: str, severity: str) -> bool:
    """Return True when the caller is within the per-severity budget.

    Buckets are kept in-process; resets per replica. Acceptable for v1 — the
    Slack severity gate plus the 2s post timeout cap blast radius. Upgrade to
    Redis if real-world abuse appears.
    """
    limits = _ESCALATION_LIMITS.get(severity)
    if not limits:
        return True
    max_calls, window = limits
    key = (cred_hash, severity)
    now = time.monotonic()
    history = _escalation_history.get(key, [])
    pruned = [ts for ts in history if now - ts < window]
    if len(pruned) >= max_calls:
        _escalation_history[key] = pruned
        return False
    pruned.append(now)
    _escalation_history[key] = pruned
    return True


@mcp.tool(
    name="cekura_report_issue",
    description=(
        "Self-report a concern about Cekura tools, skills, or documentation. "
        "Use this LIBERALLY — do not second-guess yourself. Severity 'low' and "
        "'medium' reports are just as valuable as 'high' and 'critical'.\n\n"
        "USE WHEN:\n"
        "1. A tool's input schema or description is ambiguous and you guessed.\n"
        "2. You tried tool A but had to fall back to tool B; the right tool was unclear.\n"
        "3. A tool returned an error that the description didn't predict.\n"
        "4. Two tools look like they do the same thing and you weren't sure which to pick.\n"
        "5. A required field's expected format wasn't documented.\n"
        "6. The docs and the tool behaviour disagreed.\n"
        "7. A skill instruction couldn't be followed (no matching tool / wrong shape).\n"
        "8. You retried a tool more than twice because of unclear errors.\n"
        "9. A workflow needs a tool that doesn't exist.\n"
        "10. A tool's response shape changed mid-flow.\n"
        "11. You produced a workaround that you suspect isn't the intended path.\n"
        "12. Any other moment where you wished the platform had told you something.\n\n"
        "If reporting about a prior tool call, pass that call's "
        "cekura_mcp_call_id (visible at the end of every Cekura tool response) "
        "as related_mcp_call_id."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
)
async def cekura_report_issue(
    concern: str,
    severity: str,
    cekura_resource_attempted: Optional[str] = None,
    workaround_used: Optional[str] = None,
    related_mcp_call_id: Optional[str] = None,
) -> Dict[str, Any]:
    severity_norm = (severity or "").strip().lower()
    if severity_norm not in _VALID_SEVERITIES:
        return {
            "status": "invalid_severity",
            "expected_one_of": list(_VALID_SEVERITIES),
        }

    cred_hash = _credential_fingerprint()

    if not _check_escalation_rate_limit(cred_hash, severity_norm):
        return {"status": "rate_limited", "retry_after_s": 60}

    concern_redacted = (_redact_pii(concern) or "")[:2000]
    workaround_redacted = (_redact_pii(workaround_used) or "")[:1000] or None
    resource_redacted = (_redact_pii(cekura_resource_attempted) or "")[:500] or None

    report_mcp_call_id = f"call_{uuid.uuid4().hex[:16]}"
    event_id = f"esc_{uuid.uuid4().hex[:12]}"

    logger.info(json.dumps({
        "event": "agent_escalation",
        "event_id": event_id,
        "report_mcp_call_id": report_mcp_call_id,
        "related_mcp_call_id": related_mcp_call_id,
        "severity": severity_norm,
        "resource": resource_redacted,
        "concern": concern_redacted,
        "workaround": workaround_redacted,
        "cred_hash": cred_hash,
        "client_id": _resolve_client_identifier(),
    }))

    if SLACK_ESCALATIONS_WEBHOOK and severity_norm in ("medium", "high", "critical"):
        slack_payload = {
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": f"Agent escalation: {severity_norm}"},
                },
                {
                    "type": "section",
                    "fields": [
                        {"type": "mrkdwn", "text": f"*event_id*\n`{event_id}`"},
                        {"type": "mrkdwn", "text": f"*resource*\n{resource_redacted or '—'}"},
                        {"type": "mrkdwn", "text": f"*related*\n`{related_mcp_call_id or '—'}`"},
                        {"type": "mrkdwn", "text": f"*cred_hash*\n`{cred_hash}`"},
                    ],
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*Concern*\n```{concern_redacted}```"},
                },
            ]
        }
        if workaround_redacted:
            slack_payload["blocks"].append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Workaround*\n```{workaround_redacted}```"},
            })
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=1.0)) as client:
                await client.post(SLACK_ESCALATIONS_WEBHOOK, json=slack_payload)
        except Exception:
            logger.exception("slack_post_failed event_id=%s", event_id)

    return {
        "status": "reported",
        "event_id": event_id,
        "report_mcp_call_id": report_mcp_call_id,
    }


# -- Skill activation beacon -------------------------------------------------


@mcp.tool(
    name="cekura_skill_started",
    description=(
        "Call this as the FIRST action of any Cekura skill or command. Lets us "
        "know which skills are actually being used. Returns immediately.\n\n"
        "Args:\n"
        "  skill_name: the slug of the skill — e.g. \"autogen-eval\".\n"
        "  triggering_intent: optional one-sentence description of what the "
        "user wanted that led you to pick this skill.\n"
        "  conversation_id: optional Cekura sandbox / chat conversation ID."
    ),
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
)
async def cekura_skill_started(
    skill_name: str,
    triggering_intent: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> Dict[str, Any]:
    cred_hash = _credential_fingerprint()
    intent_redacted = (_redact_pii(triggering_intent) or "")[:200] or None
    event_id = f"skill_{uuid.uuid4().hex[:12]}"

    logger.info(json.dumps({
        "event": "skill_started",
        "event_id": event_id,
        "skill": skill_name,
        "triggering_intent": intent_redacted,
        "conversation_id": conversation_id,
        "client_id": _resolve_client_identifier(),
        "cred_hash": cred_hash,
    }))

    return {"status": "ok", "event_id": event_id}


def _claude_jsonl_to_cekura_transcript(jsonl_text: str) -> List[Dict[str, object]]:
    """Convert a Claude Code session transcript (JSONL) to the cekura transcript
    shape: a list of {role, content, start_time, end_time} entries. Roles are
    "Testing Agent" (the human user) and "Main Agent" (Claude). Times are
    seconds since the first entry — the Cekura observe endpoint requires both."""
    raw: List[Dict[str, object]] = []  # {ts: datetime|None, role, content}
    for line in jsonl_text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_type = entry.get("type") or entry.get("role")
        if msg_type in ("user", "human"):
            role = "Testing Agent"
        elif msg_type == "assistant":
            role = "Main Agent"
        else:
            continue

        # Claude Code's transcript wraps the message under entry["message"]; older
        # / simpler producers may put content at the top level.
        content = entry.get("content")
        if content is None:
            message = entry.get("message")
            if isinstance(message, dict):
                content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts: List[str] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    parts.append(f"[tool_use:{block.get('name', '?')}]")
                elif btype == "tool_result":
                    parts.append("[tool_result]")
            text = "\n".join(p for p in parts if p)
        else:
            continue

        text = text.strip()
        if not text:
            continue

        ts_raw = entry.get("timestamp")
        ts: object = None
        if isinstance(ts_raw, str) and ts_raw:
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except ValueError:
                ts = None
        raw.append({"ts": ts, "role": role, "content": text})

    if not raw:
        return []

    # Anchor t0 at the first parseable timestamp; entries with no timestamp are
    # placed sequentially after the most recent timed entry.
    t0 = next((r["ts"] for r in raw if r["ts"] is not None), None)
    transcript: List[Dict[str, object]] = []
    for i, r in enumerate(raw):
        if r["ts"] is not None and t0 is not None:
            start = (r["ts"] - t0).total_seconds()
        else:
            start = float(i)
        # end_time = next entry's start (or start + 1s for the last item).
        # Avoid zero-duration ranges, which the upstream may reject.
        if i + 1 < len(raw) and raw[i + 1]["ts"] is not None and t0 is not None:
            end = (raw[i + 1]["ts"] - t0).total_seconds()
        else:
            end = start + 1.0
        if end <= start:
            end = start + 0.1
        transcript.append({
            "role": r["role"],
            "content": r["content"],
            "start_time": start,
            "end_time": end,
        })

    # Enforce per-role monotonicity: the upstream observe serializer rejects any
    # entry whose start_time or end_time is earlier than the previous entry of
    # the same role. Claude Code JSONL timestamps can have small inversions
    # (async recording, ms rounding), so clamp to the running max per role.
    role_last: Dict[str, Dict[str, float]] = {}
    for entry in transcript:
        role = entry["role"]
        last = role_last.get(role)
        if last is not None:
            if entry["start_time"] < last["start"]:
                entry["start_time"] = last["start"]
            if entry["end_time"] < last["end"]:
                entry["end_time"] = last["end"]
        if entry["end_time"] <= entry["start_time"]:
            entry["end_time"] = entry["start_time"] + 0.1
        role_last[role] = {"start": entry["start_time"], "end": entry["end_time"]}

    return transcript


def get_request_credential() -> tuple[str, str]:
    """Return (credential, type) from request context. Type is 'bearer' or 'api_key'.

    Reads the headers of the HTTP request that delivered the current MCP
    message. The session task's contextvars are snapshotted at session
    creation, so a token refreshed mid-session would never reach handlers
    through them; the contextvars remain only as a fallback for contexts
    without an MCP request (e.g. unit tests).
    """
    headers = None
    try:
        req = mcp._mcp_server.request_context.request
        headers = getattr(req, "headers", None)
    except LookupError:
        pass
    if headers is not None:
        auth = headers.get('Authorization') or headers.get('authorization')
        if auth and auth.lower().startswith('bearer '):
            return auth[7:], "bearer"
        api_key = headers.get('X-CEKURA-API-KEY') or headers.get('x-cekura-api-key')
        if api_key:
            return api_key, "api_key"
    bearer = request_bearer_token.get()
    if bearer:
        return bearer, "bearer"
    api_key = request_api_key.get()
    if api_key:
        return api_key, "api_key"
    raise ValueError(
        "No credential found. Connect via X-CEKURA-API-KEY header, Bearer token, or OAuth."
    )

def _resolve_client_identifier() -> str:
    """Best-effort client identifier from the active request context.

    Falls back to ``unknown`` when the context isn't available (e.g. unit tests).
    """
    try:
        # `request_context` is a property that returns the current RequestContext
        # via the underlying ContextVar — no `.get()` needed.
        req_ctx = mcp._mcp_server.request_context
        session = getattr(req_ctx, "session", None)
        params = getattr(session, "client_params", None) if session else None
        ci = getattr(params, "clientInfo", None) if params else None
        if ci is None:
            return "unknown"
        name = getattr(ci, "name", None) or "unknown"
        version = getattr(ci, "version", None) or "unknown"
        return f"{name}/{version}"
    except (LookupError, AttributeError):
        return "unknown"


def _read_request_meta() -> Dict[str, Any]:
    """Return the ``_meta`` dict from the current MCP request, or ``{}``.

    The MCP protocol allows callers to attach arbitrary key/value metadata
    on a tool call under ``_meta``. We use the ``com.cekura/*`` namespace
    for fields like ``skill`` and ``conversation_id``. The SDK exposes
    these via ``request.params.meta``; access defensively so handler still
    works when nothing was supplied.
    """
    try:
        req_ctx = mcp._mcp_server.request_context
        params = getattr(req_ctx.request, "params", None)
        meta_obj = getattr(params, "meta", None) if params is not None else None
        if meta_obj is None:
            return {}
        if isinstance(meta_obj, dict):
            return meta_obj
        # Pydantic model — surface declared + extra fields.
        if hasattr(meta_obj, "model_dump"):
            return meta_obj.model_dump(exclude_none=True)
        extra = getattr(meta_obj, "model_extra", None)
        return dict(extra) if isinstance(extra, dict) else {}
    except (LookupError, AttributeError):
        return {}


def _resolve_telemetry() -> Dict[str, Optional[str]]:
    """Resolve per-call telemetry fields once at the top of the handler.

    Returns a dict with keys: ``call_id``, ``client_id``, ``skill``,
    ``conversation_id``. ``skill`` is read from ``_meta["com.cekura/skill"]``;
    ``conversation_id`` falls back to the connection-level
    ``X-Cekura-Conversation-Id`` header when not supplied per-call.
    """
    meta = _read_request_meta()
    skill = meta.get("com.cekura/skill")
    conversation_id = meta.get("com.cekura/conversation_id") or request_conversation_id.get()
    mcp_session_id = request_mcp_session_id.get()
    return {
        "call_id": f"call_{uuid.uuid4().hex[:16]}",
        "client_id": _resolve_client_identifier(),
        "skill": skill if isinstance(skill, str) and skill else None,
        "conversation_id": conversation_id if isinstance(conversation_id, str) and conversation_id else None,
        "mcp_session_id": mcp_session_id if isinstance(mcp_session_id, str) and mcp_session_id else None,
    }


_PATH_PARAM_RE = re.compile(r'\{(\w+)\}')


def _dispatch_args(op, arguments: Dict[str, Any]) -> Tuple[str, Dict[str, Any], Any]:
    """Classify tool args into (resolved_path, query_params, body_payload).

    Classification rules:
    - Path params (matched against `{name}` placeholders) substituted into the URL.
    - Query params (declared in OpenAPI `parameters` with `in: query`) sent in the URL.
    - Everything else: routed to JSON body if the op has a requestBody, else to query.

    For top-level array bodies (bulk endpoints), the `items` arg is unwrapped so
    the body is sent as a bare JSON array.
    """
    path_param_names = set(_PATH_PARAM_RE.findall(op.path))
    query_param_names = {
        p["name"] for p in (op.parameters or [])
        if p.get("in") == "query" and "name" in p
    }
    has_body = bool(op.request_body)

    resolved_path = op.path
    query_args: Dict[str, Any] = {}
    body_args: Dict[str, Any] = {}

    for key, value in arguments.items():
        if value is None:
            continue
        if key in path_param_names:
            resolved_path = resolved_path.replace(f"{{{key}}}", str(value))
        elif key in query_param_names:
            query_args[key] = value
        elif has_body:
            body_args[key] = value
        else:
            query_args[key] = value

    if has_body:
        schema = op.request_body.get("content", {}).get("application/json", {}).get("schema", {})
        if schema.get("type") == "array" and "items" in body_args:
            return resolved_path, query_args, body_args["items"]

    return resolved_path, query_args, (body_args if has_body else None)


def setup_dynamic_tool_handlers():
    from mcp.types import Tool as MCPTool

    original_list_tools = mcp.list_tools
    original_call_tool = mcp.call_tool

    async def list_tools_with_dynamic():
        regular_tools = await original_list_tools()

        dynamic_tools = [
            MCPTool(
                name=name,
                description=data['description'],
                inputSchema=data['schema'],
                annotations=data.get('annotations'),
            )
            for name, data in operations_registry.items()
        ]

        return regular_tools + dynamic_tools

    async def call_tool_with_dynamic(name: str, arguments: dict):
        if name not in operations_registry:
            return await original_call_tool(name=name, arguments=arguments)

        telemetry = _resolve_telemetry()
        mcp_call_id = telemetry["call_id"]
        call_id_suffix = f"\n\n[cekura_mcp_call_id: {mcp_call_id}]"
        try:
            tool_data = operations_registry[name]

            if tool_data.get('is_proxy'):
                query = (arguments or {}).get('query', '')
                proxy_result = await call_mintlify_search(query)
                # Proxy tools don't traverse the API client, so the visible
                # call identifier is appended directly to the response text.
                return _append_call_id_to_text(proxy_result, mcp_call_id)

            credential, credential_type = get_request_credential()
            op = tool_data['operation']

            base_url = request_base_url.get() or server_config.base_url
            user_api_client = create_client(
                base_url,
                credential,
                credential_type=credential_type,
                mcp_call_id=mcp_call_id,
                mcp_client_id=telemetry["client_id"],
                mcp_tool=name,
                mcp_skill=telemetry["skill"],
                conversation_id=telemetry["conversation_id"],
                mcp_session_id=telemetry["mcp_session_id"],
            )

            # Forward the resolved per-property types so the HTTP client can respect
            # `type: string` fields (e.g. scenarios.instructions, which carries a
            # stringified JSON body) instead of auto-parsing JSON-looking strings.
            schema_properties = tool_data['schema'].get('properties', {}) or {}
            property_types = {
                k: v.get('type') for k, v in schema_properties.items() if isinstance(v, dict)
            }

            resolved_path, query_args, body_payload = _dispatch_args(op, arguments or {})
            try:
                result = await user_api_client.execute_request(
                    method=op.method,
                    path=resolved_path,
                    query_params=query_args,
                    body=body_payload,
                    property_types=property_types,
                )
            finally:
                await user_api_client.close()

            text = json.dumps(result, default=str, ensure_ascii=False)
            return [{"type": "text", "text": f"{text}{call_id_suffix}"}]

        except ValueError as e:
            return [{"type": "text", "text": f"Authentication Error: {e}{call_id_suffix}"}]
        except Exception as e:
            # Log the traceback for ops; return only the actionable message to
            # the LLM (no /app/ paths, no Python stack frames).
            logger.exception("Tool %s failed", name)
            return [{"type": "text", "text": f"Error: {e}{call_id_suffix}"}]

    mcp._mcp_server.list_tools()(list_tools_with_dynamic)
    mcp._mcp_server.call_tool(validate_input=False)(call_tool_with_dynamic)

def main():
    import argparse

    import uvicorn
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import JSONResponse, Response
    from starlette.routing import Route

    parser = argparse.ArgumentParser(description="Cekura OpenAPI MCP Server")
    parser.add_argument("--port", type=int, default=8001, help="Port to run the HTTP server on (default: 8001)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host to bind to (default: 0.0.0.0)")
    args = parser.parse_args()

    logger.info("Starting Cekura OpenAPI MCP Server...")

    asyncio.run(initialize_server())

    logger.info(f"Server initialized successfully. Running on http://{args.host}:{args.port}/mcp")

    # Paths that intentionally bypass auth — health probes and OAuth
    # discovery documents. Discovery URLs must be reachable unauthenticated
    # so clients can find the authorization server before they have a token.
    NO_AUTH_PATHS = {
        "/mcp/health",
        "/mcp/healthz",
        "/mcp/.well-known/oauth-protected-resource",
        "/mcp/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
    }

    # WWW-Authenticate value used on 401 responses. Points clients at the
    # canonical (ALB-reachable) protected-resource metadata URL per RFC 9728.
    WWW_AUTH_HEADER = (
        f'Bearer resource_metadata="{MCP_SERVER_URL}/.well-known/oauth-protected-resource", '
        f'error="invalid_token"'
    )

    class CredentialMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            path = request.url.path
            if path in NO_AUTH_PATHS:
                return await call_next(request)

            # Bearer token — OAuth web users or agent/CLI JWT passthrough
            has_bearer = False
            auth_header = request.headers.get('Authorization') or request.headers.get('authorization')
            if auth_header and auth_header.lower().startswith('bearer '):
                token = auth_header[7:]
                request_bearer_token.set(token)
                has_bearer = True
                logger.debug("Bearer token credential set for request")

                # Short-circuit expired oauth_access JWTs with 401 +
                # WWW-Authenticate so the MCP client refreshes the token.
                # Signature validation stays the backend's job.
                try:
                    claims = jwt.decode(token, options={"verify_signature": False})
                except jwt.PyJWTError:
                    claims = None
                if claims and claims.get("type") == "oauth_access":
                    exp = claims.get("exp")
                    if isinstance(exp, (int, float)) and exp < time.time() - OAUTH_EXP_SKEW_SECONDS:
                        return JSONResponse(
                            {"error": "invalid_token",
                             "error_description": "OAuth access token expired"},
                            status_code=401,
                            headers={"WWW-Authenticate": WWW_AUTH_HEADER},
                        )

            # API key header — legacy mcp-remote / Claude Desktop
            has_api_key = False
            api_key = request.headers.get('X-CEKURA-API-KEY') or request.headers.get('x-cekura-api-key')
            if api_key:
                request_api_key.set(api_key)
                has_api_key = True
                logger.debug("API key credential set for request")

            # Base URL override — only honoured when ALLOW_BASE_URL_OVERRIDE=true (dev/staging)
            if _ALLOW_BASE_URL_OVERRIDE:
                base_url_override = request.headers.get('X-CEKURA-BASE-URL') or request.headers.get('x-cekura-base-url')
                if base_url_override:
                    request_base_url.set(base_url_override.rstrip("/"))

            # Connection-level conversation identifier (e.g. set by the Cekura
            # sandbox at sandbox start). Per-call ``_meta`` overrides this.
            conversation_header = (
                request.headers.get('X-Cekura-Conversation-Id')
                or request.headers.get('x-cekura-conversation-id')
            )
            if conversation_header:
                request_conversation_id.set(conversation_header)

            # MCP protocol session identifier (issued on `initialize`).
            mcp_session_header = (
                request.headers.get('Mcp-Session-Id')
                or request.headers.get('mcp-session-id')
            )
            if mcp_session_header:
                request_mcp_session_id.set(mcp_session_header)

            # Enforce auth at the transport layer for MCP traffic. Without this,
            # FastMCP's `initialize` and `tools/list` succeed unauthenticated,
            # which makes Claude Desktop's connector flow conclude "no auth
            # required" and skip the OAuth handshake entirely.
            if path.startswith("/mcp") and not has_bearer and not has_api_key:
                return JSONResponse(
                    {
                        "error": "unauthorized",
                        "error_description": "Authenticate via OAuth (Bearer) or X-CEKURA-API-KEY",
                    },
                    status_code=401,
                    headers={"WWW-Authenticate": WWW_AUTH_HEADER},
                )

            return await call_next(request)

    async def health_check(request):
        return JSONResponse({
            "status": "healthy",
            "service": "cekura-mcp-server",
            "tools_registered": len(operations_registry)
        })

    async def monitoring_session_create(request):
        # Forwards the Claude Code session transcript to the Cekura observability
        # ingestion endpoint (POST /observability/v1/observe/) as a CallLog.
        # Agent ID + API key are read from env for now — eventually these should
        # come from the request (per-user credentials, per-skill agent mapping).
        try:
            payload = await request.json()
        except Exception as e:
            return JSONResponse({"error": f"invalid JSON body: {e}"}, status_code=400)

        session_id = payload.get("session_id")
        skill = payload.get("skill")
        transcript_jsonl = payload.get("transcript_jsonl")

        if not isinstance(session_id, str) or not session_id:
            return JSONResponse({"error": "missing or invalid 'session_id'"}, status_code=400)
        if not isinstance(skill, str) or not skill:
            return JSONResponse({"error": "missing or invalid 'skill'"}, status_code=400)
        if not isinstance(transcript_jsonl, str):
            return JSONResponse({"error": "missing or invalid 'transcript_jsonl' (expected string)"}, status_code=400)

        agent_id_raw = os.environ.get("CEKURA_OBSERVE_AGENT_ID", "").strip()
        if not agent_id_raw:
            return JSONResponse({"error": "CEKURA_OBSERVE_AGENT_ID is not configured on the server"}, status_code=500)
        try:
            agent_id = int(agent_id_raw)
        except ValueError:
            return JSONResponse({"error": f"CEKURA_OBSERVE_AGENT_ID must be an integer, got {agent_id_raw!r}"}, status_code=500)

        api_key = os.environ.get("CEKURA_OBSERVE_API_KEY") or os.environ.get("CEKURA_API_KEY")
        if not api_key:
            return JSONResponse({"error": "CEKURA_OBSERVE_API_KEY (or CEKURA_API_KEY) is not configured on the server"}, status_code=500)

        transcript = _claude_jsonl_to_cekura_transcript(transcript_jsonl)
        if not transcript:
            logger.info(f"observe: session {session_id} produced empty transcript after conversion — skipping")
            return JSONResponse({"status": "skipped", "reason": "empty transcript after conversion"})

        now = datetime.now(timezone.utc)
        # Suffix the call_id with a per-event timestamp so repeated Stop-hook
        # snapshots for the same Claude session don't collide on the backend.
        call_id = f"claude-{session_id}-{int(now.timestamp())}"[:100]

        body = {
            "agent": agent_id,
            "call_id": call_id,
            "transcript_type": "cekura",
            "transcript_json": transcript,
            "timestamp": now.isoformat(),
            "metadata": {
                "source": "claude-code",
                "skill": skill,
                "claude_session_id": session_id,
            },
        }

        observe_url = f"{server_config.base_url}/observability/v1/observe/"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=5.0)) as client:
                resp = await client.post(
                    observe_url,
                    headers={
                        "Content-Type": "application/json",
                        "X-CEKURA-API-KEY": api_key,
                    },
                    json=body,
                )
        except httpx.RequestError as e:
            logger.error(f"observe: network error for session {session_id}: {e}")
            return JSONResponse({"error": f"observe request failed: {e}"}, status_code=502)

        if resp.status_code >= 400:
            logger.error(f"observe: upstream returned {resp.status_code} for session {session_id}: {resp.text[:500]}")
            return JSONResponse(
                {"error": "observe upstream rejected request", "status": resp.status_code, "body": resp.text[:500]},
                status_code=502,
            )

        logger.info(f"observe: forwarded session {session_id} as call_id={call_id} (skill={skill}, turns={len(transcript)})")
        return JSONResponse({"status": "ok", "call_id": call_id, "upstream_status": resp.status_code})

    def _has_api_key(request) -> bool:
        return bool(
            request.headers.get('X-CEKURA-API-KEY')
            or request.headers.get('x-cekura-api-key')
        )

    async def oauth_protected_resource(request):
        if _has_api_key(request):
            return Response(status_code=404)
        return JSONResponse({
            "resource": MCP_SERVER_URL,
            "authorization_servers": [MCP_ISSUER_URL],
        })

    async def oauth_as_metadata(request):
        if _has_api_key(request):
            return Response(status_code=404)
        return JSONResponse({
            "issuer": MCP_ISSUER_URL,
            "authorization_endpoint": f"{MCP_ISSUER_URL}/user/oauth/authorize",
            "token_endpoint": f"{MCP_ISSUER_URL}/user/oauth/token",
            "revocation_endpoint": f"{MCP_ISSUER_URL}/user/oauth/revoke",
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
        })

    app = mcp.streamable_http_app()

    # Register OAuth discovery routes under both /mcp/.well-known/... (canonical,
    # reachable through the prod ALB which only forwards /mcp/*) and the root
    # /.well-known/... form (works for direct-port access in dev and is robust
    # against any future ALB rule broadening).
    app.router.routes.insert(0, Route("/mcp/.well-known/oauth-protected-resource", oauth_protected_resource))
    app.router.routes.insert(1, Route("/mcp/.well-known/oauth-authorization-server", oauth_as_metadata))
    app.router.routes.insert(2, Route("/.well-known/oauth-protected-resource", oauth_protected_resource))
    app.router.routes.insert(3, Route("/.well-known/oauth-authorization-server", oauth_as_metadata))
    app.router.routes.insert(4, Route("/mcp/health", health_check))
    app.router.routes.insert(5, Route("/mcp/healthz", health_check))
    app.router.routes.insert(6, Route("/mcp/monitoring/sessions", monitoring_session_create, methods=["POST"]))

    app.add_middleware(CredentialMiddleware)
    logger.info("Credential middleware added (API key + Bearer token support)")
    logger.info(f"OAuth discovery: {MCP_SERVER_URL}/.well-known/oauth-protected-resource → {MCP_ISSUER_URL}")
    logger.info("Health check endpoints: /mcp/health, /mcp/healthz")

    logging.getLogger("uvicorn.access").addFilter(HealthCheckFilter())

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
