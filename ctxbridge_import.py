#!/usr/bin/env python3
"""Import OMP sessions directly into Google Antigravity's native conversation DB format.

Maps OMP messages chronologically (step_type=14/15), merges consecutive assistant
tool-call fragments into clean turns, converts OMP compactions/branch summaries into
native Antigravity CHECKPOINT steps (step_type=23), and writes protobuf summaries.
Pure Python 3.12 standard library only.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
import time
import uuid

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import ctxbridge  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

MAX_STEP_TEXT_CHARS = 200_000


# ---------------------------------------------------------------- minimal protobuf encoder

def _varint(n: int) -> bytes:
    if n < 0:
        n &= (1 << 64) - 1
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _pb_varint(field_no: int, val: int) -> bytes:
    return _varint((field_no << 3) | 0) + _varint(val)


def _pb_bytes(field_no: int, data: bytes) -> bytes:
    return _varint((field_no << 3) | 2) + _varint(len(data)) + data


def _pb_str(field_no: int, text: str) -> bytes:
    return _pb_bytes(field_no, text.encode("utf-8", errors="replace"))


def _pb_ts(field_no: int, epoch_sec: int) -> bytes:
    return _pb_bytes(field_no, _pb_varint(1, max(0, int(epoch_sec))) + _pb_varint(2, 0))


def _build_metadata(
    epoch_sec: int,
    status_code: int,
    step_uuid: str,
    traj_id: str,
    idx: int,
    conv_id: str,
) -> bytes:
    cascade = (
        _pb_str(1, traj_id)
        + _pb_varint(2, idx)
        + _pb_str(4, conv_id)
    )
    return (
        _pb_ts(1, epoch_sec)
        + _pb_varint(3, status_code)
        + _pb_str(12, step_uuid)
        + _pb_bytes(20, cascade)
    )


def _build_user_payload(meta_pb: bytes, text: str, item_uuid: str) -> bytes:
    inner_19 = (
        _pb_str(2, text)
        + _pb_bytes(3, _pb_str(1, text))
        + _pb_str(19, item_uuid)
    )
    return (
        _pb_varint(1, 14)
        + _pb_varint(4, 3)
        + _pb_bytes(5, meta_pb)
        + _pb_bytes(19, inner_19)
    )


def _build_assistant_payload(meta_pb: bytes, text: str) -> bytes:
    inner_20 = (
        _pb_str(1, text)
        + _pb_str(8, text)
        + _pb_varint(12, 2)
    )
    return (
        _pb_varint(1, 15)
        + _pb_varint(4, 3)
        + _pb_bytes(5, meta_pb)
        + _pb_bytes(20, inner_20)
    )


def _build_checkpoint_payload(
    meta_pb: bytes,
    summary_text: str,
    prior_user_count: int,
    step_idx: int,
    brain_uri: str,
    prior_user_prompts: list[str],
) -> bytes:
    inner_30 = bytearray()
    inner_30 += _pb_varint(1, 2)  # compaction checkpoint
    inner_30 += _pb_str(5, summary_text)
    inner_30 += _pb_varint(11, max(0, prior_user_count))
    inner_30 += _pb_varint(12, max(0, step_idx))
    inner_30 += _pb_str(15, brain_uri)
    for prompt in prior_user_prompts[-15:]:
        snippet = prompt.strip()[:300]
        if snippet:
            inner_30 += _pb_str(19, snippet)
    return (
        _pb_varint(1, 23)
        + _pb_varint(4, 3)
        + _pb_bytes(5, meta_pb)
        + _pb_bytes(30, bytes(inner_30))
    )


def _build_trajectory_metadata(
    created_sec: int,
    client_uuid: str,
    conv_id: str,
    project_id: str = "outside-of-project",
) -> bytes:
    return (
        _pb_ts(2, created_sec)
        + _pb_str(3, client_uuid)
        + _pb_str(6, conv_id)
        + _pb_str(18, project_id)
    )


def _build_raw_summary(
    title: str,
    step_count: int,
    created_sec: int,
    last_mod_sec: int,
    last_user_sec: int,
    last_user_idx: int,
    traj_id: str,
    traj_meta_pb: bytes,
) -> bytes:
    annot_pb = _pb_str(1, title) + _pb_ts(7, last_mod_sec)
    return (
        _pb_str(1, title)
        + _pb_varint(2, step_count)
        + _pb_ts(3, last_mod_sec)
        + _pb_str(4, traj_id)
        + _pb_varint(5, 1)
        + _pb_ts(7, created_sec)
        + _pb_ts(10, last_user_sec)
        + _pb_bytes(15, annot_pb)
        + _pb_varint(16, last_user_idx)
        + _pb_bytes(17, traj_meta_pb)
        + _pb_varint(22, 4)
        + _pb_varint(28, 4)
    )


# ---------------------------------------------------------------- SQLite schema

CONV_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS `trajectory_meta` (
    `trajectory_id` text,
    `cascade_id` text,
    `trajectory_type` integer,
    `source` integer,
    PRIMARY KEY (`trajectory_id`)
);
CREATE TABLE IF NOT EXISTS `steps` (
    `idx` integer,
    `step_type` integer NOT NULL DEFAULT 0,
    `status` integer NOT NULL DEFAULT 0,
    `has_subtrajectory` numeric NOT NULL DEFAULT false,
    `metadata` blob,
    `error_details` blob,
    `permissions` blob,
    `task_details` blob,
    `render_info` blob,
    `step_payload` blob,
    `step_format` integer NOT NULL DEFAULT 0,
    PRIMARY KEY (`idx`)
);
CREATE TABLE IF NOT EXISTS `gen_metadata` (
    `idx` integer,
    `data` blob,
    `size` integer NOT NULL DEFAULT 0,
    PRIMARY KEY (`idx`)
);
CREATE TABLE IF NOT EXISTS `executor_metadata` (
    `idx` integer,
    `data` blob,
    PRIMARY KEY (`idx`)
);
CREATE TABLE IF NOT EXISTS `parent_references` (
    `idx` integer,
    `data` blob,
    PRIMARY KEY (`idx`)
);
CREATE TABLE IF NOT EXISTS `trajectory_metadata_blob` (
    `id` text DEFAULT "main",
    `data` blob,
    PRIMARY KEY (`id`)
);
CREATE TABLE IF NOT EXISTS `battle_mode_infos` (
    `idx` integer,
    `data` blob,
    PRIMARY KEY (`idx`)
);
"""


