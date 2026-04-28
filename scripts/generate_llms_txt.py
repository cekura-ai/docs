#!/usr/bin/env python3
"""
Generate a well-structured llms.txt from mint.json navigation and MDX frontmatter.

Reads the Mintlify navigation structure and page descriptions to produce
a semantically organized llms.txt that overrides Mintlify's auto-generated version.

Usage:
    python scripts/generate_llms_txt.py                    # Dry run (prints to stdout)
    python scripts/generate_llms_txt.py --write            # Write to llms.txt
    python scripts/generate_llms_txt.py --write --verbose  # Write with details
"""

import argparse
import json
import os
import re
import sys

DOCS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MINT_JSON_PATH = os.path.join(DOCS_ROOT, "mint.json")
OUTPUT_PATH = os.path.join(DOCS_ROOT, "llms.txt")
BASE_URL = "https://docs.cekura.ai"

INTRO = (
    "Cekura is the testing and observability platform for voice AI agents. "
    "Run simulated conversations, evaluate performance with LLM-judge and "
    "code metrics, and monitor production calls."
)

SECTION_MAP = {
    "Get Started": "Getting Started",
    "Key Concepts": "Key Concepts",
    "Integrations": "Integrations",
    "Guides": None,
    "API Reference": "API Reference",
    "Advanced": "Advanced",
    "MCP": "MCP Server",
}

GUIDES_SUBSECTION_MAP = {
    "Testing": "Guides",
    "Observability": "Observability",
    "Agent Integration": "Agent Integration",
}

GUIDES_PAGE_OVERRIDES = {
    "documentation/red-teaming/multi-turn": "Guides",
}

API_GROUP_DESCRIPTIONS = {
    "Calls": "Send, list, and evaluate production calls",
    "Agents": "Create and manage agent configurations for testing",
    "Mock Tools": "Define simulated tool responses for evaluator scenarios",
    "Metrics": "Create and manage LLM-judge and code-based evaluation metrics",
    "Labs": "Metric improvement pipeline with feedback and auto-review",
    "Evaluators": "Create test scenarios with personas, conditional actions, and expected outcomes",
    "Test Profiles": "Reusable test configurations for evaluator runs",
    "Results": "View and manage evaluation run results",
    "Runs": "Inspect individual run details and bulk operations",
    "Schedules": "Schedule recurring evaluation runs",
    "Projects": "Organize agents, evaluators, and metrics into projects",
    "Organization": "Billing and organization management",
    "Dashboards": "Build custom dashboards for agent performance",
    "Widgets": "Configure dashboard widgets and data visualizations",
    "API Keys": "Create and manage API keys for authentication",
    "Others": "Personalities, predefined metrics, phone numbers, and utilities",
}


def load_mint_json() -> dict:
    with open(MINT_JSON_PATH) as f:
        return json.load(f)


