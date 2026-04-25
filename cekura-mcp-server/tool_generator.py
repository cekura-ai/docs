from typing import Dict, Any, Optional, Set, Tuple
from openapi_parser import Operation
import re
import json
import sys
import hashlib
from pathlib import Path


def load_documented_apis_whitelist() -> Optional[Set[Tuple[str, str]]]:
    whitelist_file = Path(__file__).parent / 'documented_apis.json'

    if not whitelist_file.exists():
        print(f"Warning: Whitelist file not found at {whitelist_file}", file=sys.stderr)
        return None

    try:
        with open(whitelist_file, 'r') as f:
            data = json.load(f)

        whitelist = set()
        for endpoint in data.get('endpoints', []):
            method = endpoint['method'].upper()
            path = endpoint['path']
            whitelist.add((method, path))

        return whitelist
    except Exception as e:
        print(f"Error loading documented_apis.json: {e}", file=sys.stderr)
        return None


_OVERLAY_CACHE: Optional[Dict[str, Any]] = None


def load_tool_overlays() -> Dict[str, Any]:
    """Load per-tool MCP enrichments. Cached on first call."""
    global _OVERLAY_CACHE
    if _OVERLAY_CACHE is not None:
        return _OVERLAY_CACHE

    overlay_file = Path(__file__).parent / 'tool_overlays.json'
    if not overlay_file.exists():
        _OVERLAY_CACHE = {}
        return _OVERLAY_CACHE

    try:
        with open(overlay_file, 'r') as f:
            data = json.load(f)
        # Drop any underscore-prefixed keys (reserved for comments/meta).
        _OVERLAY_CACHE = {k: v for k, v in data.items() if not k.startswith('_')}
        return _OVERLAY_CACHE
    except Exception as e:
        print(f"Error loading tool_overlays.json: {e}", file=sys.stderr)
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

    # Examples precedence (JSON-Schema draft-07 `examples` array, one per request body):
    #   1. Overlay `examples: [...]` → use verbatim; backend/openapi examples ignored.
    #   2. Else, merge openapi-spec examples (from drf-spectacular @extend_schema(examples=...))
    #      with overlay.example_request (legacy fallback). Overlay filters:
    #        - `example_names: [...]` → keep only openapi examples whose name matches.
    #        - `max_examples: N` → cap the result at N. If absent, use global cap.
    # Runs even when no overlay entry exists — backend examples still flow through under defaults.
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


def generate_tool_name(operation: Operation) -> str:
    MAX_TOOL_NAME_LENGTH = 64

    if operation.operation_id:
        name = operation.operation_id.replace("-", "_")
        name = re.sub(r'[^a-zA-Z0-9_]', '', name)
    else:
        path_slug = operation.path.strip("/").replace("/", "_").replace("{", "").replace("}", "")
        path_slug = re.sub(r'[^a-zA-Z0-9_]', '', path_slug)
        method_name = operation.method.lower()
        name = f"{method_name}_{path_slug}"

    if len(name) > MAX_TOOL_NAME_LENGTH:
        hash_suffix = hashlib.md5(name.encode()).hexdigest()[:8]
        max_prefix_length = MAX_TOOL_NAME_LENGTH - len(hash_suffix) - 1
        name = f"{name[:max_prefix_length]}_{hash_suffix}"

    return name


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


def build_input_schema(operation: Operation, parser: Any) -> Dict[str, Any]:
    return parser.build_parameter_schema(operation)


def should_include_operation(
    operation: Operation,
    filter_tags: list = None,
    exclude_ops: list = None,
    whitelist: Optional[Set[Tuple[str, str]]] = None
) -> bool:
    if operation.deprecated:
        return False

    if whitelist is not None:
        method = operation.method.upper()
        path = operation.path.rstrip('/')

        if (method, path) in whitelist:
            return True

        if (method, path + '/') in whitelist:
            return True

        return False

    if exclude_ops and operation.operation_id in exclude_ops:
        return False

    if "external" in operation.path.lower() or (operation.operation_id and "external" in operation.operation_id.lower()):
        return False

    if filter_tags:
        if not operation.tags:
            return False
        has_matching_tag = any(tag in filter_tags for tag in operation.tags)
        if not has_matching_tag:
            return False

    return True
