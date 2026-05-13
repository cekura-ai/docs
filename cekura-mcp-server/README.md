# Cekura MCP Server

Model Context Protocol (MCP) server that provides unified access to Cekura's documentation and APIs for Claude and other MCP clients.

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)

## Overview

This MCP server provides **84+ tools** combining documentation search and API operations:
- **1 Documentation Search Tool** - Proxies to Mintlify's search (no API key required)
- **83 API Operations** - Dynamically generated from OpenAPI spec (requires API key)

By connecting to one server, AI assistants get complete access to both learning resources and operational capabilities.

### Key Features

- **Unified Access**: Single connection for both documentation search and API operations
- **Documentation Proxy**: Seamlessly proxies search requests to Mintlify's MCP server with retry logic
- **Opt-in API Exposure**: Registers only operations marked `x-mcp-expose: true` in the OpenAPI spec
- **64-Character Tool Name Limit**: Ensures Claude API compatibility with automatic truncation
- **Selective Authentication**: Documentation search requires no API key; API operations use `X-CEKURA-API-KEY` header
- **Dynamic Tool Generation**: API tools generated from OpenAPI spec at runtime
- **Zero Configuration**: Works out-of-the-box with sensible defaults
- **Production Ready**: Comprehensive error handling, retry logic, and test coverage

## Quick Start

### Prerequisites

- Python 3.8 or higher
- pip package manager
- Valid Cekura API key

### Installation

```bash
# Clone repository
git clone https://github.com/your-org/cekura-mcp-server.git
cd cekura-mcp-server

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Configuration

**No configuration required!** The server works out-of-the-box with sensible defaults.

**Default Configuration:**
```bash
CEKURA_BASE_URL=https://api.cekura.ai (production)
CEKURA_OPENAPI_SPEC=../openapi.json (from docs root)
```

**Optional:** Override defaults for staging/dev environments:
```bash
# Copy example configuration
cp .env.example .env

# Edit to override defaults
nano .env
```

### Running the Server

```bash
# Start server
python3 openapi_mcp_server.py

# Server starts on http://0.0.0.0:8000/mcp
# Output: "Registered 74 MCP tools"

# Health check (for load balancers/monitoring)
curl http://localhost:8000/mcp/health
# Response: {"status":"healthy","service":"cekura-mcp-server","tools_registered":74}
```

### Health Check Endpoints

The server provides health check endpoints for monitoring and load balancers:

- **`/mcp/health`** - Returns health status (not logged)
- **`/mcp/healthz`** - Kubernetes-style health check (not logged)

Both endpoints return:
```json
{
  "status": "healthy",
  "service": "cekura-mcp-server",
  "tools_registered": 74
}
```

**Note:** Health check requests are automatically filtered from logging to avoid log noise.

### Connecting Clients

**Claude Desktop / CLI:**
```bash
claude mcp add cekura-api http://localhost:8000/mcp \
  --transport http \
  --header "X-CEKURA-API-KEY:your_api_key_here"
```

Replace `your_api_key_here` with your actual Cekura API key.

## Project Structure

```
docs.git/
├── openapi.json              # OpenAPI 3.0 specification
└── cekura-mcp-server/
    ├── mcp_tools.json            # Per-tool LLM-facing overlays
    ├── openapi_mcp_server.py     # Main server application
    ├── config.py                 # Configuration management
    ├── tool_generator.py         # Tool generation and filtering
    ├── http_client.py            # HTTP client for API calls
    ├── openapi_parser.py         # OpenAPI specification parser
    ├── validate_overlays.py      # Overlay drift checks
    ├── requirements.txt          # Python dependencies
    ├── pytest.ini                # Test configuration
    ├── .env.example              # Configuration template
    └── tests/
        ├── test_config.py
        ├── test_overlays.py
        └── test_tool_generator.py
```

## Components

### Core Modules

**`openapi_mcp_server.py`**
- Main application entry point
- Initializes FastMCP server
- Manages API key sessions
- Registers dynamic tools

**`config.py`**
- Environment variable management
- Configuration validation
- Settings with sensible defaults

**`tool_generator.py`**
- Generates tool names and descriptions
- Filters operations by `x-mcp-expose` vendor extension
- Applies per-tool overlays from `mcp_tools.json`

**`openapi_parser.py`**
- Parses OpenAPI 3.0 specifications
- Extracts operations, schemas, and vendor extensions

**`http_client.py`**
- Async HTTP client for API calls
- Request/response handling
- Error management

### Data Files

**`../openapi.json`** (docs root)
- Complete OpenAPI 3.0 specification generated by the backend
- Operations exposed as MCP tools carry `x-mcp-expose: true`

**`mcp_tools.json`**
- Per-tool LLM-facing overlays keyed by operationId
- Adds `description_suffix`, `examples`, etc. on top of spec-derived metadata

## Development

### Running Tests

```bash
# Run all tests
pytest

# Verbose output
pytest -v
```

### Adding or Updating Tools

Tools are sourced directly from `openapi.json`. An operation is exposed as an MCP tool when it carries `x-mcp-expose: true`.

To customize the LLM-facing description, examples, or required fields for a tool, add an entry in `mcp_tools.json` keyed by operationId.

### Code Quality

```bash
# Linting
flake8 .

