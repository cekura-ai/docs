# Vapi MCP Server

Streamable HTTP wrapper for Vapi's official MCP. It authenticates callers with Cekura OAuth, uses each caller's Vapi API key, and returns the complete assistant configuration from REST for `get_assistant`.

## Overview

- All official Vapi MCP tools are available
- `get_assistant` uses Vapi REST so `model.messages`, including the system prompt, is returned
- Local Cekura OAuth JWT verification on every MCP request
- Per-request Vapi authentication through `x-vapi-api-key`
- Public health checks at `/mcp/health` and `/mcp/healthz`
- No server-side Vapi API key

## Deployment

Deploy this as a separate one-task Fargate service alongside the existing Cekura MCP service:

- ECS family and service: `vapi-mcp`
- ECR repository: `cekura/vapi-mcp`
- Container/target-group port: `8080`
- Health check: `GET /mcp/health`
- Public hostname: `vapi.cekura.ai`

Add these namespaced values to the existing MCP secret:

```text
VAPI_MCP_OAUTH_TOKEN_SECRET=<same value used by the Cekura backend>
VAPI_MCP_SERVER_URL=https://vapi.cekura.ai/mcp
VAPI_MCP_ISSUER_URL=https://api.cekura.ai
VAPI_MCP_CEKURA_OAUTH_AUDIENCE=https://api.cekura.ai
```

Create an HTTPS ALB host rule for `vapi.cekura.ai`, forwarding to port `8080`, and an alias record for that hostname. Reuse the existing Cekura MCP task role, execution role, security groups, subnets, and secret. The deployment workflow builds and deploys on changes to `vapi-mcp/**`.

## How to use

The client authenticates through Cekura OAuth and sends its own Vapi API key in `x-vapi-api-key`.

### Codex

```bash
codex mcp add vapi \
  --url https://vapi.cekura.ai/mcp \
  --oauth-resource https://vapi.cekura.ai/mcp
```

Add the Vapi key to `~/.codex/config.toml`:

```toml
[mcp_servers.vapi.http_headers]
x-vapi-api-key = "YOUR_VAPI_API_KEY"
```

Then authenticate and start a new Codex session:

```bash
codex mcp login vapi
```

### Claude Code

```bash
claude mcp add --transport http vapi https://vapi.cekura.ai/mcp \
  --header "x-vapi-api-key: YOUR_VAPI_API_KEY"
```

On the first connection, Claude opens the Cekura OAuth login and consent page.

## Security

The service verifies Cekura OAuth locally with `VAPI_MCP_OAUTH_TOKEN_SECRET`; it does not call the Cekura backend on MCP requests. It sends the caller's Vapi key only to Vapi and never forwards the Cekura OAuth token upstream.

Do not expose port `8080` directly. Terminate TLS and apply rate limits at the ALB or reverse proxy.
