import json
import sys


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


def main(paths):
    failed = False
    for path in paths:
        duplicates = find_duplicates(path)
        for key in duplicates:
            print(f"::error file={path}::duplicated JSON key {key!r}. Mintlify rejects the file and every API reference page renders empty.")
        if duplicates:
            failed = True
        else:
            print(f"{path}: no duplicated keys")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:] or ["openapi.json"]))
