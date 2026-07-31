# ElevenLabs MCP Server

Streamable HTTP wrapper for the official `elevenlabs-mcp` package. It authenticates callers with Cekura OAuth and calls ElevenLabs using each caller's own API key.

## Overview

- Official ElevenLabs MCP tools, pinned to `elevenlabs-mcp==0.11.0`
- Stateless HTTP endpoint at `/mcp`
- Local Cekura OAuth JWT verification on every MCP request
- Per-request ElevenLabs authentication through `xi-elevenlabs-api-key`
- Public health checks at `/mcp/health` and `/mcp/healthz`
- All tools exposed by the pinned official ElevenLabs MCP package
- No server-side ElevenLabs key

## Deployment

The production deployment is a separate one-task Fargate service in the existing
`cet-prd-usw2-cekura-ecs-cluster`:

- ECS family: `elevenlabs-mcp`
- ECS service: `cet-prd-usw2-elevenlabs-mcp-ecs-service`
- ECR repository: `cekura/elevenlabs-mcp`
- Container/target-group port: `8080`
- Desired count: `1`
- Task definition: `deployment/ecs/prod-usw2/elevenlabs-mcp-task-def.json`

The `Deploy ElevenLabs MCP Server (PROD)` workflow builds and deploys the
service whenever `elevenlabs-mcp/**` changes on `main`. Create the ECS service,
target group, listener rule, DNS record, secret, and IAM permission once before
the first workflow run.

1. Create the Secrets Manager secret named
   `cet-prd-usw2-elevenlabs-mcp-secret` in `us-west-2`. Store these values in
   its JSON value:

   ```text
   OAUTH_TOKEN_SECRET=<same value used by the Cekura backend>
   MCP_SERVER_URL=https://ELEVENLABS_MCP_HOST/mcp
   MCP_ISSUER_URL=<backend OAUTH_AUDIENCE>
   CEKURA_OAUTH_AUDIENCE=<backend OAUTH_AUDIENCE>
   ```

   Production uses `https://api.cekura.ai` as its OAuth audience. Stage must use its stage backend URL.

2. Grant the task role
   `cet-prd-usw2-elevenlabs-mcp-task-iam-role` `secretsmanager:GetSecretValue`
   on that secret. Use the existing prod ECS execution role and networking
   configuration used by `mcp-server`.
3. Create an HTTPS ALB target group for port `8080`, with `GET /mcp/health` as
   the health check. Allow inbound traffic to the task only from the ALB
   security group.
4. Add an ALB host rule for `elevenlabs-mcp.cekura.ai` forwarding these paths
   to the target group:
   `/mcp*` and `/.well-known/oauth-protected-resource`.
   This dedicated host avoids conflicting with the existing `api.cekura.ai/mcp`
   rule used by the Cekura MCP server. Add an alias record for
   `elevenlabs-mcp.cekura.ai` to the ALB.
5. Create the ECS service with the task definition above and desired count
   `1`; subsequent image updates are handled by the workflow.

The task stores no ElevenLabs API key.

## How to use

The client authenticates through Cekura OAuth and also sends its own ElevenLabs API key as `xi-elevenlabs-api-key`.

### Codex

```bash
codex mcp add elevenlabs \
  --url https://ELEVENLABS_MCP_HOST/mcp \
  --oauth-resource https://ELEVENLABS_MCP_HOST/mcp
```

Add the ElevenLabs key to `~/.codex/config.toml`:

```toml
[mcp_servers.elevenlabs.http_headers]
xi-elevenlabs-api-key = "YOUR_ELEVENLABS_API_KEY"
```

Connect the Cekura account, then start a new Codex session:

```bash
codex mcp login elevenlabs
```

### Claude Code

```bash
claude mcp add --transport http elevenlabs https://ELEVENLABS_MCP_HOST/mcp \
  --header "xi-elevenlabs-api-key: YOUR_ELEVENLABS_API_KEY"
```

On the first connection, Claude opens the Cekura OAuth login and consent page. After approval, ask it to use the ElevenLabs MCP tools.

## Security

The server exposes the complete official ElevenLabs MCP tool set. It verifies Cekura OAuth tokens locally with the configured `OAUTH_TOKEN_SECRET`; it does not call the Cekura backend on MCP requests.

OAuth access tokens remain valid until expiry, so disabling a user does not take effect here until the active token expires. Keep the OAuth access-token lifetime short.

Do not expose port `8080` directly to the internet. Use an ALB or reverse proxy that terminates TLS, applies rate limits, and forwards traffic to the task security group.

The dependency is pinned because the wrapper intentionally uses the official package's MCP registry and HTTP client hooks. Update the pin only after checking those integration points.