def extract_frontmatter(content: str) -> dict:
    match = re.match(r"^---\s*\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return {}
    fm = {}
    for line in match.group(1).split("\n"):
        if ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key in ("title", "description", "sidebarTitle"):
                fm[key] = value
    return fm


def read_page_meta(page_path: str) -> dict:
    for ext in (".mdx", ".md", ""):
        full_path = os.path.join(DOCS_ROOT, page_path + ext)
        if os.path.exists(full_path):
            with open(full_path) as f:
                return extract_frontmatter(f.read())
    return {}


def page_url(page_path: str) -> str:
    return f"{BASE_URL}/{page_path}.md"


def format_entry(page_path: str, meta: dict) -> str:
    title = meta.get("sidebarTitle") or meta.get("title") or page_path.split("/")[-1]
    description = meta.get("description", "")
    if description:
        description = description.split("\n")[0][:200]
    url = page_url(page_path)
    if description:
        return f"- [{title}]({url}): {description}"
    return f"- [{title}]({url})"


def collect_pages(pages_list: list) -> list[tuple[str, dict]]:
    results = []
    for item in pages_list:
        if isinstance(item, str):
            meta = read_page_meta(item)
            results.append((item, meta))
        elif isinstance(item, dict) and "pages" in item:
            results.extend(collect_pages(item["pages"]))
    return results


def collect_pages_by_subgroup(pages_list: list) -> dict[str | None, list[tuple[str, dict]]]:
    groups: dict[str | None, list[tuple[str, dict]]] = {}
    for item in pages_list:
        if isinstance(item, str):
            groups.setdefault(None, []).append((item, read_page_meta(item)))
        elif isinstance(item, dict) and "pages" in item:
            group_name = item.get("group")
            sub_pages = collect_pages(item["pages"])
            groups.setdefault(group_name, []).extend(sub_pages)
    return groups


def generate_api_section(nav_group: dict) -> list[str]:
    lines = ["## API Reference"]
    for item in nav_group["pages"]:
        if isinstance(item, dict) and "group" in item:
            group_name = item["group"]
            all_pages = collect_pages(item["pages"])
            if not all_pages:
                continue
            first_path, first_meta = all_pages[0]
            desc = API_GROUP_DESCRIPTIONS.get(group_name, "")
            url = page_url(first_path)
            if desc:
                lines.append(f"- [{group_name}]({url}): {desc}")
            else:
                lines.append(f"- [{group_name}]({url})")
    lines.append("")
    return lines


def generate(verbose: bool = False) -> str:
    mint = load_mint_json()
    navigation = mint.get("navigation", [])

    sections: list[tuple[str, list[str]]] = []
    section_entries: dict[str, list[str]] = {}

    for nav_group in navigation:
        group_name = nav_group.get("group", "")
        pages = nav_group.get("pages", [])

        if group_name == "API Reference":
            api_lines = generate_api_section(nav_group)
            sections.append(("API Reference", api_lines))
            continue

        if group_name == "Guides":
            subgroups = collect_pages_by_subgroup(pages)
            for sub_name, sub_pages in subgroups.items():
                if sub_name and sub_name in GUIDES_SUBSECTION_MAP:
                    default_section = GUIDES_SUBSECTION_MAP[sub_name]
                else:
                    default_section = "Automation"
                for path, meta in sub_pages:
                    section_name = GUIDES_PAGE_OVERRIDES.get(path, default_section)
                    entries = section_entries.setdefault(section_name, [])
                    entries.append(format_entry(path, meta))
                    if verbose:
                        print(f"  {section_name}: {path}")
            continue

        section_name = SECTION_MAP.get(group_name, group_name)
        if not section_name:
            continue

        all_pages = collect_pages(pages)
        entries = section_entries.setdefault(section_name, [])
        for path, meta in all_pages:
            entries.append(format_entry(path, meta))
            if verbose:
                print(f"  {section_name}: {path}")

    section_order = [
        "Getting Started",
        "Key Concepts",
        "Integrations",
        "Guides",
        "Observability",
        "Automation",
        "Agent Integration",
        "MCP Server",
        "Advanced",
    ]

    output_lines = [
        f"# Cekura",
        "",
        f"> {INTRO}",
        "",
    ]

    for section_name in section_order:
        entries = section_entries.get(section_name, [])
        if not entries:
            continue
        output_lines.append(f"## {section_name}")
        output_lines.extend(entries)
        output_lines.append("")

    for section_name, lines in sections:
        output_lines.extend(lines)

    output_lines.append("## Optional")
    output_lines.append(
        f"- [OpenAPI Spec]({BASE_URL}/openapi.json): "
        "Full OpenAPI 3.0 specification for all API endpoints"
    )
    output_lines.append("")

    return "\n".join(output_lines)


def main():
    parser = argparse.ArgumentParser(
        description="Generate structured llms.txt from mint.json and MDX frontmatter"
    )
    parser.add_argument("--write", action="store_true", help="Write to llms.txt (default is stdout)")
    parser.add_argument("--verbose", action="store_true", help="Print details for each page")
    args = parser.parse_args()

    if not os.path.exists(MINT_JSON_PATH):
        print(f"ERROR: mint.json not found at {MINT_JSON_PATH}")
        sys.exit(1)

    content = generate(verbose=args.verbose)

    if args.write:
        with open(OUTPUT_PATH, "w") as f:
            f.write(content)
        line_count = content.count("\n")
        entry_count = content.count("- [")
        print(f"Written {OUTPUT_PATH} ({line_count} lines, {entry_count} entries)")
    else:
        print(content)


if __name__ == "__main__":
    main()
