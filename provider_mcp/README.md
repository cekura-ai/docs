# Provider MCP Server

One Streamable HTTP service for provider MCPs. It authenticates clients with Cekura OAuth and uses each caller's provider API key.

## Routes

- `/elevenlabs/mcp` exposes the official ElevenLabs MCP tools.
- `/vapi/mcp` exposes the official Vapi MCP tools. Its native `get_assistant` tool is backed by Vapi REST so the response includes `model.messages` and the system prompt.

There is no generic `/mcp` endpoint.

## Deployment

Deploy one Fargate service and route its public hostname to this container on port `8080`.

Production deployment values:

- ECS family: `provider-mcp`
- ECS service: `cet-prd-usw2-provider-mcp-ecs-service`
- ECR repository: `cekura/provider-mcp`
- Public hostname: `provider-mcp.cekura.ai`
- Task definition: `deployment/ecs/prod-usw2/provider-mcp-task-def.json`
- Desired count: `1`

The `Deploy Provider MCP Server (PROD)` workflow builds and deploys the
service when `provider_mcp/**` changes on `main`. It creates the ECS service if
missing, copying network configuration from the existing Cekura MCP service,
or updates the existing service.

Before the first workflow run, create `cekura/provider-mcp` in the shared
services ECR account and apply the same cross-account AWS Organization pull
policy used by `cekura/mcp-server`. The production ECS execution role must be
able to obtain the ECR authorization token and pull this repository.

Add these values to the existing MCP secret:

```text
PROVIDER_MCP_OAUTH_TOKEN_SECRET=<same value used by the Cekura backend>
PROVIDER_MCP_BASE_URL=https://provider-mcp.cekura.ai
PROVIDER_MCP_ISSUER_URL=https://api.cekura.ai
PROVIDER_MCP_CEKURA_OAUTH_AUDIENCE=https://api.cekura.ai
```

The task reuses the existing MCP task role and execution role. No new secret
or IAM role is required. Keep the existing `elevenlabs-mcp` ECS service and
`elevenlabs.cekura.ai` endpoint during migration so they remain available as a
rollback path.

Configure the existing public HTTPS ALB with a host-only rule for
`provider-mcp.cekura.ai` forwarding to an IP target group on port `8080`. Use
`/elevenlabs/mcp/health` as the target-group health check. Create a Route 53
alias for `provider-mcp.cekura.ai` to the ALB. The service has public health
checks at `/elevenlabs/mcp/health` and `/vapi/mcp/health`. Do not expose port
`8080` directly.

## How to use

Each provider is configured as its own MCP connection and shares the Cekura OAuth login flow.

### Codex

```bash
codex mcp add elevenlabs \
  --url https://provider-mcp.cekura.ai/elevenlabs/mcp \
  --oauth-resource https://provider-mcp.cekura.ai/elevenlabs/mcp

codex mcp add vapi \
  --url https://provider-mcp.cekura.ai/vapi/mcp \
  --oauth-resource https://provider-mcp.cekura.ai/vapi/mcp
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
claude mcp add --transport http elevenlabs https://provider-mcp.cekura.ai/elevenlabs/mcp \
  --header "xi-elevenlabs-api-key: YOUR_ELEVENLABS_API_KEY"

claude mcp add --transport http vapi https://provider-mcp.cekura.ai/vapi/mcp \
  --header "x-vapi-api-key: YOUR_VAPI_API_KEY"
```

On the first connection, Claude opens the Cekura OAuth login and consent page.
