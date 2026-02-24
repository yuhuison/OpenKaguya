# CLAUDE.md

This file provides guidance for Claude Code when working on the OpenKaguya project.

## Project Overview

OpenKaguya is an open-source, personified AI companion (辉夜姬) that runs on personal computers. It is not a tool but a character-driven chatbot with:
- Autonomous "consciousness" that wakes up periodically and browses the internet
- Long-term memory via SQLite + vector embeddings (sqlite-vec)
- Multi-platform support (WeChat, CLI, extensible)
- Dynamic tool loading via a toolkit router to minimize token overhead

**Entry point**: `src/kaguya/main.py` → `uv run kaguya`

## Development Commands

```bash
# Install dependencies
uv sync

# Run the app
uv run kaguya

# Run all tests
pytest tests/

# Run a specific test
pytest tests/test_chat.py

# Lint
ruff check src/
```

## Architecture

### Message Processing Pipeline

```
User Message
  → GroupFilterMiddleware   (filter out unaddressed group messages)
  → MemoryMiddleware        (inject history, trigger vectorization)
  → ChatEngine              (LLM call + tool loop, max 15 iterations)
  → Reply
```

### Key Modules

| Path | Purpose |
|------|---------|
| `src/kaguya/core/engine.py` | Central chat processing loop, middleware system, tool calling |
| `src/kaguya/core/consciousness.py` | Autonomous wake-up scheduler with jitter and quiet hours |
| `src/kaguya/core/types.py` | Core data types: `UnifiedMessage`, `UserInfo`, `Platform` |
| `src/kaguya/llm/client.py` | OpenAI-compatible LLM wrapper with token tracking |
| `src/kaguya/llm/embedding.py` | Vector embedding client for semantic memory search |
| `src/kaguya/memory/database.py` | SQLite + sqlite-vec: messages, topics, notes, timers |
| `src/kaguya/memory/topic_manager.py` | Auto-archives messages into summarized, vectorized topics |
| `src/kaguya/memory/retriever.py` | Hybrid keyword + semantic memory search |
| `src/kaguya/tools/registry.py` | Tool registration and management |
| `src/kaguya/tools/builtin.py` | Core tools: notes, messages, timers, sub-agent |
| `src/kaguya/tools/toolkit_router.py` | Dynamic activation of optional tool groups |
| `src/kaguya/adapters/cli.py` | Terminal interaction adapter |
| `src/kaguya/adapters/wechat.py` | WeChat WebSocket + HTTP adapter |
| `src/kaguya/admin/api.py` | aiohttp REST API for web dashboard |
| `src/kaguya/config.py` | TOML configuration loader |

### Extension Points

The project has two extension tracks:

**Adapters** (in `adapters/`): Connect chat platforms. Implement `PlatformAdapter`. Must provide `get_tools(phase)`, `get_system_prompt()`, and `get_injected_prompt()`.

**Providers** (in `providers/`): Add AI capabilities (e.g., image generation). Implement `BaseProvider`. Register in `config/default.toml` under `[providers]`.

### Toolkit Router

To keep token usage low, tools are split:
- **Core tools** (~14): Always visible to the LLM (send message, notes, search, web, sub-agent, timers, `use_toolkit`)
- **Optional toolkits** (~30+ tools): Hidden by default, activated on demand via `use_toolkit`:
  - `workspace` — file read/write/delete/list, terminal
  - `browser` — browser_task, click, input, scroll, etc.
  - `image` — generate_image, edit_image, view_image, set_avatar
  - `sns` — WeChat Moments posting and interaction

### Memory System

Three-layer hierarchy:
1. **In-context window** — last N raw messages (configurable)
2. **Topics** — auto-archived, summarized, and vector-indexed conversation segments
3. **Notes** — explicitly stored facts the AI decides to remember

## Configuration

| File | Purpose |
|------|---------|
| `config/default.toml` | LLM endpoints, memory thresholds, consciousness timing, browser mode, WeChat config |
| `config/secrets.toml` | API keys — **never commit this file** |
| `config/persona.toml` | Kaguya's personality, speech style, preferences, emoji frequency |

Key config sections: `[llm.primary]`, `[llm.secondary]`, `[llm.embedding]`, `[memory]`, `[consciousness]`, `[browser]`, `[wechat]`, `[admin]`, `[[identity.users]]`.

## Code Conventions

- **Python 3.12+**, async-first (`asyncio`, `aiohttp`, `asyncio.to_thread` for blocking ops)
- **Pydantic v2** for data validation
- **Loguru** for logging (`logger.info/debug/warning/error`)
- Line length: **100 characters** (Ruff)
- Ruff rules: E, F, I, W
- Pytest with `asyncio_mode = "auto"` — all tests can be `async def`
- Type hints throughout; Pydantic models for all config and message types

## Important Notes

- The LLM backend uses an **OpenAI-compatible API** (not the Anthropic API). The default provider is Qwen via Aliyun Dashscope, configured via `config/secrets.toml`.
- `data/` directory is git-ignored (contains SQLite DB, logs, workspaces).
- `config/secrets.toml` is git-ignored. Use `config/secrets.example.toml` as a template.
- The `consciousness` system runs as a background asyncio task. It calls `ChatEngine` directly with a synthetic `UnifiedMessage` when it wakes up.
- WeChat integration uses a locally running WeChat hook that exposes a WebSocket for receiving messages and an HTTP API for sending.
- Sub-agent delegation (`run_sub_agent` tool) spawns a separate `ChatEngine` call with a focused system prompt, enabling complex multi-step tasks.
