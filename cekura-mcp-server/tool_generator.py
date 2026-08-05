from typing import Dict, Any, Optional
from openapi_parser import Operation
from mcp.types import ToolAnnotations
import re
import json
import sys
import hashlib
from pathlib import Path


# POST endpoints whose final path segment marks them as destructive even though
# they aren't DELETE. These remove or terminate things server-side.
_DESTRUCTIVE_POST_PREFIXES = ('delete_', 'unmark_', 'discard-', 'discard_', 'cancel-', 'cancel_')
_DESTRUCTIVE_POST_EXACT = {'end_call', 'end_calls'}


def compute_annotations(operation: Operation) -> ToolAnnotations:
    """Return MCP tool annotations classifying a tool as read-only / non-destructive / destructive.

    Rules:
        GET           → readOnlyHint=True
        DELETE        → readOnlyHint=False, destructiveHint=True
        POST (delete/end/unmark/discard/cancel) → readOnlyHint=False, destructiveHint=True
        POST (other)  → readOnlyHint=False, destructiveHint=False
        PUT, PATCH    → readOnlyHint=False, destructiveHint=False
    """
    method = operation.method.upper()
    read_only = (method == 'GET')

    explicit = (operation.extensions or {}).get('x-mcp-destructive')
    if explicit is not None:
        return ToolAnnotations(readOnlyHint=read_only, destructiveHint=bool(explicit))

    if method == 'GET':
        return ToolAnnotations(readOnlyHint=True)
    if method == 'DELETE':
        return ToolAnnotations(readOnlyHint=False, destructiveHint=True)
    if method == 'POST':
        last_segment = operation.path.rstrip('/').split('/')[-1].lower()
        is_destructive = (
            last_segment in _DESTRUCTIVE_POST_EXACT
            or any(last_segment.startswith(p) for p in _DESTRUCTIVE_POST_PREFIXES)
        )
        return ToolAnnotations(readOnlyHint=False, destructiveHint=is_destructive)
    return ToolAnnotations(readOnlyHint=False, destructiveHint=False)


_OVERLAY_CACHE: Optional[Dict[str, Any]] = None


def load_tool_overlays() -> Dict[str, Any]:
    """Load per-tool MCP enrichments. Cached on first call."""
    global _OVERLAY_CACHE
    if _OVERLAY_CACHE is not None:
        return _OVERLAY_CACHE

    overlay_file = Path(__file__).parent / 'mcp_tools.json'
    if not overlay_file.exists():
        _OVERLAY_CACHE = {}
        return _OVERLAY_CACHE

    try:
        with open(overlay_file, 'r') as f:
            data = json.load(f)
        _OVERLAY_CACHE = {k: v for k, v in data.items() if not k.startswith('_')}
        return _OVERLAY_CACHE
    except Exception as e:
        print(f"Error loading mcp_tools.json: {e}", file=sys.stderr)
        _OVERLAY_CACHE = {}
        return _OVERLAY_CACHE


def apply_overlay_to_description(tool_name: str, description: str) -> str:
    overlay = load_tool_overlays().get(tool_name)
    if not overlay:
        return description

    parts = []
    if overlay.get('destructive'):
        parts.append("⚠ DESTRUCTIVE OPERATION — irreversible. See suffix for cascade behavior.")
    prefix = overlay.get('description_prefix')
    if prefix:
        parts.append(prefix)
    parts.append(description)
    suffix = overlay.get('description_suffix')
    if suffix:
        parts.append(suffix)

    return "\n\n".join(p for p in parts if p)


