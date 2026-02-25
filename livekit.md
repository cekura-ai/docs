# Cekura Python SDK

**Testing and Observability for AI Voice Agents. Launch in minutes not weeks by ensuring your agents deliver a seamless experience in every conversational scenario.**

## Installation

```bash
pip install cekura
```

## LiveKit Integration

### Setup

1. Create a new agent in the Cekura dashboard
2. Select **LiveKit** as the provider in agent settings
3. Enable tracing for your agent
4. Copy your API key and Agent ID

### Quick Start

```python
import os
from cekura.livekit import LiveKitTracer

# Initialize Cekura tracer
cekura = LiveKitTracer(
    api_key=os.getenv("CEKURA_API_KEY"),
    agent_id=123
)

@server.rtc_session(agent_name="my_agent")
async def entrypoint(ctx: agents.JobContext):
    # Create your LiveKit session
    session = agents.AgentSession(...)

    # Start Cekura session tracking
    session_id = cekura.start_session(session)

    # Add shutdown callback to export Cekura data
    async def cekura_shutdown():
        await cekura.export(session_id, ctx)

    ctx.add_shutdown_callback(cekura_shutdown)

    # Start your session
    await session.start(room=ctx.room, agent=YourAssistant())
```

### Next Steps

After integrating the SDK, you can:
- Run tests from the Cekura platform
- Monitor real-time conversations
- Analyze performance metrics
- Review conversation transcripts and tool calls

## Features

- **Automatic Metrics Collection**: Captures STT, LLM, TTS, and End-of-Utterance metrics
- **Conversation Tracking**: Records complete chat history with tool calls
- **Session Reports**: Generates comprehensive session reports from LiveKit
- **Easy Integration**: Simple API with minimal setup required

## Configuration

### Parameters

- `api_key` (str, required): Your Cekura API key
- `agent_id` (int, required): Unique identifier for your agent from Cekura dashboard
- `host` (str, optional): API host URL (default: `https://api.cekura.ai`)
- `enabled` (bool, optional): Enable/disable tracing. Defaults to `CEKURA_TRACING_ENABLED` env var ("true" if not set)

## Data Collected

The SDK automatically collects:

- Session start/end timestamps
- Complete conversation transcripts
- Tool/function calls and outputs
- Performance metrics:
  - Speech-to-Text (STT) latency and duration
  - LLM token usage and timing
  - Text-to-Speech (TTS) generation time
  - End-of-Utterance detection timing
- Room and job information
- Custom metadata

## Requirements

- Python 3.9+
- aiohttp>=3.9.0

## License

MIT License - see [LICENSE](LICENSE) file for details.

## Links

- [Homepage](https://cekura.ai)
- [Documentation](https://docs.cekura.ai)
- [Dashboard](https://dashboard.cekura.ai)

## Support

For support, email support@cekura.ai
