#!/usr/bin/env python3
"""Check the JSON files the docs site and the MCP server are built from.

Git merges these files as text, and a text merge can produce JSON that still
parses but is wrong: two branches that add the same object at different
positions leave a duplicate key, and every JSON parser silently keeps only the
last copy. This check fails on that, and on the other drift that makes the
files merge badly:

- ``openapi.json`` must be in canonical form (2-space indent, UTF-8, trailing
  newline) with no duplicate keys. It is generated, never edited by hand.
- ``cekura-mcp-server/mcp_tools.json`` must have no duplicate keys and keep its
  top-level keys sorted, so overlays added by different PRs land on different
  lines.
- ``mint.json`` must have no duplicate keys.

With ``--unchanged-from BASE``, ``openapi.json`` must also parse to the same
content as BASE: only the automated release PR may change what it says.

Usage: python3 scripts/check_generated_json.py [--root DIR] [--unchanged-from BASE]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def _load(path: Path, problems: list[str]) -> tuple[object, str] | None:
    """Parse a file, reporting duplicate keys. Returns (document, text) or None."""
    duplicates: list[str] = []

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict:
        seen: set[str] = set()
        for key, _ in pairs:
            if key in seen:
                duplicates.append(key)
            seen.add(key)
        return dict(pairs)

    try:
        text = path.read_text(encoding="utf-8")
        document = json.loads(text, object_pairs_hook=reject_duplicates)
    except (OSError, ValueError) as exc:
        problems.append(f"{path}: not valid JSON ({exc})")
        return None
    for key in dict.fromkeys(duplicates):
        problems.append(f"{path}: duplicate key {key!r} — parsers keep only the last copy")
    return document, text


def check(root: Path, unchanged_from: Path | None = None) -> list[str]:
    problems: list[str] = []

    spec_path = root / "openapi.json"
    loaded = _load(spec_path, problems)
    if loaded is not None:
        spec, text = loaded
        if text != json.dumps(spec, indent=2, ensure_ascii=False) + "\n":
            problems.append(
                f"{spec_path}: not in canonical form; it is generated from the backend and "
                "must not be edited or merged by hand"
            )
        if unchanged_from is not None and spec != json.loads(unchanged_from.read_text(encoding="utf-8")):
            problems.append(
                f"{spec_path}: content changed outside the automated release PR (branch "
                "docs/openapi-sync); schema changes reach this repo only through that PR"
            )

    overlay_path = root / "cekura-mcp-server" / "mcp_tools.json"
    loaded = _load(overlay_path, problems)
    if loaded is not None and isinstance(loaded[0], dict) and list(loaded[0]) != sorted(loaded[0]):
        problems.append(f"{overlay_path}: top-level keys must be sorted (insert new tools in order)")

    _load(root / "mint.json", problems)
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--unchanged-from", type=Path, help="openapi.json to compare parsed content against")
    args = parser.parse_args()
    problems = check(args.root, args.unchanged_from)
    for problem in problems:
        print(f"ERROR: {problem}", file=sys.stderr)
    if not problems:
        print("openapi.json, mcp_tools.json and mint.json are well-formed.")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
