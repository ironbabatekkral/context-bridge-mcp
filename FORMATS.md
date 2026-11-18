# Observed Data Formats

Labels: **[OBSERVED]** verified directly in data stores; **[INFERRED]** indirect/unverified.

## omp (`~/.omp/agent`)

### Sessions
- Location: `sessions/<cwd-slug>/<timestamp>_<uuid>.jsonl`. **[OBSERVED]**
- Each line is a JSON object keyed by `type`. **[OBSERVED]**
- Entry types used:
  - `session`: `id`, `timestamp`, `cwd`, `version` (session ID and working directory). **[OBSERVED]**
  - `title` / `title_change`: `title`, `source`, `updatedAt`. **[OBSERVED]**
  - `compaction`: `summary`, `shortSummary`, `firstKeptEntryId`, `tokensBefore`, `tokensAfter`, `parentId` (primary compiled summary source). **[OBSERVED]**
  - `branch_summary`: `summary`, `fromId`. **[OBSERVED]**
  - `message`: `message.role` in `{user, assistant, toolResult, developer}`, `message.content` list of typed blocks (`text`, `thinking`, `toolCall`, `image`). **[OBSERVED]**
- Excluded from context packs: `thinking`, raw `toolResult` output (noise + secret risk), images, sidecar `*.bash.log` / `*.eval.log` / `*.read.log` / `draft.txt`. **[OBSERVED]**

### `history.db` (`~/.omp/agent/history.db`)
- `history(id, prompt UNIQUE, created_at, cwd, session_id, use_count)` + FTS tables. **[OBSERVED]**
- `session_recaps(session_id, cwd, recap, created_at)` (fallback summary source). **[OBSERVED]**
- `session_titles(session_id, title, updated_at)`. **[OBSERVED]**
- Accessed read-only via `file:...?mode=ro` on a temp copy when WAL-locked.

### `agent.db` — Never Read
- Contains `auth_*` credential tables and is strictly ignored.

### Memories
- `memories/<cwd-slug>/` containing `MEMORY.md`, `memory_summary.md`, `raw_memories.md`, `rollout_summaries/`, `skills/`. **[OBSERVED]**
- Example slug on Windows: `C:\Users\<user>\project` -> `--c--users-<user>-project--`.

## Google Antigravity (`~/.gemini/antigravity` or `~/.gemini/antigravity-ide`)

### Conversation Summaries
- `conversation_summaries.db`, table `conversation_summaries`: `conversation_id, title, preview, step_count, last_modified_time, workspace_uris, status, source, project_id, agent_name, raw_summary`. **[OBSERVED]**
- `conversations_fts`: `conversation_id, title, workspace, project, user_body, agent_body`. **[OBSERVED]**

### Step Transcripts
- `brain/<uuid>/.system_generated/logs/transcript_full.jsonl` or `transcript.jsonl`. **[OBSERVED]**
- Each JSONL record includes `step_index, source, type, status, created_at` plus type-specific fields:
  - `USER_INPUT`, `CHECKPOINT`, `GENERIC`: `content`. **[OBSERVED]**
  - `PLANNER_RESPONSE`: `thinking`, `tool_calls`, and assistant `content`. **[OBSERVED]**
- Included in context packs: `USER_INPUT.content`, `PLANNER_RESPONSE.content`, `CHECKPOINT.content` (fallback summary).
- Excluded: `thinking`, raw `tool_calls` payloads.

### Opaque / Excluded Artifacts
- `conversations/<uuid>.db` (`step_payload` protobuf blobs), `*.pb`, `*.pbtxt`. **[OBSERVED: opaque]**

## Sensitive Files (NEVER Read)
- `~/.gemini/oauth_creds.json`, `~/.gemini/google_accounts.json`, `~/.gemini/antigravity-cli/antigravity-oauth-token`
- `~/.omp/agent/abacus_key.txt`, `kimi-device-id`, `*.env`, `*.pem`, `*.key`
- `auth_*` tables inside `agent.db`
