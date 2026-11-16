import argparse
import json
import sys

MARKER = "<!-- openapi-duplicate-keys -->"


def find_duplicates(path):
    duplicates = []

    def hook(pairs):
        seen = set()
        for key, _ in pairs:
            if key in seen:
                duplicates.append(key)
            seen.add(key)
        return dict(pairs)

    with open(path, encoding="utf-8") as f:
        json.load(f, object_pairs_hook=hook)
    return duplicates


def render_report(found):
    if not found:
        return "\n".join([
            MARKER,
            "✅ **Fixed.** `openapi.json` and `mint.json` are valid with no duplicated keys, so Mintlify can load the API reference again.",
        ]) + "\n"
    lines = [
        MARKER,
        "⚠️ **Not safe to merge: this PR breaks the API reference.**",
        "",
        "Mintlify rejects a spec with a duplicated key or invalid JSON, drops it, and every API reference page on docs.cekura.ai renders empty.",
        "",
        "| File | Problem |",
        "| --- | --- |",
    ]
    for path, key in found:
        lines.append(f"| `{path}` | {key if key.startswith('invalid JSON') else f'duplicated key `{key}`'} |")
    lines += [
        "",
        "Fix the file and push again. A duplicated key usually comes from a git merge where both sides added the same block in different places.",
    ]
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("paths", nargs="*", default=["openapi.json"])
    parser.add_argument("--report")
    args = parser.parse_args()

    found = []
    for path in args.paths:
        try:
            duplicates = find_duplicates(path)
        except ValueError as e:
            found.append((path, f"invalid JSON: {e}"))
            print(f"::error file={path}::invalid JSON: {e}")
            continue
        for key in duplicates:
            found.append((path, key))
            print(f"::error file={path}::duplicated JSON key {key!r}. Mintlify rejects the file and every API reference page renders empty.")
        if not duplicates:
            print(f"{path}: no duplicated keys")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(render_report(found))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
