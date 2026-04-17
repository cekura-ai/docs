#!/usr/bin/env python3
"""
Sync descriptions from openapi.json into API reference MDX page frontmatter.

Reads the OpenAPI spec and updates all api-reference/**/*.mdx files to include
a `description` field in their YAML frontmatter, extracted from the matching
operation's description or summary.

This ensures Mintlify's auto-generated llms.txt has meaningful descriptions
for all API reference pages.

Usage:
    python scripts/sync_descriptions.py                    # Dry run
    python scripts/sync_descriptions.py --write            # Apply changes
    python scripts/sync_descriptions.py --write --verbose  # Apply with details
"""

import argparse
import json
import os
import re
import sys

DOCS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OPENAPI_PATH = os.path.join(DOCS_ROOT, "openapi.json")
API_REF_DIR = os.path.join(DOCS_ROOT, "api-reference")

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}


def load_openapi_spec(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def build_operation_map(spec: dict) -> dict[str, dict]:
    """Build a map of 'method /path' -> operation dict."""
    op_map = {}
    for path, path_item in spec.get("paths", {}).items():
        for method in HTTP_METHODS:
            if method in path_item:
                key = f"{method} {path}"
                op_map[key] = path_item[method]
    return op_map


def extract_frontmatter(content: str) -> tuple[str, str, int, int]:
    """Extract YAML frontmatter from MDX file content.

    Returns (frontmatter_text, body, fm_start, fm_end).
    """
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return "", content, 0, 0
    fm_text = match.group(1)
    fm_end = match.end()
    body = content[fm_end:]
    return fm_text, body, match.start(), fm_end


def get_openapi_key_from_frontmatter(fm_text: str) -> str | None:
    """Extract the 'openapi: method /path' value from frontmatter."""
    for line in fm_text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("openapi:"):
            value = stripped.partition(":")[2].strip()
            # Remove any quotes
            value = value.strip('"').strip("'")
            return value
    return None


def get_existing_description(fm_text: str) -> str | None:
    """Extract existing description from frontmatter if present."""
    for line in fm_text.split("\n"):
        stripped = line.strip()
        if stripped.startswith("description:"):
            value = stripped.partition(":")[2].strip()
            # Remove surrounding quotes
            if (value.startswith('"') and value.endswith('"')) or \
               (value.startswith("'") and value.endswith("'")):
                value = value[1:-1]
            return value
    return None


def generate_description_from_title(title: str) -> str:
    """Generate a basic description from the page title when no spec description exists."""
    return title


def escape_yaml_string(s: str) -> str:
    """Escape a string for safe YAML double-quoted value."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def add_description_to_frontmatter(fm_text: str, description: str) -> str:
    """Add or update description field in YAML frontmatter."""
    lines = fm_text.split("\n")
    new_lines = []
    description_added = False
    escaped = escape_yaml_string(description)

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("description:"):
            # Replace existing description
            new_lines.append(f'description: "{escaped}"')
            description_added = True
        else:
            new_lines.append(line)

    if not description_added:
        # Add description after title line
        final_lines = []
        for line in new_lines:
            final_lines.append(line)
            if line.strip().startswith("title:"):
                final_lines.append(f'description: "{escaped}"')
                description_added = True

        if not description_added:
            # Fallback: add at the end
            final_lines.append(f'description: "{escaped}"')

        new_lines = final_lines

    return "\n".join(new_lines)


def process_mdx_files(
    op_map: dict[str, dict],
    write: bool = False,
    verbose: bool = False,
) -> dict:
    """Process all API reference MDX files and sync descriptions.

    Returns stats dict with counts.
    """
    stats = {
        "total": 0,
        "updated": 0,
        "already_has_description": 0,
        "no_openapi_key": 0,
        "no_spec_match": 0,
        "no_spec_description": 0,
    }

    for dirpath, _dirnames, filenames in os.walk(API_REF_DIR):
        for filename in sorted(filenames):
            if not filename.endswith(".mdx"):
                continue

            filepath = os.path.join(dirpath, filename)
            rel_path = os.path.relpath(filepath, DOCS_ROOT)
            stats["total"] += 1

            with open(filepath) as f:
                content = f.read()

            fm_text, body, _fm_start, _fm_end = extract_frontmatter(content)
            if not fm_text:
                if verbose:
                    print(f"  SKIP {rel_path}: no frontmatter")
                continue

            openapi_key = get_openapi_key_from_frontmatter(fm_text)
            if not openapi_key:
                stats["no_openapi_key"] += 1
                if verbose:
                    print(f"  SKIP {rel_path}: no openapi key in frontmatter")
                continue

            # Look up operation in spec
            operation = op_map.get(openapi_key)

            # Get description from spec operation, or fall back to title
            spec_desc = ""
            if operation:
                spec_desc = operation.get("description") or operation.get("summary") or ""
            else:
                stats["no_spec_match"] += 1
                if verbose:
                    print(f"  WARN {rel_path}: no spec match for '{openapi_key}'")

            if spec_desc:
                # Use first meaningful line
                description = spec_desc.split("\n")[0].strip()
            else:
                if operation:
                    stats["no_spec_description"] += 1
                # Fall back to title-based description
                title_line = ""
                for line in fm_text.split("\n"):
                    if line.strip().startswith("title:"):
                        title_line = line.partition(":")[2].strip()
                        break
                if title_line:
                    description = generate_description_from_title(title_line)
                else:
                    continue

            # Check if already has the same description
            existing = get_existing_description(fm_text)
            if existing and existing == description:
                stats["already_has_description"] += 1
                continue

            # Update frontmatter
            new_fm = add_description_to_frontmatter(fm_text, description)
            new_content = f"---\n{new_fm}\n---{body}"

            if write:
                with open(filepath, "w") as f:
                    f.write(new_content)
                stats["updated"] += 1
                if verbose:
                    print(f"  UPDATED {rel_path}: {description[:60]}...")
            else:
                stats["updated"] += 1
                if verbose:
                    print(f"  WOULD UPDATE {rel_path}: {description[:60]}...")

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Sync OpenAPI descriptions to API reference MDX frontmatter"
    )
    parser.add_argument(
        "--write", action="store_true",
        help="Actually write changes (default is dry run)"
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print details for each file"
    )
    parser.add_argument(
        "--openapi", default=OPENAPI_PATH,
        help=f"Path to openapi.json (default: {OPENAPI_PATH})"
    )
    args = parser.parse_args()

    if not os.path.exists(args.openapi):
        print(f"ERROR: OpenAPI spec not found at {args.openapi}")
        sys.exit(1)

    if not os.path.isdir(API_REF_DIR):
        print(f"ERROR: API reference directory not found at {API_REF_DIR}")
        sys.exit(1)

    print(f"Loading OpenAPI spec from {args.openapi}...")
    spec = load_openapi_spec(args.openapi)
    op_map = build_operation_map(spec)
    print(f"Found {len(op_map)} operations in spec")

    mode = "WRITE" if args.write else "DRY RUN"
    print(f"\nProcessing API reference MDX files ({mode})...\n")

    stats = process_mdx_files(op_map, write=args.write, verbose=args.verbose)

    print(f"\n{'=' * 50}")
    print(f"Results ({mode}):")
    print(f"  Total MDX files:          {stats['total']}")
    print(f"  Updated/would update:     {stats['updated']}")
    print(f"  Already has description:  {stats['already_has_description']}")
    print(f"  No openapi key:           {stats['no_openapi_key']}")
    print(f"  No spec match:            {stats['no_spec_match']}")
    print(f"  No spec description:      {stats['no_spec_description']}")
    print(f"{'=' * 50}")

    if not args.write and stats["updated"] > 0:
        print(f"\nRun with --write to apply {stats['updated']} changes.")


if __name__ == "__main__":
    main()
