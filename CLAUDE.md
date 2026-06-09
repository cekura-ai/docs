# Claude Code Configuration — `docs` repo

This is the public Mintlify documentation repo for Cekura. It hosts the user-facing docs site, the OpenAPI spec, and the `cekura-mcp-server` package.

## Repo layout

- `documentation/`, `api-reference/`, `mcp/`, `cli-sdk/` — Mintlify `.mdx` source for the docs site.
- `mint.json` — Mintlify navigation and site config.
- `openapi.json` — OpenAPI 3.0 spec. **Generated upstream — do not hand-edit.**
- `cekura-mcp-server/` — Python MCP server that turns `openapi.json` into Model-Context-Protocol tools.
- `scripts/` — local automation (description sync, llms.txt generator, etc.).

## MCP server (`cekura-mcp-server/`)

The MCP server registers an operation from `openapi.json` as a tool when the operation carries:

```yaml
x-mcp-expose: true
```

That is the only inclusion signal. The spec is the source of truth; the server does not read any other whitelist.

### `mcp_tools.json` — per-tool overlays

`cekura-mcp-server/mcp_tools.json` lets you augment a tool's LLM-facing metadata without changing the spec. The file is keyed by the tool's name (the post-processed `operationId`). Common fields per entry:

| Field | Purpose |
|---|---|
| `description_suffix` | Appended to the spec-derived description. Use for MCP-transport caveats or cross-tool guidance. |
| `required` | Additional fields the LLM should always supply (validated against the tool's input schema). |
| `examples` / `example_request` | Override or extend openapi examples shown to the model. |
| `max_examples`, `example_names` | Filter/cap the openapi examples surfaced. |
| `destructive` | Mark the tool as destructive when the description doesn't already include "⚠ Irreversible…". |

Run `python3 cekura-mcp-server/validate_overlays.py` to catch drift: orphan entries, stale field names, missing destructive markers.

### Adding a new tool overlay

1. Confirm the operation in `openapi.json` carries `x-mcp-expose: true`.
2. Identify its tool name (the kebab-case `operationId` converted to snake_case — the value used by `validate_overlays`).
3. Add an entry to `cekura-mcp-server/mcp_tools.json` keyed by that tool name.
4. `python3 cekura-mcp-server/validate_overlays.py` — must report no drift.

### Tests

```bash
cd cekura-mcp-server
python3 -m pytest tests/
```

`tests/test_overlays.py` runs the drift checks as a CI gate.

## Mintlify (docs preview)

```bash
npm i -g mintlify
mintlify dev      # run from this directory
```

## Rule — keep this repo public-safe

This repo is public. Do not add references to internal implementation details, upstream service names, internal file paths, decorator names, or migration history in any file here (`.mdx`, `.py`, `.md`, workflow YAML). Describe behavior in terms of what is visible in `openapi.json` and the files in this repo.

