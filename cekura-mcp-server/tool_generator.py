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


def generate_tool_description(operation: Operation) -> str:
    if operation.description:
        desc = operation.description.strip()
        if len(desc) > 200:
            desc = desc[:197] + "..."
        return desc

    if operation.summary:
        desc = operation.summary.strip()
        if len(desc) > 200:
            desc = desc[:197] + "..."
        return desc

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