def apply_overlay_to_schema(tool_name: str, schema: Dict[str, Any]) -> Dict[str, Any]:
    overlay = load_tool_overlays().get(tool_name) or {}

    if 'required' in overlay:
        existing = set(schema.get('required', []) or [])
        existing.update(overlay['required'])
        schema['required'] = sorted(existing)

    # Force specific body fields to a given JSON-schema type. For fields the
    # backend stores as JSON objects but the OpenAPI spec types as `string`,
    # declaring `object` makes the HTTP client coerce the model's stringified
    # value to a dict before sending — the backend rejects a raw string.
    # Scoped per-tool via mcp_tools.json.
    for field_name, ptype in (overlay.get('property_types') or {}).items():
        prop = (schema.get('properties') or {}).get(field_name)
        if isinstance(prop, dict):
            prop['type'] = ptype
            if ptype == 'object':
                prop.setdefault('additionalProperties', True)

    # Examples precedence (JSON-Schema draft-07 `examples` array, one per request body):
    #   1. Overlay `examples: [...]` → use verbatim; openapi examples ignored.
    #   2. Else, merge openapi-spec examples with overlay.example_request. Overlay filters:
    #        - `example_names: [...]` → keep only openapi examples whose name matches.
    #        - `max_examples: N` → cap the result at N. If absent, use global cap.
    schema_examples = _resolve_examples_for_tool(schema, overlay)
    # Clean up the private key from the parser either way.
    schema.pop('_openapi_examples', None)
    if schema_examples:
        schema['examples'] = schema_examples

    return schema


def _resolve_examples_for_tool(schema: Dict[str, Any], overlay: Dict[str, Any]) -> list:
    """Pick which example bodies to expose, per precedence model."""
    # 1. Full override via overlay.
    if 'examples' in overlay:
        return list(overlay['examples'])

    openapi_examples = schema.get('_openapi_examples') or []

    if not openapi_examples and 'example_request' in overlay:
        return [overlay['example_request']]

    # Filter by name if overlay specifies.
    if 'example_names' in overlay:
        wanted = set(overlay['example_names'])
        openapi_examples = [
            e for e in openapi_examples if e.get('name') in wanted
        ]

    # Cap by overlay.max_examples, else global env cap (CEKURA_MAX_EXAMPLES_PER_TOOL).
    cap = overlay.get('max_examples')
    if cap is None:
        cap = _global_max_examples_per_tool()
    if cap is not None and cap >= 0:
        openapi_examples = openapi_examples[:cap]

    result = [e['value'] for e in openapi_examples if 'value' in e]

    # If the openapi path produced nothing and there's a legacy example_request, fall back.
    if not result and 'example_request' in overlay:
        return [overlay['example_request']]

    return result


_CACHED_MAX_EXAMPLES = None


def _global_max_examples_per_tool() -> int:
    """Read once. Defaults to 2. Set `CEKURA_MAX_EXAMPLES_PER_TOOL=0` to disable openapi-sourced examples."""
    global _CACHED_MAX_EXAMPLES
    if _CACHED_MAX_EXAMPLES is not None:
        return _CACHED_MAX_EXAMPLES
    import os
    raw = os.getenv('CEKURA_MAX_EXAMPLES_PER_TOOL')
    if raw is None:
        _CACHED_MAX_EXAMPLES = 2
    else:
        try:
            _CACHED_MAX_EXAMPLES = int(raw)
        except ValueError:
            _CACHED_MAX_EXAMPLES = 2
    return _CACHED_MAX_EXAMPLES


# Strip a trailing numeric collision suffix (`_2`, `_3`, ...) from the tool
# name. `_v2` and other version suffixes are preserved — only pure-digit
# trailers match.
_COLLISION_SUFFIX = re.compile(r"_\d+$")


def generate_tool_name(operation: Operation) -> str:
    """Convert an OpenAPI operationId into an MCP tool name."""
    MAX_TOOL_NAME_LENGTH = 64

    if operation.operation_id:
        name = operation.operation_id.replace("-", "_")
        name = re.sub(r'[^a-zA-Z0-9_]', '', name)
    else:
        path_slug = operation.path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
        path_slug = re.sub(r'[^a-zA-Z0-9_]', '', path_slug)
        method_name = operation.method.lower()
        name = f"{method_name}_{path_slug}"

    name = _COLLISION_SUFFIX.sub("", name)

    if len(name) > MAX_TOOL_NAME_LENGTH:
        hash_suffix = hashlib.md5(name.encode()).hexdigest()[:8]
        max_prefix_length = MAX_TOOL_NAME_LENGTH - len(hash_suffix) - 1
        name = f"{name[:max_prefix_length]}_{hash_suffix}"

    return name


