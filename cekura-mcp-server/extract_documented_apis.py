import json
import re
from pathlib import Path
from typing import List, Dict, Optional


def extract_api_paths_from_mint(mint_json_path: str) -> List[str]:
    with open(mint_json_path, 'r', encoding='utf-8') as f:
        mint_data = json.load(f)

    api_files = []

    for section in mint_data.get('navigation', []):
        if section.get('group') == 'API Reference':
            api_files.extend(_extract_pages_recursive(section.get('pages', [])))
            break

    return api_files


def _extract_pages_recursive(pages_list: list) -> List[str]:
    result = []

    for item in pages_list:
        if isinstance(item, str):
            if item.startswith('api-reference/'):
                result.append(item)
        elif isinstance(item, dict):
            if 'pages' in item:
                result.extend(_extract_pages_recursive(item['pages']))

    return result


def extract_openapi_from_mdx(mdx_path: Path) -> Optional[Dict[str, str]]:
    try:
        with open(mdx_path, 'r', encoding='utf-8') as f:
            content = f.read()

        frontmatter_match = re.search(r'^---\s*\n(.*?)\n---', content, re.DOTALL | re.MULTILINE)
        if not frontmatter_match:
            return None

        frontmatter = frontmatter_match.group(1)

        openapi_match = re.search(r'openapi:\s*["\']?(\w+)\s+([^"\'\n]+)["\']?', frontmatter, re.IGNORECASE)

        if openapi_match:
            method = openapi_match.group(1).strip().upper()
            path = openapi_match.group(2).strip()

            if not path.startswith('/'):
                path = '/' + path

            return {
                'method': method,
                'path': path,
                'file': str(mdx_path.relative_to(mdx_path.parent.parent.parent))
            }

    except Exception as e:
        print(f"  ⚠ Error reading {mdx_path.name}: {e}")

    return None


def normalize_path(path: str) -> str:
    path = path.rstrip('/')
    if not path.startswith('/'):
        path = '/' + path
    return path


def main():
    print("=" * 70)
    print("Extracting Documented APIs from mint.json")
    print("=" * 70)

    mcp_server_dir = Path(__file__).parent
    docs_root = mcp_server_dir.parent

    mint_json_path = docs_root / 'mint.json'

    if not mint_json_path.exists():
        print(f"❌ Error: mint.json not found at {mint_json_path}")
        print(f"\n   Expected location: docs_root/mint.json")
        return

    print(f"📖 Reading mint.json from: {mint_json_path}")

    print(f"\n📖 Reading mint.json...")
    api_files = extract_api_paths_from_mint(str(mint_json_path))
    print(f"✓ Found {len(api_files)} API reference files in documentation")

    print(f"\n🔍 Extracting OpenAPI endpoints from MDX files...")
    endpoints = []
    missing_files = []
    invalid_files = []

    for api_file in api_files:
        mdx_path = docs_root / f"{api_file}.mdx"

        if not mdx_path.exists():
            missing_files.append(api_file)
            continue

        openapi_data = extract_openapi_from_mdx(mdx_path)

        if openapi_data:
            openapi_data['path'] = normalize_path(openapi_data['path'])
            endpoints.append(openapi_data)
        else:
            invalid_files.append(api_file)

    print(f"\n{'=' * 70}")
    print(f"📊 Summary:")
    print(f"{'=' * 70}")
    print(f"  Total documented APIs:     {len(api_files)}")
    print(f"  Successfully extracted:    {len(endpoints)}")
    print(f"  Missing MDX files:         {len(missing_files)}")
    print(f"  Invalid/No openapi field:  {len(invalid_files)}")

    if missing_files:
        print(f"\n⚠ Missing MDX files:")
        for f in missing_files:
            print(f"  - {f}.mdx")

    if invalid_files:
        print(f"\n⚠ Files without valid openapi field:")
        for f in invalid_files:
            print(f"  - {f}.mdx")

    print(f"\n{'=' * 70}")
    print(f"✅ Extracted {len(endpoints)} documented endpoints:")
    print(f"{'=' * 70}")

    grouped = {}
    for ep in endpoints:
        method = ep['method']
        if method not in grouped:
            grouped[method] = []
        grouped[method].append(ep)

    for method in sorted(grouped.keys()):
        print(f"\n{method}:")
        for ep in sorted(grouped[method], key=lambda x: x['path']):
            print(f"  {ep['path']}")

    output_file = Path(__file__).parent / 'documented_apis.json'
    output_data = {
        'total': len(endpoints),
        'endpoints': endpoints
    }

    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(output_data, f, indent=2)

    print(f"\n{'=' * 70}")
    print(f"💾 Saved to: {output_file}")
    print(f"{'=' * 70}")


if __name__ == '__main__':
    main()
