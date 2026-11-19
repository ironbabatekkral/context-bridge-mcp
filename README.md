# context-bridge-mcp

Two-way **stdio MCP server** (plus CLI & GUI) that lets **Oh My Pi (`omp`)** and **Google Antigravity** inspect, search, and pull redacted session context from each other in real time — without mutating `AGENTS.md` or `GEMINI.md`.

- **Zero dependencies:** Python 3.12+ standard library only (~23 MB RAM, fast cold start).
- **Read-only against source stores:** SQLite databases (`history.db`, `conversation_summaries.db`) are copied safely (WAL-aware) to temp storage and opened `mode=ro`.
- **Two-way by design:** Both harnesses run the exact same `ctxbridge_mcp.py` server and can share explicit handoff notes in `~/.ctxbridge/handoffs/`.
- **Secret & path scrubbing:** Automatically masks home paths (`~` / `<home>`) and redacts common API keys, Bearer tokens, PEM blocks, and key/value credentials before returning anything to the model. *(Redaction is best-effort.)*

## MCP Tools

| Tool | Description |
| :--- | :--- |
| `list_sessions` | List recent `omp` and/or `agy` sessions (`source`: `omp` \| `agy` \| `all`, `limit`: `1..50`). |
| `get_context_pack` | Export a redacted, token-budgeted Markdown context pack (`source`, `id` or `"latest"`, `budget_tokens`: `200..32000`). |
| `search_sessions` | Case-insensitive keyword search across OMP titles/`history.db` recaps and Antigravity summaries/FTS (`query`, `source`, `limit`). |
| `save_handoff` | Save a redacted handoff summary + next steps to `~/.ctxbridge/handoffs/` without touching `AGENTS.md` or `GEMINI.md`. |
| `get_handoff` | Read recent handoff notes saved by the other harness (optional `project` filter). |
| `import_omp_to_agy` | Convert one or batch-import OMP sessions (`id="latest"\|"all"`, `limit`) into native Antigravity conversation DBs, mapping OMP compactions to native `CHECKPOINT` (`step_type=23`) steps, extracting images (`omp_media/`), and preserving full timelines. |
| `import_agy_to_omp` | Convert one or batch-import Antigravity conversations (`id="latest"\|"all"`, `limit`) into native OMP v3 `.jsonl` sessions with `blobs/` image support and `history.db` metadata. |

## Setup

Clone the repository:

```bash
git clone https://github.com/ironbabatekkral/context-bridge-mcp.git
```

### 1. Google Antigravity (`~/.gemini/config/mcp_config.json`)

```json
{
  "mcpServers": {
    "context-bridge": {
      "command": "python",
      "args": ["/absolute/path/to/context-bridge-mcp/ctxbridge_mcp.py"]
    }
  }
}
```

### 2. Oh My Pi (`~/.omp/agent/mcp.json`)

```json
{
  "mcpServers": {
    "context-bridge": {
      "type": "stdio",
      "command": "python",
      "args": ["/absolute/path/to/context-bridge-mcp/ctxbridge_mcp.py"]
    }
  }
}
```

### 3. Other MCP Clients (Claude Code / Codex)

Any MCP client supporting `stdio` can run:

```bash
python /absolute/path/to/context-bridge-mcp/ctxbridge_mcp.py
```

Environment overrides (optional):
- `CTXBRIDGE_OMP_DIR` — override `~/.omp/agent`
- `CTXBRIDGE_AGY_DIR` — override auto-detected `~/.gemini/antigravity*`
- `CTXBRIDGE_HANDOFF_DIR` — override `~/.ctxbridge/handoffs`

## CLI & GUI (Optional)

The underlying CLI and tkinter GUI remain available:

```bash
python ctxbridge.py list omp --limit 20
python ctxbridge.py list agy --limit 20
python ctxbridge.py export omp latest --budget-tokens 4000 -o pack.md
python ctxbridge.py export agy <conversation-id> -o pack.md
python ctxbridge.py inject omp pack.md --project /path/to/project          # dry-run
python ctxbridge.py inject omp pack.md --project /path/to/project --apply  # writes
python ctxbridge_gui.py            # GUI
python ctxbridge_gui.py --selftest # headless smoke test

# Import OMP sessions directly as native Antigravity conversations
python ctxbridge_import.py --to agy --id latest --apply
python ctxbridge_import.py --to agy --limit 20 --apply

# Import Antigravity conversations directly as native OMP v3 sessions
python ctxbridge_import.py --to omp --id latest --apply
python ctxbridge_import.py --to omp --all --apply
```

## Tests

```bash
python -m unittest discover -v
```

*(Tests use synthetic temporary directories and fake tokens only.)*

## Files

- `ctxbridge_mcp.py`: Pure-stdlib stdio JSON-RPC 2.0 MCP server (7 tools)
- `ctxbridge_import.py`: Two-way native converter (`--to agy|omp`, `--id`, `--limit N`, `--all`, `--apply`) with native `CHECKPOINT` (`step_type=23`) compaction mapping and two-way image/blob bridging
- `ctxbridge.py`: Core session parser, redactor, token budgeter, and CLI
- `ctxbridge_gui.py`: Optional tkinter front end
- `test_ctxbridge.py` & `test_ctxbridge_mcp.py`: Unit and stdio subprocess test suites
- `DESIGN.md` & `FORMATS.md`: Architecture notes and observed data formats

## License

MIT. See [LICENSE](LICENSE).
