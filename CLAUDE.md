# CLAUDE.md

This file provides guidance for Claude Code when working on the OpenKaguya project.

## Project Overview

OpenKaguya is an open-source, personified AI companion (辉夜姬) that runs on Windows. The v2 design philosophy: **给 AI 一台电脑，它就能做一切** — give AI a computer, and it can do everything.

Core concepts:
- AI controls the Windows desktop via Win32 API (ctypes) as its universal interface
- Autonomous "consciousness" with heartbeat, notification watching, and timers
- Recursive summarization memory (L0→L1→L2→L3) in SQLite — no vector DB
- Multi-modal LLM for direct screenshot understanding
- Gateway-based tool routing — heavy tools (desktop, browser) activated on demand

**Entry point**: `src/kaguya/main.py` → `uv run kaguya`

## Development Commands

```bash
# Install dependencies
uv sync

# Install with browser support
uv sync --extra browser

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
    → Build system prompt (persona + memory context + time + optional avatar)
    → LLM chat with tools (max 100 iterations)
      → ToolRouter dispatches tool calls (gateway activation + executor chaining)
      → Feed results back to LLM
    → Save to memory (user message + AI response)
  → Reply
```

Streaming variant: `handle_message_stream()` yields real-time events (tool calls, partial text).

### Key Modules

| Path | Purpose |
|------|---------|
| `src/kaguya/core/engine.py` | Central chat loop with tool calling (max 100 iterations) |
| `src/kaguya/core/router.py` | ToolRouter: gateway-based tool group activation + dispatch |
| `src/kaguya/core/consciousness.py` | Background loops: heartbeat, notifications, timers |
| `src/kaguya/core/memory.py` | Recursive summarization memory (L0/L1/L2/L3) + notes + timers |
| `src/kaguya/core/types.py` | Core data types: `UnifiedMessage`, `ToolCall`, `ChatResponse`, `Platform` |
| `src/kaguya/desktop/controller.py` | Win32 ctypes wrapper: mouse, keyboard, screenshot, window management, clipboard |
| `src/kaguya/desktop/screen.py` | Numbered grid overlay annotation for screenshot element identification |
| `src/kaguya/desktop/tools.py` | Desktop tool definitions (~13 tools) exposed to LLM |
| `src/kaguya/tools/notes.py` | Notes tools: write, read, delete |
| `src/kaguya/tools/common.py` | Common tools: set_timer, list_timers, generate_image, set_avatar |
| `src/kaguya/tools/workspace.py` | File I/O + terminal execution in sandboxed workspace |
| `src/kaguya/tools/image.py` | Image generation (DashScope Z-Image) + editing (Qwen) + viewing |
| `src/kaguya/tools/avatar.py` | Avatar management for multi-modal system prompt |
| `src/kaguya/tools/browser.py` | Browser automation via browser-use (local/CDP/cloud) |
| `src/kaguya/tools/sub_agent.py` | Sub-agent delegation for complex tasks |
| `src/kaguya/llm/client.py` | OpenAI-compatible LLM wrapper with token tracking |
| `src/kaguya/adapters/base.py` | `PlatformAdapter` abstract base class |
| `src/kaguya/adapters/cli.py` | Terminal interaction adapter |
| `src/kaguya/admin/api.py` | aiohttp web UI + REST API (with SSE streaming) |
| `src/kaguya/config.py` | TOML configuration loader (layered: default → secrets → persona → user_mixin) |

### Desktop Module

The desktop module replaces the old phone module. Instead of ADB on Android, the AI controls a Windows desktop via Win32 API:

- **DesktopController** (`desktop/controller.py`): Pure ctypes + PIL — screenshot, click, drag, scroll, type text (native Unicode, supports Chinese), hotkeys, window management, clipboard read/write, notification detection via window title changes
- **DesktopScreenReader** (`desktop/screen.py`): Generates numbered circle grid overlay on screenshots (row-major, left-to-right top-to-bottom). Supports full-screen and per-window screenshots with coordinate translation. Configurable scale and grid spacing.
- **Desktop Tools** (`desktop/tools.py`): `desktop_screenshot`, `desktop_click`, `desktop_click_coord`, `desktop_double_click`, `desktop_right_click`, `desktop_type`, `desktop_hotkey`, `desktop_scroll`, `desktop_drag`, `desktop_list_windows`, `desktop_focus_window`, `desktop_clipboard_read`, `desktop_clipboard_write`

### Tool Router (Gateway Pattern)

Tools are organized into groups with on-demand activation:

