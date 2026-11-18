#!/usr/bin/env python3
"""Import an OMP session directly into Google Antigravity's native conversation DB format.

Pure Python 3.12 standard library only (minimal protobuf wire-format encoder + SQLite).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
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
    return _pb_bytes(field_no, _pb_varint(1, epoch_sec) + _pb_varint(2, 0))


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


def import_omp_to_agy(
    ident: str = "latest",
    max_messages: int = 30,
    apply: bool = True,
    agy_dir: Path | None = None,
) -> dict:
    """Convert an OMP session into an Antigravity conversation DB + summary row."""
    meta, summary, raw_messages = ctxbridge.export_omp(ident, budget=16000)
    omp_id = meta["id"]
    conv_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ctxbridge-omp:{omp_id}"))
    traj_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ctxbridge-traj:{omp_id}"))

    title_raw = (meta.get("title") or f"OMP {omp_id[:8]}").strip()
    title = f"[OMP] {title_raw}" if not title_raw.startswith("[OMP]") else title_raw

    msgs: list[tuple[str, str]] = []
    if summary:
        msgs.append(
            ("user", f"[Imported OMP Session {omp_id}]\n\n## Summary\n{ctxbridge.redact(summary)}")
        )
        msgs.append(("assistant", "OMP session summary loaded. Continuing with recent messages below."))
    for role, text in raw_messages[-max_messages:]:
        clean = ctxbridge.redact(text).strip()
        if clean:
            msgs.append((role, clean))

    if not msgs:
        msgs.append(("user", f"[Imported OMP Session {omp_id}]"))

    target_root = Path(agy_dir) if agy_dir else ctxbridge.agy_root()
    conv_db_path = target_root / "conversations" / f"{conv_id}.db"
    transcript_path = (
        target_root / "brain" / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    )
    annot_path = target_root / "annotations" / f"{conv_id}.pbtxt"
    summaries_db_path = target_root / "conversation_summaries.db"

    if not apply:
        return {
            "status": "dry_run",
            "omp_id": omp_id,
            "conversation_id": conv_id,
            "title": title,
            "step_count": len(msgs),
            "target_db": ctxbridge.scrub_paths(str(conv_db_path)),
        }

    conv_db_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    annot_path.parent.mkdir(parents=True, exist_ok=True)

    now_sec = int(time.time())
    created_sec = now_sec - max(10, len(msgs) * 2)
    client_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ctxbridge-client:{omp_id}"))
    traj_meta_pb = _build_trajectory_metadata(created_sec, client_uuid, conv_id, "outside-of-project")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f+00:00")
    last_user_idx = 0
    last_user_sec = created_sec

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
        for idx, (role, text) in enumerate(msgs):
            step_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{conv_id}:step:{idx}"))
            ts = now_sec - max(0, (len(msgs) - idx) * 2)
            if role == "user":
                last_user_idx = idx
                last_user_sec = ts
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
                            "created_at": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
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
                            "created_at": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
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
        f"title:{json.dumps(title, ensure_ascii=False)} last_user_view_time:{{seconds:{now_sec} nanos:0}}",
        encoding="utf-8",
    )

    raw_summary_pb = _build_raw_summary(
        title=title,
        step_count=len(msgs),
        created_sec=created_sec,
        last_mod_sec=now_sec,
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
                "step_count": len(msgs),
                "last_modified_time": now_iso,
                "workspace_uris": "",
                "status": "CASCADE_RUN_STATUS_IDLE",
                "source": "",
                "project_id": "outside-of-project",
                "agent_name": "",
                "app_data_dir": "antigravity",
                "last_user_input_time": now_iso,
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
        "step_count": len(msgs),
        "summary_db_updated": summary_updated,
        "target_db": ctxbridge.scrub_paths(str(conv_db_path)),
    }
    if summary_warning:
        out["summary_warning"] = summary_warning
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Import OMP session into Antigravity Conversations DB")
    parser.add_argument("--id", default="latest", help="OMP session ID prefix or 'latest'")
    parser.add_argument("--max-messages", type=int, default=30, help="Max recent messages to import")
    parser.add_argument("--apply", action="store_true", help="Write DB and summary row (default: dry-run)")
    args = parser.parse_args()

    res = import_omp_to_agy(ident=args.id, max_messages=args.max_messages, apply=args.apply)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