# Human-readable tool titles (MCP spec 2025-06-18 `Tool.title`). Directory
# reviewers (e.g. the Anthropic Connectors Directory) require every tool to
# carry one. Precedence: the operation's OpenAPI `summary` (the backend is
# authoritative — vocera.backend `add_mcp_configuration` guarantees one on
# every exposed operation), then a `title` overlay in `mcp_tools.json`, then
# derivation from the tool name. Overlay and derivation exist only for specs
# predating the backend guarantee — overlay `title` entries should be removed
# once the regenerated spec carries the summary.

# Domain words the naive title-caser would mangle.
_TITLE_WORD_MAP = {
    'aiagents': 'AI Agents', 'ai': 'AI', 'api': 'API', 'csv': 'CSV',
    'json': 'JSON', 'sip': 'SIP', 'vapi': 'VAPI', 'webrtc': 'WebRTC',
    'livekit': 'LiveKit', 'elevenlabs': 'ElevenLabs', 'pipecat': 'Pipecat',
    'retell': 'Retell', 'agora': 'Agora', 'dtmf': 'DTMF', 'url': 'URL',
    'id': 'ID', 'ids': 'IDs', 'mcp': 'MCP', 'ivr': 'IVR', 'cekura': 'Cekura',
    'slack': 'Slack', 'v1': 'v1', 'v2': 'v2', 'bg': 'in Background',
}

# Leading verb for title phrasing, keyed by the name token that implies it.
_TITLE_VERB_MAP = {
    'list': 'List', 'create': 'Create', 'retrieve': 'Get', 'update': 'Update',
    'destroy': 'Delete', 'delete': 'Delete', 'run': 'Run',
    'generate': 'Generate', 'duplicate': 'Duplicate', 'upload': 'Upload',
    'evaluate': 'Evaluate', 'rerun': 'Rerun', 'copy': 'Copy',
    'preview': 'Preview', 'improve': 'Improve', 'simplify': 'Simplify',
    'dismiss': 'Dismiss', 'reproduce': 'Reproduce', 'end': 'End',
    'move': 'Move', 'rename': 'Rename', 'search': 'Search',
    'enable': 'Enable', 'disable': 'Disable', 'fix': 'Fix', 'mark': 'Mark',
    'process': 'Process',
}

# REST-suffix verbs from generic CRUD routes. When one of these is the final
# token but the name carries a more specific verb earlier (`scenarios_
# duplicate_create`), the earlier verb names the action better.
_GENERIC_SUFFIX_VERBS = {'create', 'update', 'destroy', 'retrieve', 'list', 'delete'}


def _title_words(tokens: list) -> str:
    # Collapse adjacent duplicates (`slack_slack_workspaces_list`).
    deduped = [t for i, t in enumerate(tokens) if t and (i == 0 or t != tokens[i - 1])]
    return " ".join(_TITLE_WORD_MAP.get(t, t.capitalize()) for t in deduped)


def _find_verb(tokens: list) -> Optional[int]:
    """Index of the action token, scanning right-to-left.

    A verb token directly after `from` is a noun (`test_sets_create_from_run`).
    A generic REST suffix yields to a more specific verb earlier in the name,
    provided that verb still has an object before it.
    """
    verb_idx = None
    for i in range(len(tokens) - 1, -1, -1):
        if tokens[i] in _TITLE_VERB_MAP and (i == 0 or tokens[i - 1] != 'from'):
            verb_idx = i
            break
    if (
        verb_idx == len(tokens) - 1
        and tokens[verb_idx] in _GENERIC_SUFFIX_VERBS
    ):
        for i in range(verb_idx - 1, 0, -1):
            if tokens[i] in _TITLE_VERB_MAP and tokens[i - 1] != 'from':
                return i
    return verb_idx