def _ensure_summaries_schema(con: sqlite3.Connection) -> None:
    con.execute(
        "CREATE TABLE IF NOT EXISTS `conversation_summaries` ("
        "`conversation_id` text, `title` text NOT NULL DEFAULT '', "
        "`preview` text NOT NULL DEFAULT '', `step_count` integer NOT NULL DEFAULT 0, "
        "`last_modified_time` datetime NOT NULL, `workspace_uris` text NOT NULL, "
        "`status` text NOT NULL DEFAULT '', `source` text NOT NULL DEFAULT '', "
        "`project_id` text NOT NULL DEFAULT '', `agent_name` text NOT NULL DEFAULT '', "
        "`parent_conversation_id` text NOT NULL DEFAULT '', `nesting_depth` integer NOT NULL DEFAULT 0, "
        "`battle_id` text NOT NULL DEFAULT '', `winning_conversation_id` text NOT NULL DEFAULT '', "
        "`not_fully_idle` numeric NOT NULL DEFAULT false, `killed` numeric NOT NULL DEFAULT false, "
        "`last_user_input_time` datetime NOT NULL, `last_user_input_step_index` integer NOT NULL DEFAULT -1, "
        "`app_data_dir` text NOT NULL DEFAULT '', `raw_summary` blob, "
        "`group_id` TEXT NOT NULL DEFAULT '', PRIMARY KEY (`conversation_id`))"
    )


# ---------------------------------------------------------------- OMP chronological parser

def _parse_iso_epoch(raw: str | int | float | None, fallback: int) -> int:
    if isinstance(raw, (int, float)):
        val = int(raw)
        return val // 1000 if val > 10_000_000_000 else val
    if not isinstance(raw, str) or not raw.strip():
        return fallback
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return fallback


def _cap_text(text: str) -> str:
    if len(text) <= MAX_STEP_TEXT_CHARS:
        return text
    return text[: MAX_STEP_TEXT_CHARS - 20] + "\n...[truncated]"


