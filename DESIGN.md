# `context-bridge-mcp` Design

Purpose: Bridge conversation context two-way between **Oh My Pi (`omp`)** and **Google Antigravity (`agy`)** without mutating rule files (`AGENTS.md` / `GEMINI.md`).

## Architecture Overview

1. **Core Read/Export/Redaction Engine (`ctxbridge.py`)**
   - Opens OMP session `.jsonl` / `history.db` and Antigravity `conversation_summaries.db` / `transcript*.jsonl` strictly read-only (copying SQLite + `-wal`/`-shm` into `%TEMP%` when locked).
   - Builds token-budgeted Markdown packs (`ceil(chars / 4)`, summary capped at 40%, chronological recent messages from newest back).
   - Applies best-effort secret redaction (`sk-...`, `AIza...`, `ghp_...`, `github_pat_...`, `Bearer ...`, PEM blocks, `password/token/api_key/secret=...`) and path scrubbing (`user_home` -> `~`, other home dirs -> `<home>`).

2. **Two-Way Stdio MCP Server (`ctxbridge_mcp.py`)**
   - Pure Python 3.12 standard library JSON-RPC 2.0 over `stdio` (newline-delimited JSON messages, zero external packages, ~20–25 MB RSS).
   - Exposes 7 tools:
     - `list_sessions(source="omp"|"agy"|"all", limit=10)`: Lists recent sessions with scrubbed project paths.
     - `get_context_pack(source="omp"|"agy", id="latest", budget_tokens=4000)`: Exports a redacted Markdown pack.
     - `search_sessions(query, source="omp"|"agy"|"all", limit=10)`: Searches session titles, OMP `history.db` recaps, and Antigravity `conversation_summaries` / `conversations_fts`.
     - `save_handoff(from_tool, summary, project?, next_steps?)`: Saves a redacted JSON handoff note into `~/.ctxbridge/handoffs/` (overrideable via `CTXBRIDGE_HANDOFF_DIR`) without touching `AGENTS.md` or `GEMINI.md`.
     - `get_handoff(project?, limit=3)`: Retrieves recent handoff notes saved by either harness.
     - `import_omp_to_agy(id="latest"|"all", limit=20, max_messages=0, apply=True)`: Two-way native converter (`ctxbridge_import.py`) that writes full OMP session timelines directly into Antigravity's `conversations/<uuid>.db` (`step_type=14/15`, mapping OMP `compaction`/`branch_summary` entries to native Antigravity `CHECKPOINT` `step_type=23`, merging consecutive assistant fragments, extracting OMP blob/inline images into `brain/<uuid>/omp_media/`, `PRAGMA user_version=1`, WAL, `trajectory_metadata_blob`), `annotations/*.pbtxt`, `transcript.jsonl`, and `conversation_summaries.db` (`raw_summary` protobuf BLOB).
     - `import_agy_to_omp(id="latest"|"all", limit=20, apply=True)`: Reverse native converter (`ctxbridge_import.py`) that writes Antigravity transcripts (`USER_INPUT`, `PLANNER_RESPONSE`, `CHECKPOINT`) into native OMP v3 `.jsonl` sessions, stores local images in `~/.omp/agent/blobs/<sha256>`, and updates `history.db` (`session_titles`, `session_recaps`). Skips `[OMP]`-prefixed conversations in batch mode (and `[AGY]`-prefixed ones in `import_omp_to_agy` batch mode) to prevent ping-pong loops.

3. **Legacy CLI & GUI (`ctxbridge.py`, `ctxbridge_gui.py`)**
   - Preserves `list`, `export`, and dry-run-by-default `inject` commands plus the tkinter GUI (`--selftest` smoke check included).