# Format checking
black --check .

# Import sorting
isort --check .

# Security scan
bandit -r .
```

## Tool Categories

The server provides 84 tools across two main groups:

### Documentation Search (1 tool, no API key required)

| Tool | Description |
|------|-------------|
| SearchCekura | Search across Cekura's knowledge base for integration guides, API schemas, code examples, and feature documentation. Proxies to Mintlify's MCP server with retry logic and error handling. |

### API Operations (83 tools, API key required)

| Category | Count | Description |
|----------|-------|-------------|
| Calls | 7 | Observability and call logging |
| Agents | 8 | AI agent management (CRUD, duplicate, knowledge base) |
| Metrics | 9 | Custom metrics and evaluation criteria |
| Evaluators | 11 | Scenario testing (voice, text, websocket, pipecat) |
| Test Profiles | 5 | Test configuration management |
| Results | 7 | Test execution results and analysis |
| Projects | 5 | Project management |
| Schedules | 5 | Cron job scheduling |
| Others | 3 | Personalities, predefined metrics, phone numbers |

**Total:** 84 tools (1 documentation + 83 API operations from 622 available endpoints)

## Architecture

```
┌─────────────────┐
│  MCP Client     │
│  (Claude)       │
└────────┬────────┘
         │ HTTP + X-CEKURA-API-KEY
         ↓
┌─────────────────────────────┐
│  Cekura MCP Server          │
│  (Port 8000)                │
│                             │
│  ┌─────────────────────┐   │
│  │ FastMCP Framework   │   │
│  ├─────────────────────┤   │
│  │ Dynamic Tools       │   │
│  │ (x-mcp-expose only) │   │
│  ├─────────────────────┤   │
│  │ Session Management  │   │
│  │ (API Keys)          │   │
│  └─────────────────────┘   │
└────────┬────────────────────┘
         │ API Requests (with user's key)
         ↓
┌─────────────────────────────┐
│  Cekura API                 │
│  api.cekura.ai              │
└─────────────────────────────┘
```

## Configuration Options

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `CEKURA_BASE_URL` | No | `https://api.cekura.ai` | Cekura API base URL (override for staging/dev) |
| `CEKURA_OPENAPI_SPEC` | No | `../openapi.json` | Path to OpenAPI spec (from docs root) |
| `CEKURA_MAX_TOOLS` | No | - | Max tools to register (for testing) |

**Note:** All configuration is optional. The set of registered tools is determined by the `x-mcp-expose: true` marker on each operation in `openapi.json`.

## Authentication

API keys are **required** via the `X-CEKURA-API-KEY` HTTP header. The server:

- ✅ Accepts API keys from headers only
- ✅ Stores keys per session
- ✅ Has no default or fallback keys
- ✅ Returns clear error if key is missing

Each client connecting to the MCP server must provide their own API key.

## Troubleshooting

### Server won't start

```bash
# Check Python version
python3 --version  # Should be 3.8+

# Reinstall dependencies
pip install -r requirements.txt

# Verify configuration
cat .env
```

### Wrong number of tools registered

```bash
# Inspect markers in the current spec
python3 -c "import json; spec=json.load(open('../openapi.json')); print(sum(1 for p,m in spec['paths'].items() for _,o in m.items() if isinstance(o,dict) and o.get('x-mcp-expose')))"
```

If a tool is missing, confirm the operation in `openapi.json` carries `x-mcp-expose: true`.

### API key errors

- Ensure header `X-CEKURA-API-KEY` is provided when connecting
- No default/fallback key exists - header is mandatory
- Verify key is valid at https://api.cekura.ai

### Port already in use

```bash
# Find process using port 8000
lsof -i :8000

# Kill process
kill -9 <PID>

# Or specify different port
python3 openapi_mcp_server.py --port 8001
```

## Requirements

**Runtime:**
- Python 3.8+
- fastmcp >= 0.1.0
- httpx >= 0.24.0
- pydantic >= 2.0.0
- python-dotenv >= 1.0.0

**Development:**
- pytest >= 7.4.0
- pytest-asyncio >= 0.21.0
- pytest-cov >= 4.1.0
- pytest-mock >= 3.11.1

## Performance

- **Tool Registration:** 74 tools in ~500ms
- **Tool Name Generation:** 100 operations in < 0.5 seconds
- **Memory Usage:** ~50MB baseline
- **Startup Time:** ~2 seconds

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Make your changes
4. Run tests (`pytest`)
5. Commit changes (`git commit -m 'Add amazing feature'`)
6. Push to branch (`git push origin feature/amazing-feature`)
7. Open a Pull Request

**Exposing a new endpoint as an MCP tool:**
- Ensure the operation in `openapi.json` carries `x-mcp-expose: true`.
- (Optional) Add a `mcp_tools.json` entry keyed by its operationId for LLM-facing description overlays.

## License

[MIT License](LICENSE)

## Support

- **Documentation:** [https://docs.cekura.ai](https://docs.cekura.ai)
- **Issues:** [GitHub Issues](https://github.com/your-org/cekura-mcp-server/issues)
- **API Status:** [https://status.cekura.ai](https://status.cekura.ai)

## Acknowledgments

Built with [FastMCP](https://github.com/jlowin/fastmcp) - A fast, lightweight MCP server framework.