def _parse_omp_timeline(
    path: Path, db_titles: dict[str, str] | None = None
) -> tuple[dict, list[tuple[str, int, str]]]:
    """Read an OMP session JSONL chronologically.

    Returns (meta_dict, timeline) where timeline entries are (kind, epoch_sec, redacted_text)
    with kind in {'user', 'assistant', 'checkpoint'}.
    """
    mtime = int(path.stat().st_mtime)
    sid = path.stem.split("_")[-1]
    cwd = ""
    title = ""
    created_sec: int | None = None
    timeline: list[tuple[str, int, str]] = []

    for obj in ctxbridge.read_jsonl(path):
        if not isinstance(obj, dict):
            continue
        typ = obj.get("type")
        ts = _parse_iso_epoch(obj.get("timestamp"), mtime)
        if created_sec is None and obj.get("timestamp"):
            created_sec = ts

        if typ == "session":
            if obj.get("id"):
                sid = str(obj["id"])
            if obj.get("cwd"):
                cwd = str(obj["cwd"])
        elif typ in ("title", "title_change") and obj.get("title"):
            title = str(obj["title"])
        elif typ in ("compaction", "branch_summary"):
            summary_raw = obj.get("summary")
            if isinstance(summary_raw, str) and summary_raw.strip():
                red = _cap_text(ctxbridge.redact(summary_raw.strip()))
                timeline.append(("checkpoint", ts, red))
        elif typ == "message":
            msg = obj.get("message") or {}
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            raw_text = ctxbridge.omp_text(msg.get("content")).strip()
            if not raw_text:
                continue
            msg_ts = _parse_iso_epoch(msg.get("timestamp") or obj.get("timestamp"), ts)
            red = _cap_text(ctxbridge.redact(raw_text))
            if role == "assistant" and timeline and timeline[-1][0] == "assistant":
                prev_kind, _prev_ts, prev_text = timeline[-1]
                timeline[-1] = (prev_kind, msg_ts, _cap_text(prev_text + "\n\n" + red))
            else:
                timeline.append((role, msg_ts, red))

    if db_titles is None:
        db_titles = ctxbridge.omp_db_titles()
    title = db_titles.get(sid) or title

    if not timeline:
        recap = ctxbridge.omp_db_recap(sid)
        if isinstance(recap, str) and recap.strip():
            timeline.append(("checkpoint", mtime, _cap_text(ctxbridge.redact(recap.strip()))))

    meta = {
        "id": sid,
        "cwd": cwd,
        "title": ctxbridge.redact(title or f"OMP {sid[:8]}"),
        "created_sec": created_sec if created_sec is not None else max(0, mtime - max(10, len(timeline) * 2)),
        "mtime": mtime,
    }
    return meta, timeline


def _trim_timeline(
    timeline: list[tuple[str, int, str]], max_messages: int
) -> list[tuple[str, int, str]]:
    """Keep all items when max_messages <= 0, otherwise keep latest messages + preceding checkpoint."""
    if max_messages <= 0:
        return timeline
    msg_indices = [i for i, item in enumerate(timeline) if item[0] in ("user", "assistant")]
    if len(msg_indices) <= max_messages:
        return timeline
    cutoff = msg_indices[-max_messages]
    preceding_cp = [item for item in timeline[:cutoff] if item[0] == "checkpoint"]
    kept_tail = timeline[cutoff:]
    if preceding_cp:
        return [preceding_cp[-1]] + kept_tail
    return kept_tail


# ---------------------------------------------------------------- importer

