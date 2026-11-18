# `context-bridge-mcp` Design

Purpose: Bridge conversation context two-way between **Oh My Pi (`omp`)** and **Google Antigravity (`agy`)** without mutating rule files (`AGENTS.md` / `GEMINI.md`).

## Architecture Overview

1. **Core Read/Export/Redaction Engine (`ctxbridge.py`)**
   - Opens OMP session `.jsonl` / `history.db` and Antigravity `conversation_summaries.db` / `transcript*.jsonl` strictly read-only (copying SQLite + `-wal`/`-shm` into `%TEMP%` when locked).
   - Builds token-budgeted Markdown packs (`ceil(chars / 4)`, summary capped at 40%, chronological recent messages from newest back).
   - Applies best-effort secret redaction (`sk-...`, `AIza...`, `ghp_...`, `github_pat_...`, `Bearer ...`, PEM blocks, `password/token/api_key/secret=...`) and path scrubbing (`user_home` -> `~`, other home dirs -> `<home>`).

2. **Two-Way Stdio MCP Server (`ctxbridge_mcp.py`)**
   - Pure Python 3.12 standard library JSON-RPC 2.0 over `stdio` (newline-delimited JSON messages, zero external packages, ~20–25 MB RSS).
   - Exposes 5 tools:
     - `list_sessions(source="omp"|"agy"|"all", limit=10)`: Lists recent sessions with scrubbed project paths.
     - `get_context_pack(source="omp"|"agy", id="latest", budget_tokens=4000)`: Exports a redacted Markdown pack.
     - `search_sessions(query, source="omp"|"agy"|"all", limit=10)`: Searches session titles, OMP `history.db` recaps, and Antigravity `conversation_summaries` / `conversations_fts`.
     - `save_handoff(from_tool, summary, project?, next_steps?)`: Saves a redacted JSON handoff note into `~/.ctxbridge/handoffs/` (overrideable via `CTXBRIDGE_HANDOFF_DIR`) without touching `AGENTS.md` or `GEMINI.md`.
     - `get_handoff(project?, limit=3)`: Retrieves recent handoff notes saved by either harness.
     - `import_omp_to_agy(id="latest", max_messages=30, apply=True)`: Experimental converter (`ctxbridge_import.py`) that writes an OMP session directly into Antigravity's `conversations/<uuid>.db` (wire-format protobuf `step_type=14/15`) and `conversation_summaries.db`.

3. **Legacy CLI & GUI (`ctxbridge.py`, `ctxbridge_gui.py`)**
   - Preserves `list`, `export`, and dry-run-by-default `inject` commands plus the tkinter GUI (`--selftest` smoke check included).
