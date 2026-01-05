# Cekura MCP Server

Model Context Protocol (MCP) server that exposes Cekura's documented APIs as tools for Claude and other MCP clients.

[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)

## Overview

This MCP server dynamically generates tools from Cekura's OpenAPI specification, filtered to only documented endpoints. It reduces the tool count from 622 to 74 operations (88% reduction), significantly improving context efficiency while maintaining full functionality.

### Key Features

- **Whitelist-Based Filtering**: Automatically registers only documented APIs from `mint.json` (docs root)
- **Header-Based Authentication**: Secure API key management via `X-CEKURA-API-KEY` header
- **Dynamic Tool Generation**: Tools generated from OpenAPI spec at runtime
- **Zero Configuration**: Works out-of-the-box with sensible defaults
- **Production Ready**: Comprehensive logging, error handling, and test coverage

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
├── openapi.json              # OpenAPI 3.0 specification (622 operations)
├── mint.json                 # Documentation source
└── cekura-mcp-server/
    ├── documented_apis.json      # Generated whitelist (75 endpoints)
    ├── openapi_mcp_server.py     # Main server application
    ├── config.py                 # Configuration management
    ├── tool_generator.py         # Tool generation and filtering
    ├── http_client.py            # HTTP client for API calls
    ├── openapi_parser.py         # OpenAPI specification parser
    ├── extract_documented_apis.py # Whitelist generator
    ├── test_whitelist.py         # Whitelist validator
    ├── sync_apis.sh              # Whitelist sync script
    ├── requirements.txt          # Python dependencies
    ├── pytest.ini                # Test configuration
    ├── .env.example              # Configuration template
    └── tests/
        ├── test_config.py
        ├── test_extract_apis.py
        ├── test_tool_generator.py
        └── test_mcp_server.py
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
- Whitelist-based filtering
- Operation inclusion logic

**`openapi_parser.py`**
- Parses OpenAPI 3.0 specifications
- Extracts operations and schemas
- Builds parameter schemas

**`http_client.py`**
- Async HTTP client for API calls
- Request/response handling
- Error management

**`extract_documented_apis.py`**
- Extracts documented endpoints from ../mint.json
- Generates `documented_apis.json` whitelist
- Maps MDX files to API operations

### Data Files

**`../openapi.json`** (2MB, docs root)
- Complete OpenAPI 3.0 specification
- 622 total operations
- Used as source for tool generation

**`../mint.json`** (12KB, docs root)
- Mintlify documentation configuration
- Lists all documented API references
- Source of truth for whitelist

**`documented_apis.json`** (12KB, generated)
- Generated whitelist of 75 documented endpoints
- Maps method + path combinations
- Automatically regenerated via `sync_apis.sh`

## Development

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov

# Run specific test file
pytest tests/test_mcp_server.py

# Verbose output
pytest -v
```

**Test Coverage:** 61 tests (93.4% pass rate, ~85% code coverage)

### Updating API Whitelist

When documentation changes in `../mint.json` (docs root):

```bash
# Regenerate whitelist
./sync_apis.sh

# Or manually
python3 extract_documented_apis.py

# Restart server to apply changes
```

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

## API Categories

The server exposes 74 documented API endpoints across these categories:

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

**Total:** 74 curated tools from 622 available operations

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
│  │ 74 Dynamic Tools    │   │
│  │ (Whitelist Filtered)│   │
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
| `CEKURA_FILTER_TAGS` | No | - | Filter by tags (comma-separated) |
| `CEKURA_EXCLUDE_OPERATIONS` | No | - | Exclude operations (comma-separated) |
| `CEKURA_MAX_TOOLS` | No | - | Max tools to register (for testing) |

**Note:** All configuration is optional. The whitelist in `documented_apis.json` automatically limits tools to 75 documented endpoints.

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
# Should show: "Registered 74 MCP tools"
python3 test_whitelist.py

# If different, regenerate whitelist
./sync_apis.sh
```

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
- **Whitelist Filtering:** 1000 operations in < 1 second
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

**Updating Documentation:**
- Modify `../mint.json` (docs root) with new API references
- Run `./sync_apis.sh` to regenerate whitelist
- Commit `documented_apis.json`

## License

[MIT License](LICENSE)

## Support

- **Documentation:** [https://docs.cekura.ai](https://docs.cekura.ai)
- **Issues:** [GitHub Issues](https://github.com/your-org/cekura-mcp-server/issues)
- **API Status:** [https://status.cekura.ai](https://status.cekura.ai)

## Acknowledgments

Built with [FastMCP](https://github.com/jlowin/fastmcp) - A fast, lightweight MCP server framework.