def import_omp_to_agy(
    ident: str = "latest",
    max_messages: int = 0,
    apply: bool = True,
    agy_dir: Path | None = None,
    _session_path: Path | None = None,
    _db_titles: dict[str, str] | None = None,
) -> dict:
    """Convert an OMP session into an Antigravity conversation DB + summary row."""
    session_path = _session_path if _session_path is not None else ctxbridge.find_omp_session(ident)
    meta, raw_timeline = _parse_omp_timeline(session_path, db_titles=_db_titles)
    timeline = _trim_timeline(raw_timeline, max_messages)

    omp_id = meta["id"]
    conv_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ctxbridge-omp:{omp_id}"))
    traj_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ctxbridge-traj:{omp_id}"))

    title_raw = (meta.get("title") or f"OMP {omp_id[:8]}").strip()
    title = f"[OMP] {title_raw}" if not title_raw.startswith("[OMP]") else title_raw

    if not timeline:
        timeline = [("user", meta["mtime"], f"[Imported OMP Session {omp_id}]")]

    target_root = Path(agy_dir) if agy_dir else ctxbridge.agy_root()
    conv_db_path = target_root / "conversations" / f"{conv_id}.db"
    brain_dir = target_root / "brain" / conv_id
    transcript_path = brain_dir / ".system_generated" / "logs" / "transcript.jsonl"
    annot_path = target_root / "annotations" / f"{conv_id}.pbtxt"
    summaries_db_path = target_root / "conversation_summaries.db"

    checkpoints = sum(1 for item in timeline if item[0] == "checkpoint")
    user_turns = sum(1 for item in timeline if item[0] == "user")

    if not apply:
        return {
            "status": "dry_run",
            "omp_id": omp_id,
            "conversation_id": conv_id,
            "title": title,
            "step_count": len(timeline),
            "user_turns": user_turns,
            "checkpoints": checkpoints,
            "target_db": ctxbridge.scrub_paths(str(conv_db_path)),
        }

    conv_db_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    annot_path.parent.mkdir(parents=True, exist_ok=True)

    created_sec = int(meta["created_sec"])
    last_mod_sec = max(int(meta["mtime"]), max(item[1] for item in timeline))
    client_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ctxbridge-client:{omp_id}"))
    traj_meta_pb = _build_trajectory_metadata(created_sec, client_uuid, conv_id, "outside-of-project")
    brain_uri = brain_dir.as_uri()

    last_user_idx = 0
    last_user_sec = created_sec
    prior_user_prompts: list[str] = []

    con = sqlite3.connect(str(conv_db_path))
    try:
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA user_version = 1")
        con.executescript(CONV_DB_SCHEMA)
        con.execute("DELETE FROM trajectory_meta")
        con.execute("DELETE FROM steps")
        con.execute(
            "INSERT INTO trajectory_meta (trajectory_id, cascade_id, trajectory_type, source) VALUES (?, ?, 4, 1)",
            (traj_id, conv_id),
        )
        con.execute(
            "INSERT OR REPLACE INTO trajectory_metadata_blob (id, data) VALUES ('main', ?)",
            (sqlite3.Binary(traj_meta_pb),),
        )

        t_lines: list[str] = []
        for idx, (kind, ts, text) in enumerate(timeline):
            step_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{conv_id}:step:{idx}"))
            iso_ts = datetime.fromtimestamp(ts, timezone.utc).isoformat()
            if kind == "user":
                last_user_idx = idx
                last_user_sec = ts
                prior_user_prompts.append(text)
                step_type = 14
                meta_pb = _build_metadata(ts, 4, step_uuid, traj_id, idx, conv_id)
                payload_pb = _build_user_payload(
                    meta_pb, text, str(uuid.uuid5(uuid.NAMESPACE_URL, f"{conv_id}:u:{idx}"))
                )
                t_lines.append(
                    json.dumps(
                        {
                            "step_index": idx,
                            "source": "USER_EXPLICIT",
                            "type": "USER_INPUT",
                            "status": "DONE",
                            "created_at": iso_ts,
                            "content": text,
                        },
                        ensure_ascii=False,
                    )
                )
            elif kind == "checkpoint":
                step_type = 23
                meta_pb = _build_metadata(ts, 5, step_uuid, traj_id, idx, conv_id)
                payload_pb = _build_checkpoint_payload(
                    meta_pb=meta_pb,
                    summary_text=text,
                    prior_user_count=len(prior_user_prompts),
                    step_idx=idx,
                    brain_uri=brain_uri,
                    prior_user_prompts=prior_user_prompts,
                )
                t_lines.append(
                    json.dumps(
                        {
                            "step_index": idx,
                            "source": "SYSTEM",
                            "type": "CHECKPOINT",
                            "status": "DONE",
                            "created_at": iso_ts,
                            "content": text,
                        },
                        ensure_ascii=False,
                    )
                )
            else:
                step_type = 15
                meta_pb = _build_metadata(ts, 2, step_uuid, traj_id, idx, conv_id)
                payload_pb = _build_assistant_payload(meta_pb, text)
                t_lines.append(
                    json.dumps(
                        {
                            "step_index": idx,
                            "source": "MODEL",
                            "type": "PLANNER_RESPONSE",
                            "status": "DONE",
                            "created_at": iso_ts,
                            "content": text,
                        },
                        ensure_ascii=False,
                    )
                )

            con.execute(
                "INSERT INTO steps (idx, step_type, status, has_subtrajectory, metadata, step_payload, step_format) "
                "VALUES (?, ?, 3, 0, ?, ?, 0)",
                (idx, step_type, meta_pb, payload_pb),
            )
        con.commit()
    finally:
        con.close()

    transcript_path.write_text("\n".join(t_lines) + "\n", encoding="utf-8")
    annot_path.write_text(
        f"title:{json.dumps(title, ensure_ascii=False)} last_user_view_time:{{seconds:{last_mod_sec} nanos:0}}",
        encoding="utf-8",
    )

    last_mod_iso = datetime.fromtimestamp(last_mod_sec, timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")
    last_user_iso = datetime.fromtimestamp(last_user_sec, timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")

    raw_summary_pb = _build_raw_summary(
        title=title,
        step_count=len(timeline),
        created_sec=created_sec,
        last_mod_sec=last_mod_sec,
        last_user_sec=last_user_sec,
        last_user_idx=last_user_idx,
        traj_id=traj_id,
        traj_meta_pb=traj_meta_pb,
    )
    summary_updated = False
    summary_warning = None
    try:
        scon = sqlite3.connect(str(summaries_db_path), timeout=5.0)
        try:
            _ensure_summaries_schema(scon)
            existing_cols = {
                r[1]
                for r in scon.execute("PRAGMA table_info(conversation_summaries)").fetchall()
            }
            row_map = {
                "conversation_id": conv_id,
                "title": title,
                "preview": title,
                "step_count": len(timeline),
                "last_modified_time": last_mod_iso,
                "workspace_uris": "",
                "status": "CASCADE_RUN_STATUS_IDLE",
                "source": "",
                "project_id": "outside-of-project",
                "agent_name": "",
                "app_data_dir": "antigravity",
                "last_user_input_time": last_user_iso,
                "last_user_input_step_index": last_user_idx,
                "raw_summary": sqlite3.Binary(raw_summary_pb),
            }
            cols = [c for c in row_map if c in existing_cols]
            placeholders = ", ".join("?" for _ in cols)
            col_sql = ", ".join(cols)
            scon.execute(
                f"INSERT OR REPLACE INTO conversation_summaries ({col_sql}) VALUES ({placeholders})",
                tuple(row_map[c] for c in cols),
            )
            scon.commit()
            summary_updated = True
        finally:
            scon.close()
    except sqlite3.Error as exc:
        summary_warning = str(exc)

    out = {
        "status": "imported",
        "omp_id": omp_id,
        "conversation_id": conv_id,
        "title": title,
        "step_count": len(timeline),
        "user_turns": user_turns,
        "checkpoints": checkpoints,
        "summary_db_updated": summary_updated,
        "target_db": ctxbridge.scrub_paths(str(conv_db_path)),
    }
    if summary_warning:
        out["summary_warning"] = summary_warning
    return out


def import_omp_batch(
    limit: int = 20,
    all_sessions: bool = False,
    max_messages: int = 0,
    apply: bool = True,
    agy_dir: Path | None = None,
) -> dict:
    """Import multiple recent OMP sessions (skipping empty ones)."""
    files = sorted(ctxbridge.omp_session_files(), key=lambda p: p.stat().st_mtime, reverse=True)
    db_titles = ctxbridge.omp_db_titles()

    imported: list[dict] = []
    skipped_empty = 0

    for f in files:
        if not all_sessions and len(imported) >= limit:
            break
        _, timeline = _parse_omp_timeline(f, db_titles=db_titles)
        if not timeline:
            skipped_empty += 1
            continue
        res = import_omp_to_agy(
            max_messages=max_messages,
            apply=apply,
            agy_dir=agy_dir,
            _session_path=f,
            _db_titles=db_titles,
        )
        imported.append(res)

    return {
        "status": "imported" if apply else "dry_run",
        "imported_count": len(imported),
        "skipped_empty": skipped_empty,
        "sessions": imported,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Import OMP sessions into Antigravity Conversations DB")
    parser.add_argument("--id", default="latest", help="OMP session ID prefix or 'latest'")
    parser.add_argument("--limit", type=int, default=0, help="Batch-import top N newest non-empty OMP sessions")
    parser.add_argument("--all", action="store_true", help="Batch-import all non-empty OMP sessions")
    parser.add_argument("--max-messages", type=int, default=0, help="Max recent messages (0 = full timeline)")
    parser.add_argument("--apply", action="store_true", help="Write DBs and summary rows (default: dry-run)")
    args = parser.parse_args()

    if args.all or args.limit > 0:
        res = import_omp_batch(
            limit=max(1, args.limit) if args.limit > 0 else 20,
            all_sessions=args.all,
            max_messages=args.max_messages,
            apply=args.apply,
        )
    else:
        res = import_omp_to_agy(ident=args.id, max_messages=args.max_messages, apply=args.apply)

    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
