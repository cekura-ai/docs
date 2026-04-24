"""Validate tool_overlays.json against the live openapi.json + whitelist.

Run from the MCP server directory:

    python3 validate_overlays.py

Exits non-zero when any finding is at `error` level. Used both as a CLI (CI) and
as a library (imported by `tests/test_overlays.py` and by the MCP server
startup path for non-fatal drift warnings).
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from openapi_parser import Operation, load_openapi_spec
from tool_generator import (
    generate_tool_name,
    load_documented_apis_whitelist,
    load_tool_overlays,
    should_include_operation,
)

# Operations whose MCP tool name is a bare DELETE but intentionally non-destructive
# (e.g., soft deletes that do not cascade). Add here with justification when needed.
DESTRUCTIVE_OPTOUT: Set[str] = set()


@dataclass
class Finding:
    level: str  # "error" or "warning"
    category: str
    tool: Optional[str]
    message: str


@dataclass
class _Context:
    operations_by_tool: Dict[str, Operation]
    schema_by_tool: Dict[str, Dict]
    overlays: Dict[str, Dict]
    findings: List[Finding] = field(default_factory=list)

    def add(self, level: str, category: str, tool: Optional[str], message: str) -> None:
        self.findings.append(Finding(level=level, category=category, tool=tool, message=message))


def _build_context(
    spec_path: Path,
    overlays: Optional[Dict] = None,
) -> _Context:
    parser = load_openapi_spec(str(spec_path))
    whitelist = load_documented_apis_whitelist()
    overlays = overlays if overlays is not None else load_tool_overlays()

    operations_by_tool: Dict[str, Operation] = {}
    schema_by_tool: Dict[str, Dict] = {}

    for op in parser.extract_operations():
        if not should_include_operation(op, whitelist=whitelist):
            continue
        name = generate_tool_name(op)
        operations_by_tool[name] = op
        try:
            schema_by_tool[name] = parser.build_parameter_schema(op)
        except Exception as e:
            schema_by_tool[name] = {"type": "object", "properties": {}, "required": []}
            # Non-fatal — report and keep going.
            operations_by_tool[name] = op
            print(f"warn: could not build schema for {name}: {e}", file=sys.stderr)

    return _Context(
        operations_by_tool=operations_by_tool,
        schema_by_tool=schema_by_tool,
        overlays=overlays,
    )


def _check_orphans(ctx: _Context) -> None:
    for tool_name in ctx.overlays:
        if tool_name in ctx.operations_by_tool:
            continue
        ctx.add(
            "error",
            "orphan",
            tool_name,
            f"Overlay entry '{tool_name}' does not match any registered tool. "
            "Either the tool was renamed/removed upstream, or the overlay key is a typo.",
        )


def _check_required_fields(ctx: _Context) -> None:
    for tool_name, overlay in ctx.overlays.items():
        required = overlay.get("required") or []
        if not required:
            continue
        schema = ctx.schema_by_tool.get(tool_name)
        if schema is None:
            # Handled by orphan check.
            continue
        known = set((schema.get("properties") or {}).keys())
        for field_name in required:
            if field_name not in known:
                ctx.add(
                    "error",
                    "missing_required_field",
                    tool_name,
                    f"Overlay declares '{field_name}' as required, but the tool's input schema "
                    f"has no such property. Schema properties: {sorted(known)}",
                )


def _check_example_fields(ctx: _Context) -> None:
    for tool_name, overlay in ctx.overlays.items():
        example = overlay.get("example_request")
        if not example:
            continue
        schema = ctx.schema_by_tool.get(tool_name)
        if schema is None:
            continue
        known = set((schema.get("properties") or {}).keys())
        for field_name in example.keys():
            if field_name not in known:
                ctx.add(
                    "warning",
                    "stale_example_field",
                    tool_name,
                    f"example_request references '{field_name}' which is not in the tool's "
                    f"input schema. Likely the field was renamed or removed upstream.",
                )


def _check_schema_example_fields(ctx: _Context) -> None:
    """Flag backend-sourced `@extend_schema(examples=[...])` entries whose keys
    reference fields not in the tool's input schema (renamed / removed upstream).

    Warning-level: backend examples are surfaced by default, but a stale field
    in an example won't break callers — the backend will just reject the call
    with a clear 400. Still worth flagging so backend authors notice."""
    for tool_name, schema in ctx.schema_by_tool.items():
        openapi_examples = schema.get("_openapi_examples") or []
        if not openapi_examples:
            continue
        known = set((schema.get("properties") or {}).keys())
        for example in openapi_examples:
            value = example.get("value") or {}
            if not isinstance(value, dict):
                continue
            unknown = [k for k in value.keys() if k not in known]
            if unknown:
                name = example.get("name") or "<unnamed>"
                ctx.add(
                    "warning",
                    "stale_schema_example_field",
                    tool_name,
                    f"Backend example '{name}' references field(s) {sorted(unknown)} "
                    "that are not in the tool's input schema. Likely a field was renamed "
                    "or removed upstream without updating @extend_schema(examples=...).",
                )


def _check_destructive_coverage(ctx: _Context) -> None:
    """Verify that every DELETE / *_destroy tool has a ⚠ / 'Irreversible' marker.

    The marker can live in two places (preferred order):
    1. Upstream — the OpenAPI operation description starts with '⚠' or contains 'Irreversible'
       (set via @extend_schema on the view, then propagated by manage.py spectacular).
    2. Overlay fallback — the overlay entry has `destructive: true`, which causes the MCP
       server to prepend the DESTRUCTIVE marker at registration time.

    If neither is present the tool silently looks like a safe read operation to LLM agents.
    """
    for tool_name, op in ctx.operations_by_tool.items():
        looks_destructive = (
            op.method.upper() == "DELETE"
            or tool_name.endswith("_destroy")
            or tool_name.endswith("_delete")
        )
        if not looks_destructive:
            continue
        if tool_name in DESTRUCTIVE_OPTOUT:
            continue

        # Check overlay flag (legacy / fallback path)
        overlay = ctx.overlays.get(tool_name, {})
        if overlay.get("destructive") is True:
            continue

        # Check upstream OpenAPI description for ⚠ or 'Irreversible'
        upstream_desc = (op.description or op.summary or "").strip()
        if "⚠" in upstream_desc or "irreversible" in upstream_desc.lower():
            continue

        ctx.add(
            "error",
            "destructive_missing",
            tool_name,
            f"Operation {op.method} {op.path} registers as tool '{tool_name}' but has no "
            "destructive marker. Add '⚠ Irreversible...' to the @extend_schema description "
            "upstream, or set 'destructive: true' in the overlay as a fallback.",
        )


def run_checks(
    spec_path: Optional[Path] = None,
    overlays: Optional[Dict] = None,
) -> List[Finding]:
    """Run all overlay-drift checks. Returns a list of findings (may be empty)."""
    here = Path(__file__).parent
    spec_path = spec_path or (here.parent / "openapi.json")
    ctx = _build_context(spec_path=spec_path, overlays=overlays)

    _check_orphans(ctx)
    _check_required_fields(ctx)
    _check_example_fields(ctx)
    _check_schema_example_fields(ctx)
    _check_destructive_coverage(ctx)

    return ctx.findings


def _format(findings: List[Finding]) -> str:
    if not findings:
        return "✓ No overlay drift detected."
    by_level: Dict[str, List[Finding]] = {"error": [], "warning": []}
    for f in findings:
        by_level.setdefault(f.level, []).append(f)

    lines = []
    for level in ("error", "warning"):
        bucket = by_level.get(level) or []
        if not bucket:
            continue
        lines.append(f"\n{'=' * 60}")
        lines.append(f"{level.upper()}  ({len(bucket)})")
        lines.append("=" * 60)
        for f in bucket:
            tool = f.tool or "<global>"
            lines.append(f"  [{f.category}] {tool}")
            lines.append(f"    {f.message}")
    return "\n".join(lines)


def main() -> int:
    findings = run_checks()
    print(_format(findings))
    errors = [f for f in findings if f.level == "error"]
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
