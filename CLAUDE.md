# CLAUDE.md

This file provides guidance for Claude Code when working on the OpenKaguya project.

## Project Overview

OpenKaguya is an open-source, personified AI companion (辉夜姬) that runs on personal computers. The v2 design philosophy: **给 AI 一部手机，它就能做一切** — give AI a phone, and it can do everything.

Core concepts:
- AI controls a real Android phone via ADB as its universal interface to the world
- Autonomous "consciousness" with heartbeat, notification watching, and timers
- Recursive summarization memory (L0→L1→L2→L3) — no vector DB needed
- Multi-modal LLM (Qwen VL Max) for direct screenshot understanding

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
pytest tests/test_memory.py

# Lint
ruff check src/
```

## Architecture

### Message Processing Pipeline

```
User Message (CLI / Admin Web UI)
  → ChatEngine.handle_message()
    → Build working memory (L0 + new message)
    → Build system prompt (persona + memory context + time)
    → LLM chat with tools (max 15 iterations)
      → Execute tool calls (phone, notes, timers)
      → Feed results back to LLM
    → Save to memory (user message + AI response)
  → Reply
```

### Key Modules

| Path | Purpose |
|------|---------|
| `src/kaguya/core/engine.py` | Central chat loop with tool calling (max 15 iterations) |
| `src/kaguya/core/consciousness.py` | Background loops: heartbeat, notifications, timers |
| `src/kaguya/core/memory.py` | Recursive summarization memory (L0/L1/L2/L3) + notes + timers |
| `src/kaguya/core/types.py` | Core data types: `UnifiedMessage`, `ToolCall`, `ChatResponse`, `Platform` |
| `src/kaguya/phone/controller.py` | ADB wrapper: tap, swipe, type, screenshot, app control |
| `src/kaguya/phone/screen.py` | UI dump parsing + Set-of-Mark screenshot annotation |
| `src/kaguya/phone/tools.py` | Phone tool definitions exposed to LLM |
| `src/kaguya/tools/notes.py` | Notes tools: write, read, delete |
| `src/kaguya/tools/common.py` | Common tools: set_timer, list_timers |
| `src/kaguya/llm/client.py` | OpenAI-compatible LLM wrapper with token tracking |
| `src/kaguya/adapters/base.py` | `PlatformAdapter` abstract base class |
| `src/kaguya/adapters/cli.py` | Terminal interaction adapter |
| `src/kaguya/admin/api.py` | aiohttp web UI + REST API |
| `src/kaguya/config.py` | TOML configuration loader (layered: default → secrets → persona → user_mixin) |

### Phone Module

The phone module replaces all v1 platform adapters. Instead of reverse-engineering each app's API, the AI controls a real Android phone via ADB:

- **PhoneController** (`phone/controller.py`): ADB commands — screenshot, UI dump, tap, swipe, type text (Chinese via ADBKeyboard), open app, get notifications, push/pull files
- **ScreenReader** (`phone/screen.py`): Parses uiautomator XML → extracts interactive elements → annotates screenshot with Set-of-Mark (red boxes + numbered labels)
- **Phone Tools** (`phone/tools.py`): `phone_screenshot`, `phone_tap`, `phone_long_press`, `phone_type`, `phone_swipe`, `phone_back`, `phone_home`, `phone_open_app`, `phone_notifications`

### Memory System

Four-layer recursive summarization in SQLite (no vector DB):

| Layer | Storage | Size | Description |
|-------|---------|------|-------------|
| L0 | In-memory | ~50 messages | Working memory (raw messages) |
| L1 | SQLite `short_term_memory` | Max 100 records | Summaries of L0 batches (200-500 chars each) |
| L2 | SQLite `long_term_memory` | Max 50 records | Compressed L1 records (500-1000 chars each) |
| L3 | SQLite `core_memory` | 1 record | Ultimate memory (1000-2000 chars) |

Additional tables: `notes` (user-writable facts), `timers` (scheduled reminders), `consciousness_log` (action history).

Compression is triggered automatically when a layer exceeds its max size.

### Tool System

All tools are always visible to the LLM (no more toolkit router):
- **Phone tools** (~9): Screen capture, tap, swipe, type, navigation, app control
- **Notes tools** (~3): Write, read, delete persistent notes
- **Common tools** (~2): Set timer, list timers

Tool execution uses executor chaining — iterate executors until one handles the call.

### Consciousness System

Three independent background loops:

1. **Heartbeat** — Wakes every 30±10 min (skips quiet hours 23:00-07:00), reviews recent actions and pending timers, decides what to do
2. **Notifications** — Polls phone notifications every 30s, filters by watch_apps/ignore_apps/regex, wakes engine for new notifications
3. **Timers** — Checks triggered timers every 60s, wakes engine when a timer fires

## Configuration

| File | Purpose |
|------|---------|
| `config/default.toml` | LLM endpoints, phone, memory thresholds, consciousness, notifications, admin |
| `config/secrets.toml` | API keys — **never commit this file** |
| `config/persona.toml` | Kaguya's personality, speech style, interests |
| `data/user_mixin.toml` | Runtime user config written by admin UI (git-ignored) |

Key config sections: `[llm.primary]`, `[llm.summarizer]`, `[phone]`, `[memory]`, `[consciousness]`, `[notifications]`, `[admin]`, `[persona]`.

## Admin Web UI

aiohttp server on `localhost:8080` with:
- Chat interface (`/`) — send messages with optional image upload
- Settings page (`/settings`) — configure notifications, view memory
- REST API: `/api/chat`, `/api/stats`, `/api/memory/{l1,l2,core}`, `/api/notes`, `/api/timers`, `/api/logs`, `/api/working`, `/api/phone/apps`, `/api/notifications/config`
- Optional Bearer token auth

## Code Conventions

- **Python 3.12+**, async-first (`asyncio`, `aiohttp`, `asyncio.to_thread` for blocking ops)
- **Pydantic v2** for data validation and config models
- **Loguru** for logging (`logger.info/debug/warning/error`)
- Line length: **100 characters** (Ruff)
- Ruff rules: E, F, I, W
- Pytest with `asyncio_mode = "auto"` — all tests can be `async def`
- Type hints throughout; dataclasses for core types, Pydantic for config

## Important Notes

- The LLM backend uses an **OpenAI-compatible API** (not Anthropic). Default: Qwen VL Max (primary) + Qwen Turbo (summarizer) via Aliyun DashScope.
- `data/` directory is git-ignored (contains SQLite DB, logs).
- `config/secrets.toml` is git-ignored. Use `config/secrets.example.toml` as a template.
- Phone control requires a connected Android device with USB debugging enabled and ADB accessible.
- Chinese text input requires ADBKeyboard installed on the phone.
- The consciousness system runs as background asyncio tasks, calling `ChatEngine` directly with synthetic prompts.
- Screenshot annotation uses Set-of-Mark: red bounding boxes with numbered labels overlaid on scaled screenshots for element identification.
