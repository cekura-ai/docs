# ElevenLabs MCP Server

Streamable HTTP wrapper for the official `elevenlabs-mcp` package. It is safe for multi-user hosting: every MCP request must include the caller's own ElevenLabs API key and the server stores no ElevenLabs key.

## Overview

- Official ElevenLabs MCP tools, pinned to `elevenlabs-mcp==0.11.0`
- Stateless HTTP endpoint at `/mcp`
- Per-request authentication through `xi-api-key`
- Public health checks at `/mcp/health` and `/mcp/healthz`
- All tools exposed by the pinned official ElevenLabs MCP package
- No server-side ElevenLabs key

## Quick Start

```bash
cd elevenlabs-mcp
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
./venv/bin/python server.py
```

The server listens on `http://0.0.0.0:8080/mcp`.

## Health Check

```bash
curl http://localhost:8080/mcp/health
```

Expected response:

```json
{"status":"healthy","service":"elevenlabs-mcp","tools_registered":27}
```

## Connect a Client

Each client supplies its own ElevenLabs key. Do not configure a shared server-side key.

```bash
claude mcp add --transport http elevenlabs http://SERVER_HOST:8080/mcp \
  --header "xi-api-key: YOUR_ELEVENLABS_API_KEY"
```

For Codex, configure the HTTP MCP server with the same URL and `xi-api-key` header.

## Docker

```bash
cd elevenlabs-mcp
docker build -t elevenlabs-mcp .
docker run --detach --name elevenlabs-mcp --restart unless-stopped \
  --publish 127.0.0.1:8080:8080 elevenlabs-mcp
```

Verify the container:

```bash
curl http://localhost:8080/mcp/health
```

## ECS Hosting

1. Build this folder's Dockerfile and push the image to ECR.
2. Run one Fargate task behind an internal or internet-facing ALB.
3. Forward the ALB target group to container port `8080`.
4. Configure the target-group health check as `GET /mcp/health` with success code `200`.
5. Terminate TLS at the ALB and expose only the resulting HTTPS URL to MCP clients.
6. Allow inbound traffic to the ECS task security group only from the ALB security group.

The task needs no ElevenLabs secret. Clients send `xi-api-key` on each request.

## Security

The server exposes the complete official ElevenLabs MCP tool set. Place authentication, authorization, and usage controls in the backend or ingress layer that sits in front of this service.

Do not expose port `8080` directly to the internet. Use an ALB or reverse proxy that terminates TLS, applies rate limits, and forwards traffic to the task security group.

The dependency is pinned because the wrapper intentionally uses the official package's MCP registry and HTTP client hooks. Update the pin only after checking those integration points.