**Always-visible tools** (base groups):
- `notes` — Write, read, delete persistent notes
- `common` — Set timer, list timers
- `workspace` — File read/write/delete/list + terminal command execution (sandboxed to `data/workspaces/kaguya/`)
- `avatar` — Set avatar image for multi-modal system prompt
- `image` — Generate/edit/view images (DashScope)
- `sub_agent` — Delegate complex tasks to a sub-agent (with tool blacklist to prevent recursion)

**Gated tools** (activated via gateway call):
- `desktop` — Activated by `use_desktop` gateway → unlocks all desktop control tools
- `browser` — Activated by `use_browser` gateway → unlocks browser automation tools

**Flow**: AI calls gateway tool (e.g. `use_desktop`) → router activates tool group → tools become visible in subsequent LLM turns → reset on next `handle_message()`.

### Memory System

Four-layer recursive summarization in SQLite (no vector DB):

| Layer | Storage | Size | Description |
|-------|---------|------|-------------|
| L0 | In-memory + SQLite `working_memory` | ~50 messages | Working memory (raw messages, persisted as backup) |
| L1 | SQLite `short_term_memory` | Max 100 records | Summaries of L0 batches (200-500 chars each) |
| L2 | SQLite `long_term_memory` | Max 50 records | Compressed L1 records (500-1000 chars each) |
| L3 | SQLite `core_memory` | 1 record | Ultimate memory (1000-2000 chars) |

Additional tables: `notes` (AI-writable facts), `timers` (scheduled reminders), `consciousness_log` (action history).

Compression is triggered automatically when a layer exceeds its max size. Memory context is injected into system prompt via `build_context()`.

### Consciousness System

Three independent background loops in `ConsciousnessScheduler`:

1. **Heartbeat** — Wakes every 30±10 min (skips quiet hours 23:00-07:00), reviews recent actions and pending timers, calls `engine.handle_consciousness()` to let AI decide autonomously
2. **Notifications** — Polls desktop notifications (window title changes) every 3s, applies watch_apps whitelist + content filter rules, 2s debounce before waking engine
3. **Timers** — Checks triggered timers every 60s, supports periodic timers (daily/weekly auto-reschedule), wakes engine when a timer fires

## Configuration

| File | Purpose |
|------|---------|
| `config/default.toml` | LLM endpoints, desktop, memory thresholds, consciousness, notifications, browser, image, admin |
| `config/secrets.toml` | API keys — **never commit this file** |
| `config/persona.toml` | Kaguya's personality, speech style, interests, per-scenario guidelines |
| `data/user_mixin.toml` | Runtime user config written by admin UI (git-ignored) |

Key config sections: `[llm.primary]`, `[llm.summarizer]`, `[desktop]`, `[memory]`, `[consciousness]`, `[notifications]`, `[browser]`, `[image]`, `[admin]`, `[persona]`.

## Admin Web UI

aiohttp server on `localhost:8080` with:
- Chat interface (`/`) — send messages with optional image upload
- Settings page (`/settings`) — configure notifications, view memory
- REST API:
  - `POST /api/chat` — send message (JSON response)
  - `POST /api/chat/stream` — send message (SSE streaming)
  - `GET /api/stats` — token usage stats
  - `GET /api/memory/{l1,l2,core}` — layered memory queries
  - `GET /api/notes`, `/api/timers`, `/api/logs`, `/api/working` — data access
  - `GET/POST /api/notifications/config` — notification settings
  - `GET /api/debug/sessions` — interaction logs
  - `POST /api/debug/clear` — clear debug logs
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

- The LLM backend uses an **OpenAI-compatible API** (not Anthropic). Default: Qwen 3.5 Plus (primary) + Qwen 3.5 Flash (summarizer) via Aliyun DashScope.
- `data/` directory is git-ignored (contains SQLite DB `kaguya.db`, logs, workspaces).
- `config/secrets.toml` is git-ignored. Use `config/secrets.example.toml` as a template.
- Desktop control uses pure Win32 API via ctypes — no external dependencies for mouse/keyboard/screenshot. Native Unicode support means Chinese input works without special tools.
- The consciousness system runs as background asyncio tasks, calling `ChatEngine.handle_consciousness()` with synthetic prompts.
- Screenshot annotation uses numbered circle grid overlay (not the old Set-of-Mark bounding boxes).
- Browser tools are optional — install with `uv sync --extra browser` (requires `browser-use` package).
- Workspace tools are sandboxed to `data/workspaces/kaguya/` with path traversal protection.
- Sub-agent tool has a blacklist to prevent recursive delegation and protect critical tools.
