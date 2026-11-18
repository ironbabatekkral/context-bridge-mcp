#!/usr/bin/env python3
"""Pure-stdlib two-way stdio MCP server wrapping ctxbridge (OMP <-> Antigravity)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sys
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import ctxbridge  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "context-bridge-mcp", "version": "2.0.0"}

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "list_sessions",
        "description": "List recent OMP and/or Antigravity sessions (read-only, path-scrubbed).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["omp", "agy", "all"],
                    "default": "all",
                    "description": "Which harness to list: 'omp', 'agy', or 'all'.",
                },
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Max sessions per source (1..50).",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "get_context_pack",
        "description": "Export a redacted, token-budgeted Markdown context pack from an OMP or Antigravity session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source": {
                    "type": "string",
                    "enum": ["omp", "agy"],
                    "description": "Source harness: 'omp' or 'agy'.",
                },
                "id": {
                    "type": "string",
                    "default": "latest",
                    "description": "Session/conversation ID prefix (min 8 chars) or 'latest'.",
                },
                "budget_tokens": {
                    "type": "integer",
                    "default": 4000,
                    "minimum": 200,
                    "maximum": 32000,
                    "description": "Approximate token budget (200..32000).",
                },
            },
            "required": ["source"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_sessions",
        "description": "Search OMP and/or Antigravity session titles, summaries, and transcripts by keyword.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Case-insensitive keyword or phrase to search for.",
                },
                "source": {
                    "type": "string",
                    "enum": ["omp", "agy", "all"],
                    "default": "all",
                    "description": "Which harness to search: 'omp', 'agy', or 'all'.",
                },
                "limit": {
                    "type": "integer",
                    "default": 10,
                    "minimum": 1,
                    "maximum": 30,
                    "description": "Maximum total matches to return (1..30).",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "save_handoff",
        "description": "Save a redacted cross-harness handoff note to ~/.ctxbridge/handoffs/ without touching AGENTS.md or GEMINI.md.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "from_tool": {
                    "type": "string",
                    "enum": ["omp", "agy"],
                    "description": "Harness saving the handoff note.",
                },
                "summary": {
                    "type": "string",
                    "description": "Concise summary of current state, decisions, and findings.",
                },
                "project": {
                    "type": "string",
                    "description": "Optional project name or workspace path.",
                },
                "next_steps": {
                    "type": "string",
                    "description": "Optional next steps for the receiving harness.",
                },
            },
            "required": ["from_tool", "summary"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_handoff",
        "description": "Read recent redacted handoff notes saved by the other harness.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "Optional case-insensitive project substring filter.",
                },
                "limit": {
                    "type": "integer",
                    "default": 3,
                    "minimum": 1,
                    "maximum": 20,
                    "description": "Maximum number of handoff notes to return (1..20).",
                },
            },
            "additionalProperties": False,
        },
    },
]


def _clamp_int(val: Any, default: int, lo: int, hi: int) -> int:
    if val is None:
        return default
    if isinstance(val, bool) or not isinstance(val, int):
        try:
            val = int(val)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Expected integer, got {val!r}") from exc
    return max(lo, min(hi, val))


def handoff_dir() -> Path:
    env = os.environ.get("CTXBRIDGE_HANDOFF_DIR")
    if env:
        return Path(env)
    return ctxbridge.user_home() / ".ctxbridge" / "handoffs"


def _snippet(text: str, needle: str, radius: int = 90) -> str:
    clean = " ".join(text.split())
    if not clean:
        return ""
    idx = clean.lower().find(needle.lower())
    if idx < 0:
        return clean[: radius * 2] + ("..." if len(clean) > radius * 2 else "")
    start = max(0, idx - radius)
    end = min(len(clean), idx + len(needle) + radius)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(clean) else ""
    return prefix + clean[start:end] + suffix


def _table_exists(con, table_name: str) -> bool:
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def tool_list_sessions(args: dict[str, Any]) -> str:
    source = str(args.get("source", "all")).lower()
    if source not in ("omp", "agy", "all"):
        raise ValueError("source must be 'omp', 'agy', or 'all'")
    limit = _clamp_int(args.get("limit"), 10, 1, 50)

    sessions: list[dict[str, Any]] = []
    errors: list[str] = []

    if source in ("omp", "all"):
        try:
            for when, sid, cwd, title in ctxbridge.omp_list_rows(limit):
                sessions.append(
                    {
                        "source": "omp",
                        "id": sid,
                        "updated": when,
                        "project": ctxbridge.redact(str(cwd or "")),
                        "title": ctxbridge.redact(str(title or "")),
                    }
                )
        except ctxbridge.NotFound as exc:
            if source == "omp":
                raise
            errors.append(str(exc))

    if source in ("agy", "all"):
        try:
            for mtime, cid, steps, workspace, title in ctxbridge.agy_list_rows(limit):
                sessions.append(
                    {
                        "source": "agy",
                        "id": cid,
                        "updated": mtime,
                        "steps": steps,
                        "project": ctxbridge.redact(str(workspace or "")),
                        "title": ctxbridge.redact(str(title or "")),
                    }
                )
        except ctxbridge.NotFound as exc:
            if source == "agy":
                raise
            errors.append(str(exc))

    out: dict[str, Any] = {"sessions": sessions}
    if errors and not sessions:
        out["warnings"] = errors
    return json.dumps(out, ensure_ascii=False, indent=2)


def tool_get_context_pack(args: dict[str, Any]) -> str:
    source = args.get("source")
    if source not in ("omp", "agy"):
        raise ValueError("source is required and must be 'omp' or 'agy'")
    ident = str(args.get("id") or "latest")
    budget = _clamp_int(args.get("budget_tokens"), 4000, 200, 32000)
    return ctxbridge.export_text(source, ident, budget)


def _search_omp(q: str, limit: int) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    q_low = q.lower()

    if ctxbridge.omp_sessions_dir().is_dir():
        for when, sid, cwd, title in ctxbridge.omp_list_rows(200):
            t_str = str(title or "")
            c_str = str(cwd or "")
            if q_low in t_str.lower() or q_low in c_str.lower():
                seen_ids.add(sid)
                matches.append(
                    {
                        "source": "omp",
                        "id": sid,
                        "updated": when,
                        "title": ctxbridge.redact(t_str),
                        "project": ctxbridge.redact(c_str),
                        "snippet": ctxbridge.redact(_snippet(t_str or c_str, q)),
                    }
                )
                if len(matches) >= limit:
                    return matches

    hpath = ctxbridge.omp_history_path()
    if hpath.is_file():
        like = f"%{q}%"
        with ctxbridge.ro_db(hpath) as con:
            if _table_exists(con, "session_recaps"):
                rows = con.execute(
                    "SELECT session_id, cwd, recap, created_at FROM session_recaps "
                    "WHERE lower(coalesce(recap,'')) LIKE lower(?) OR lower(coalesce(cwd,'')) LIKE lower(?) "
                    "ORDER BY created_at DESC LIMIT ?",
                    (like, like, limit * 2),
                ).fetchall()
                titles = ctxbridge.omp_db_titles()
                for sid, cwd, recap, created_at in rows:
                    sid_s = str(sid or "")
                    if not sid_s or sid_s in seen_ids:
                        continue
                    seen_ids.add(sid_s)
                    matches.append(
                        {
                            "source": "omp",
                            "id": sid_s,
                            "updated": str(created_at or ""),
                            "title": ctxbridge.redact(str(titles.get(sid_s, ""))),
                            "project": ctxbridge.redact(str(cwd or "")),
                            "snippet": ctxbridge.redact(_snippet(str(recap or ""), q)),
                        }
                    )
                    if len(matches) >= limit:
                        return matches

            if _table_exists(con, "session_titles"):
                rows = con.execute(
                    "SELECT session_id, title, updated_at FROM session_titles "
                    "WHERE lower(coalesce(title,'')) LIKE lower(?) ORDER BY updated_at DESC LIMIT ?",
                    (like, limit * 2),
                ).fetchall()
                for sid, title, updated_at in rows:
                    sid_s = str(sid or "")
                    if not sid_s or sid_s in seen_ids:
                        continue
                    seen_ids.add(sid_s)
                    matches.append(
                        {
                            "source": "omp",
                            "id": sid_s,
                            "updated": str(updated_at or ""),
                            "title": ctxbridge.redact(str(title or "")),
                            "project": "",
                            "snippet": ctxbridge.redact(_snippet(str(title or ""), q)),
                        }
                    )
                    if len(matches) >= limit:
                        return matches
    return matches


def _search_agy(q: str, limit: int) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    q_low = q.lower()

    spath = ctxbridge.agy_summaries_path()
    if not spath.is_file():
        return matches

    with ctxbridge.ro_db(spath) as con:
        rows = con.execute(
            "SELECT conversation_id, title, preview, raw_summary, workspace_uris, last_modified_time "
            "FROM conversation_summaries ORDER BY last_modified_time DESC"
        ).fetchall()
        for cid, title, preview, raw_summary, workspace, mtime in rows:
            cid_s = str(cid or "")
            t_s = str(title or "")
            p_s = str(preview or "")
            r_s = str(raw_summary or "")
            w_s = str(workspace or "")
            hay = "\n".join((t_s, p_s, r_s, w_s))
            if q_low in hay.lower():
                seen_ids.add(cid_s)
                chosen = r_s if q_low in r_s.lower() else (p_s if q_low in p_s.lower() else t_s)
                matches.append(
                    {
                        "source": "agy",
                        "id": cid_s,
                        "updated": str(mtime or ""),
                        "title": ctxbridge.redact(t_s),
                        "project": ctxbridge.redact(w_s),
                        "snippet": ctxbridge.redact(_snippet(chosen or w_s, q)),
                    }
                )
                if len(matches) >= limit:
                    return matches

        if _table_exists(con, "conversations_fts"):
            like = f"%{q}%"
            fts_rows = con.execute(
                "SELECT conversation_id, title, workspace, user_body, agent_body FROM conversations_fts "
                "WHERE lower(coalesce(title,'')) LIKE lower(?) "
                "OR lower(coalesce(user_body,'')) LIKE lower(?) "
                "OR lower(coalesce(agent_body,'')) LIKE lower(?) "
                "OR lower(coalesce(workspace,'')) LIKE lower(?) "
                "LIMIT ?",
                (like, like, like, like, limit * 2),
            ).fetchall()
            for cid, title, workspace, user_body, agent_body in fts_rows:
                cid_s = str(cid or "")
                if not cid_s or cid_s in seen_ids:
                    continue
                seen_ids.add(cid_s)
                ub = str(user_body or "")
                ab = str(agent_body or "")
                t_s = str(title or "")
                chosen = ub if q_low in ub.lower() else (ab if q_low in ab.lower() else t_s)
                matches.append(
                    {
                        "source": "agy",
                        "id": cid_s,
                        "updated": "",
                        "title": ctxbridge.redact(t_s),
                        "project": ctxbridge.redact(str(workspace or "")),
                        "snippet": ctxbridge.redact(_snippet(chosen, q)),
                    }
                )
                if len(matches) >= limit:
                    return matches
    return matches


def tool_search_sessions(args: dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        raise ValueError("query is required and cannot be empty")
    source = str(args.get("source", "all")).lower()
    if source not in ("omp", "agy", "all"):
        raise ValueError("source must be 'omp', 'agy', or 'all'")
    limit = _clamp_int(args.get("limit"), 10, 1, 30)

    matches: list[dict[str, Any]] = []
    if source in ("omp", "all"):
        matches.extend(_search_omp(query, limit))
    if source in ("agy", "all") and len(matches) < limit:
        matches.extend(_search_agy(query, limit - len(matches)))

    return json.dumps(
        {"query": ctxbridge.redact(query), "matches": matches[:limit]},
        ensure_ascii=False,
        indent=2,
    )


def tool_save_handoff(args: dict[str, Any]) -> str:
    from_tool = args.get("from_tool")
    if from_tool not in ("omp", "agy"):
        raise ValueError("from_tool is required and must be 'omp' or 'agy'")
    summary = str(args.get("summary") or "").strip()
    if not summary:
        raise ValueError("summary is required and cannot be empty")
    project = str(args.get("project") or "").strip()
    next_steps = str(args.get("next_steps") or "").strip()

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    hid = f"{stamp}-{from_tool}-{secrets.token_hex(3)}"

    hdir = handoff_dir()
    hdir.mkdir(parents=True, exist_ok=True)
    fpath = hdir / f"{hid}.json"

    record = {
        "id": hid,
        "from_tool": from_tool,
        "created_at": now.isoformat(timespec="seconds"),
        "project": ctxbridge.redact(project),
        "summary": ctxbridge.redact(summary),
        "next_steps": ctxbridge.redact(next_steps),
    }
    fpath.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    return json.dumps(
        {
            "status": "saved",
            "id": hid,
            "from_tool": from_tool,
            "path": ctxbridge.scrub_paths(str(fpath)),
        },
        ensure_ascii=False,
        indent=2,
    )


def tool_get_handoff(args: dict[str, Any]) -> str:
    project_filter = str(args.get("project") or "").strip().lower()
    limit = _clamp_int(args.get("limit"), 3, 1, 20)

    hdir = handoff_dir()
    if not hdir.is_dir():
        return json.dumps({"handoffs": []}, ensure_ascii=False, indent=2)

    files = sorted(
        hdir.glob("*.json"),
        key=lambda p: (p.stat().st_mtime, p.name),
        reverse=True,
    )
    handoffs: list[dict[str, Any]] = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        if project_filter:
            proj = str(data.get("project") or "").lower()
            if project_filter not in proj:
                continue
        handoffs.append(data)
        if len(handoffs) >= limit:
            break

    return json.dumps({"handoffs": handoffs}, ensure_ascii=False, indent=2)


TOOL_DISPATCH = {
    "list_sessions": tool_list_sessions,
    "get_context_pack": tool_get_context_pack,
    "search_sessions": tool_search_sessions,
    "save_handoff": tool_save_handoff,
    "get_handoff": tool_get_handoff,
}


def handle_request(req: dict[str, Any]) -> dict[str, Any] | None:
    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params") if isinstance(req.get("params"), dict) else {}

    if method == "notifications/initialized" or req_id is None:
        return None

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOL_SCHEMAS},
        }

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": "Error: arguments must be an object"}],
                    "isError": True,
                },
            }
        fn = TOOL_DISPATCH.get(str(name))
        if fn is None:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Error: unknown tool '{name}'"}],
                    "isError": True,
                },
            }
        try:
            text = fn(arguments)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": False,
                },
            }
        except (ctxbridge.CtxError, ValueError, KeyError, OSError) as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Error: {exc}"}],
                    "isError": True,
                },
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def serve_stdio() -> int:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as exc:
            err_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
            sys.stdout.write(json.dumps(err_resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue

        if not isinstance(req, dict):
            err_resp = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request"},
            }
            sys.stdout.write(json.dumps(err_resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
            continue

        resp = handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(serve_stdio())