def generate_tool_title(tool_name: str, operation: Optional[Operation] = None) -> str:
    """Return the human-readable title for an MCP tool.

    Precedence: the operation's OpenAPI `summary`, then overlay `title`
    (bridge for specs predating the backend summary guarantee), then name
    derivation (`..._progress` phrasing, verb-first phrasing around the
    action token, plain title-casing).
    """
    if operation is not None and (operation.summary or '').strip():
        return operation.summary.strip()

    overlay = load_tool_overlays().get(tool_name) or {}
    if overlay.get('title'):
        return overlay['title']

    name = tool_name.replace('partial_update', 'update')

    # Progress pollers: "<thing>_progress[_retrieve]" → "Get <Thing> Progress".
    m = re.match(r'^(.*?)_progress(?:_retrieve)?$', name)
    if m:
        return f"Get {_title_words(m.group(1).split('_'))} Progress"

    tokens = name.split('_')
    verb_idx = _find_verb(tokens)
    if verb_idx is None:
        return _title_words(tokens)

    verb = _TITLE_VERB_MAP[tokens[verb_idx]]
    obj = tokens[:verb_idx]
    # Qualifiers between the verb and a displaced trailing REST suffix.
    qualifiers = tokens[verb_idx + 1:]
    if qualifiers and qualifiers[-1] in _GENERIC_SUFFIX_VERBS:
        qualifiers = qualifiers[:-1]
    if obj and obj[-1] == 'bulk':
        verb = f"Bulk {verb}"
        obj = obj[:-1]
    if not obj:
        # Verb-first names like `end_calls` or `delete_runs`.
        return f"{verb} {_title_words(qualifiers)}".strip()
    title = f"{verb} {_title_words(obj)}"
    if qualifiers:
        title += f" ({_title_words(qualifiers)})"
    return title


MAX_DESCRIPTION_LENGTH = 2000


def _truncate_sentence_aware(desc: str, limit: int) -> str:
    if len(desc) <= limit:
        return desc
    head = desc[: limit - 3]
    # Prefer breaking at a sentence-ending boundary over cutting mid-word.
    for sep in ['. ', '.\n', '! ', '? ']:
        idx = head.rfind(sep)
        if idx >= limit // 2:
            return head[: idx + 1] + " ..."
    # Fall back to last whitespace.
    idx = head.rfind(' ')
    if idx >= limit // 2:
        return head[:idx] + " ..."
    return head + "..."


def generate_tool_description(operation: Operation) -> str:
    if operation.description:
        return _truncate_sentence_aware(operation.description.strip(), MAX_DESCRIPTION_LENGTH)

    if operation.summary:
        return _truncate_sentence_aware(operation.summary.strip(), MAX_DESCRIPTION_LENGTH)

    method = operation.method.upper()
    path = operation.path
    return f"{method} {path}"


# Shared hint appended to any tool whose input schema accepts an organization
# or project reference. Edit here once; every applicable tool picks it up.
# Tools can opt out via the overlay flag `suppress_org_project_hint: true`.
ORG_PROJECT_HINT = (
    "If neither organization_id nor project_id is known, call user_organizations_list "
    "(returns your accessible organisations) or projects_list (returns your "
    "accessible projects) first, then retry with the chosen ID."
)


_ORG_PROJECT_FIELD_NAMES = (
    "organization_id", "project_id",
    "organization", "project",
)


def maybe_append_org_project_hint(
    tool_name: str,
    input_schema: Dict[str, Any],
    description: str,
) -> str:
    """Append the shared org/project hint when the schema accepts either field."""
    properties = input_schema.get("properties") or {}
    if not any(name in properties for name in _ORG_PROJECT_FIELD_NAMES):
        return description

    overlay = load_tool_overlays().get(tool_name) or {}
    if overlay.get("suppress_org_project_hint"):
        return description

    return f"{description}\n\n{ORG_PROJECT_HINT}"


def build_input_schema(operation: Operation, parser: Any) -> Dict[str, Any]:
    return parser.build_parameter_schema(operation)


def should_include_operation(operation: Operation) -> bool:
    if operation.deprecated:
        return False
    return bool((operation.extensions or {}).get('x-mcp-expose'))
