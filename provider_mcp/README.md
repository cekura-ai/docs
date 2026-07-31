# Provider MCP Server

One Streamable HTTP service for provider MCPs. It authenticates clients with Cekura OAuth and uses each caller's provider API key.

## Routes

- `/elevenlabs/mcp` exposes the official ElevenLabs MCP tools.
- `/vapi/mcp` exposes the official Vapi MCP tools. Its native `get_assistant` tool is backed by Vapi REST so the response includes `model.messages` and the system prompt.

## Deployment

Deploy one Fargate service and route its public hostname to this container on port `8080`.

Add these values to the existing MCP secret:

```text
PROVIDER_MCP_OAUTH_TOKEN_SECRET=<same value used by the Cekura backend>
PROVIDER_MCP_BASE_URL=https://elevenlabs.cekura.ai
PROVIDER_MCP_ISSUER_URL=https://api.cekura.ai
PROVIDER_MCP_CEKURA_OAUTH_AUDIENCE=https://api.cekura.ai
```

Configure the existing ALB and `elevenlabs.cekura.ai` DNS record to forward the shared service. The service has public health checks at `/elevenlabs/mcp/health` and `/vapi/mcp/health`. The existing `/mcp` endpoint remains an ElevenLabs compatibility alias. Do not expose port `8080` directly.

## How to use

Each provider is configured as its own MCP connection and shares the Cekura OAuth login flow.

### Codex

```bash
codex mcp add elevenlabs \
  --url https://elevenlabs.cekura.ai/elevenlabs/mcp \
  --oauth-resource https://elevenlabs.cekura.ai/elevenlabs/mcp

codex mcp add vapi \
  --url https://elevenlabs.cekura.ai/vapi/mcp \
  --oauth-resource https://elevenlabs.cekura.ai/vapi/mcp
```

Add provider keys to `~/.codex/config.toml`:

```toml
[mcp_servers.elevenlabs.http_headers]
xi-elevenlabs-api-key = "YOUR_ELEVENLABS_API_KEY"

[mcp_servers.vapi.http_headers]
x-vapi-api-key = "YOUR_VAPI_API_KEY"
```

Authenticate each connection, then start a new Codex session:

```bash
codex mcp login elevenlabs
codex mcp login vapi
```

### Claude Code

```bash
claude mcp add --transport http elevenlabs https://elevenlabs.cekura.ai/elevenlabs/mcp \
  --header "xi-elevenlabs-api-key: YOUR_ELEVENLABS_API_KEY"

claude mcp add --transport http vapi https://elevenlabs.cekura.ai/vapi/mcp \
  --header "x-vapi-api-key: YOUR_VAPI_API_KEY"
```

On the first connection, Claude opens the Cekura OAuth login and consent page.
