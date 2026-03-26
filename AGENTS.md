# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Project Overview

GhostLLM is a local AI gateway that provides OpenAI and Anthropic API-compatible endpoints, bridging to NVIDIA NIM for LLM inference. It consists of a Python FastAPI server, a Next.js web UI, and a CLI tool for management.

## Repository Structure

```
GhostLLM/
├── apps/
│   ├── server/          # FastAPI server (main entry: main.py)
│   │   ├── api/         # API endpoints (translation.py)
│   │   ├── auth/        # Authentication (security.py, middleware.py)
│   │   ├── middleware/  # Rate limiting
│   │   ├── providers/   # NVIDIA NIM provider
│   │   ├── database.py  # SQLModel/SQLite models
│   │   └── main.py      # FastAPI app entry point
│   ├── cli/             # CLI tool (main.py)
│   └── web/             # Next.js frontend
│       └── src/app/     # Next.js app directory
├── packages/
│   └── py-core/         # Shared Python package
│       └── ghostllm_core/
│           └── config.py  # Config loading (YAML)
├── configs/
│   ├── default.yaml     # Server config (upstream, monitoring)
│   └── models.yaml      # Model registry mappings
└── pyproject.toml       # Python workspace config (uv)
```

## Development Commands

### Python (Server/CLI)

```bash
# Install dependencies (uses uv)
uv sync

# Run the server directly
python apps/server/main.py
# Or with uv
uv run python apps/server/main.py

# Run the CLI
python apps/cli/main.py
# Or use the batch wrapper on Windows
ghost.bat <command>

# Run tests (if pytest configured)
uv run pytest
```

### CLI Commands

```bash
# Initialize database and configs
python apps/cli/main.py init

# Start/stop/restart the daemon
python apps/cli/main.py start
python apps/cli/main.py stop
python apps/cli/main.py restart

# Check status
python apps/cli/main.py status

# Launch Codex via GhostLLM bridge
python apps/cli/main.py Codex
python apps/cli/main.py Codex --model kimi

# Run health checks
python apps/cli/main.py doctor

# List enabled models
python apps/cli/main.py models list
```

### Next.js Web UI

```bash
cd apps/web

# Install dependencies
npm install

# Development server
npm run dev

# Build for production
npm run build

# Start production server
npm start

# Lint
npm run lint
```

## Architecture

### API Translation Layer

The server acts as a bridge between client APIs (OpenAI/Anthropic) and NVIDIA NIM:

- **Anthropic → OpenAI**: `apps/server/api/translation.py:translate_anthropic_to_openai()`
  - Maps model aliases (sonnet → kimi)
  - Strips XML tool instructions for non-Codex models
  - Applies fine-grained max_tokens policy (4096/8192 based on context)
  - Kimi-specific tweaks (temperature, thinking mode)

- **OpenAI → Anthropic**: `translate_openai_to_anthropic()` / `stream_openai_to_anthropic()`
  - Converts OpenAI responses to Anthropic message format
  - Handles reasoning blocks, tool calls, streaming SSE

### Model Registry

Models are configured in `configs/models.yaml`:

```yaml
models:
  - name: "fast"           # Local alias
    upstream_id: "meta/llama-3.1-8b-instruct"
    enabled: true
    expensive: false       # Admin-only if true
```

Supported aliases: `sonnet`, `Codex-3-5-sonnet`, `Codex-sonnet-4-6` → maps to `kimi`

### Authentication

- Default admin created on startup (username: `admin`, password: `ghost-admin-123`)
- API keys stored in SQLite (`ghost.db`)
- JWT tokens for session auth
- CLI extracts API key from database for Codex integration

### Database

SQLModel with SQLite (`ghost.db`):
- `User`: id, username, hashed_password, api_key, role, quota_limit, quota_used
- `UsageRecord`: Tracks token usage and estimated costs

### Server Endpoints

- `POST /v1/chat/completions` - OpenAI compatible
- `POST /v1/messages` - Anthropic compatible
- `POST /auth/login` - Authentication
- `POST /admin/users` - User management (admin only)
- `GET /health` - Health check
- `/ui/*` - Web UI (served from `apps/web/out`)

## Configuration

### default.yaml

```yaml
server:
  host: 127.0.0.1
  port: 8000

upstream:
  base_url: "https://integrate.api.nvidia.com/v1"
  nvidia_api_key: "..."

monitoring:
  log_format: json
  log_level: INFO
  prometheus_port: 9090
```

## Key Implementation Details

- **Process Management**: CLI uses Windows `tasklist`/`taskkill` for daemon management
- **Logging**: JSON formatted logs to `ghost.log`
- **Streaming**: SSE-based streaming with TTFT (time to first token) telemetry
- **Rate Limiting**: Middleware in `apps/server/middleware/rate_limit.py`
- **Expensive Models**: Models marked `expensive: true` require admin role

## Testing

Test scripts in repo root:
- `test_claude.py` - Tests Anthropic API compatibility
- `test_codex.py` - Tests OpenAI API compatibility
- `test_kimi.py` - Tests Kimi-specific features
