#!/usr/bin/env python3
"""Two-way native conversation importer between Oh My Pi (omp) and Google Antigravity.

- OMP -> Antigravity: maps messages chronologically (step_type=14/15), converts OMP
  compactions/branch summaries into native Antigravity CHECKPOINT steps (step_type=23),
  merges consecutive assistant fragments, extracts OMP blob/inline images into
  brain/<id>/omp_media/ (USER_INPUT Media field 9 + Markdown embeds), and writes
  protobuf conversation_summaries rows.
- Antigravity -> OMP: converts Antigravity transcripts (USER_INPUT, PLANNER_RESPONSE,
  CHECKPOINT) into native OMP v3 .jsonl sessions, stores local images into
  ~/.omp/agent/blobs/<sha256>, and updates history.db titles/recaps.

Pure Python 3.12 standard library only.
"""

from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
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
MAX_INLINE_PB_IMAGE_BYTES = 512 * 1024

MIME_TO_EXT = {
    "image/png": ".png",
    "image/webp": ".webp",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
}
EXT_TO_MIME = {
    ".png": "image/png",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
}
MD_IMG_RX = re.compile(r"!\[[^\]]*\]\(([^)\r\n]+)\)")


# ---------------------------------------------------------------- image helpers

def _ext_for_mime(mime: str | None) -> str:
    m = (mime or "").split(";")[0].strip().lower()
    return MIME_TO_EXT.get(m, ".png")


def _mime_for_path(path: Path) -> str:
    return EXT_TO_MIME.get(path.suffix.lower(), "image/png")


def _resolve_omp_image(block: dict, omp_agent_root: Path) -> tuple[bytes, str] | None:
    """Resolve an OMP image content block (blob:sha256:<hex> or inline base64)."""
    data = block.get("data")
    if not isinstance(data, str) or not data.strip():
        return None
    mime = str(block.get("mimeType") or block.get("mime_type") or "image/png").split(";")[0].strip() or "image/png"
    s = data.strip()
    if s.startswith("blob:sha256:"):
        digest = s[len("blob:sha256:"):].strip()
        if not digest or "/" in digest or "\\" in digest:
            return None
        blobs_dir = omp_agent_root / "blobs"
        ext = _ext_for_mime(mime)
        candidates = [blobs_dir / f"{digest}{ext}"]
        for alt_ext in (".png", ".webp", ".jpg", ".jpeg", ".gif", ""):
            cand = blobs_dir / f"{digest}{alt_ext}"
            if cand not in candidates:
                candidates.append(cand)
        for cand in candidates:
            if cand.is_file():
                try:
                    raw = cand.read_bytes()
                    if raw:
                        detected_mime = _mime_for_path(cand) if cand.suffix else mime
                        return raw, detected_mime
                except OSError:
                    return None
        return None

    # Inline base64 or data URL
    if s.startswith("data:") and ";base64," in s:
        header, s = s.split(";base64,", 1)
        if header.startswith("data:") and len(header) > 5:
            mime = header[5:].strip() or mime
    try:
        raw = base64.b64decode(s, validate=False)
        return (raw, mime) if raw else None
    except Exception:
        return None


def _store_omp_blob(raw: bytes, mime: str, omp_agent_root: Path) -> str:
    """Write image bytes into ~/.omp/agent/blobs and return blob:sha256:<hex>."""
    digest = hashlib.sha256(raw).hexdigest()
    blobs_dir = omp_agent_root / "blobs"
    blobs_dir.mkdir(parents=True, exist_ok=True)
    ext = _ext_for_mime(mime)
    for fname in (digest, f"{digest}{ext}"):
        target = blobs_dir / fname
        if not target.exists():
            target.write_bytes(raw)
    return f"blob:sha256:{digest}"


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


def _build_user_payload(
    meta_pb: bytes,
    text: str,
    item_uuid: str,
    media_items: list[tuple[str, str, str, bytes]] | None = None,
) -> bytes:
    inner_19 = bytearray()
    inner_19 += _pb_str(2, text)
    inner_19 += _pb_bytes(3, _pb_str(1, text))
    for mime_type, filename, posix_path, raw_bytes in media_items or ():
        media_pb = bytearray()
        media_pb += _pb_str(1, mime_type)
        media_pb += _pb_str(4, filename)
        media_pb += _pb_str(5, posix_path)
        if raw_bytes and len(raw_bytes) <= MAX_INLINE_PB_IMAGE_BYTES:
            media_pb += _pb_bytes(6, raw_bytes)
        inner_19 += _pb_bytes(9, bytes(media_pb))
    inner_19 += _pb_str(19, item_uuid)
    return (
        _pb_varint(1, 14)
        + _pb_varint(4, 3)
        + _pb_bytes(5, meta_pb)
        + _pb_bytes(19, bytes(inner_19))
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
    inner_30 += _pb_varint(1, 2)
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


# ---------------------------------------------------------------- OMP -> AGY parser & importer

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


def _extract_images_from_content(content, omp_agent_root: Path) -> list[tuple[bytes, str]]:
    if not isinstance(content, list):
        return []
    out: list[tuple[bytes, str]] = []
    for blk in content:
        if isinstance(blk, dict) and blk.get("type") == "image":
            resolved = _resolve_omp_image(blk, omp_agent_root)
            if resolved is not None:
                out.append(resolved)
    return out


def _parse_omp_timeline(
    path: Path,
    db_titles: dict[str, str] | None = None,
    omp_agent_root: Path | None = None,
) -> tuple[dict, list[tuple[str, int, str, list[tuple[bytes, str]]]]]:
    """Read an OMP session JSONL chronologically.

    Returns (meta_dict, timeline) where timeline items are
    (kind, epoch_sec, redacted_text, images) with kind in {'user', 'assistant', 'checkpoint'}.
    """
    omp_agent_root = omp_agent_root or ctxbridge.omp_root()
    mtime = int(path.stat().st_mtime)
    sid = path.stem.split("_")[-1]
    cwd = ""
    title = ""
    created_sec: int | None = None
    timeline: list[tuple[str, int, str, list[tuple[bytes, str]]]] = []

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
                timeline.append(("checkpoint", ts, red, []))
        elif typ == "message":
            msg = obj.get("message") or {}
            role = msg.get("role")
            msg_ts = _parse_iso_epoch(msg.get("timestamp") or obj.get("timestamp"), ts)
            if role == "toolResult":
                imgs = _extract_images_from_content(msg.get("content"), omp_agent_root)
                if imgs:
                    if timeline and timeline[-1][0] == "assistant":
                        pk, _pts, pt, pimgs = timeline[-1]
                        timeline[-1] = (pk, msg_ts, pt, pimgs + imgs)
                    else:
                        timeline.append(("assistant", msg_ts, "", imgs))
                continue
            if role not in ("user", "assistant"):
                continue
            raw_text = ctxbridge.omp_text(msg.get("content")).strip()
            imgs = _extract_images_from_content(msg.get("content"), omp_agent_root)
            if not raw_text and not imgs:
                continue
            red = _cap_text(ctxbridge.redact(raw_text)) if raw_text else ""
            if role == "user":
                timeline.append(("user", msg_ts, red or "[Image]", imgs))
            else:
                if timeline and timeline[-1][0] == "assistant":
                    pk, _pts, pt, pimgs = timeline[-1]
                    joined = "\n\n".join(x for x in (pt, red) if x)
                    timeline[-1] = (pk, msg_ts, _cap_text(joined), pimgs + imgs)
                else:
                    timeline.append(("assistant", msg_ts, red, imgs))

    if db_titles is None:
        db_titles = ctxbridge.omp_db_titles()
    title = db_titles.get(sid) or title

    if not timeline:
        recap = ctxbridge.omp_db_recap(sid)
        if isinstance(recap, str) and recap.strip():
            timeline.append(("checkpoint", mtime, _cap_text(ctxbridge.redact(recap.strip())), []))

    meta = {
        "id": sid,
        "cwd": cwd,
        "title": ctxbridge.redact(title or f"OMP {sid[:8]}"),
        "created_sec": created_sec if created_sec is not None else max(0, mtime - max(10, len(timeline) * 2)),
        "mtime": mtime,
    }
    return meta, timeline


def _trim_timeline(timeline: list, max_messages: int) -> list:
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


def import_omp_to_agy(
    ident: str = "latest",
    max_messages: int = 0,
    apply: bool = True,
    agy_dir: Path | None = None,
    _session_path: Path | None = None,
    _db_titles: dict[str, str] | None = None,
) -> dict:
    """Convert an OMP session into an Antigravity conversation DB + summary row."""
    omp_agent_root = ctxbridge.omp_root()
    session_path = _session_path if _session_path is not None else ctxbridge.find_omp_session(ident)
    meta, raw_timeline = _parse_omp_timeline(
        session_path, db_titles=_db_titles, omp_agent_root=omp_agent_root
    )
    timeline = _trim_timeline(raw_timeline, max_messages)

    omp_id = meta["id"]
    conv_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ctxbridge-omp:{omp_id}"))
    traj_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ctxbridge-traj:{omp_id}"))

    title_raw = (meta.get("title") or f"OMP {omp_id[:8]}").strip()
    title = f"[OMP] {title_raw}" if not title_raw.startswith("[OMP]") else title_raw

    if not timeline:
        timeline = [("user", meta["mtime"], f"[Imported OMP Session {omp_id}]", [])]

    target_root = Path(agy_dir) if agy_dir else ctxbridge.agy_root()
    conv_db_path = target_root / "conversations" / f"{conv_id}.db"
    brain_dir = target_root / "brain" / conv_id
    media_dir = brain_dir / "omp_media"
    transcript_path = brain_dir / ".system_generated" / "logs" / "transcript.jsonl"
    annot_path = target_root / "annotations" / f"{conv_id}.pbtxt"
    summaries_db_path = target_root / "conversation_summaries.db"

    checkpoints = sum(1 for item in timeline if item[0] == "checkpoint")
    user_turns = sum(1 for item in timeline if item[0] == "user")
    total_images = sum(len(item[3]) for item in timeline)

    if not apply:
        return {
            "status": "dry_run",
            "omp_id": omp_id,
            "conversation_id": conv_id,
            "title": title,
            "step_count": len(timeline),
            "user_turns": user_turns,
            "checkpoints": checkpoints,
            "images_copied": total_images,
            "target_db": ctxbridge.scrub_paths(str(conv_db_path)),
        }

    conv_db_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    annot_path.parent.mkdir(parents=True, exist_ok=True)
    if total_images > 0:
        media_dir.mkdir(parents=True, exist_ok=True)

    created_sec = int(meta["created_sec"])
    last_mod_sec = max(int(meta["mtime"]), max(item[1] for item in timeline))
    client_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ctxbridge-client:{omp_id}"))
    traj_meta_pb = _build_trajectory_metadata(created_sec, client_uuid, conv_id, "outside-of-project")
    brain_uri = brain_dir.as_uri()

    last_user_idx = 0
    last_user_sec = created_sec
    prior_user_prompts: list[str] = []
    images_copied = 0

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
        for idx, (kind, ts, text, imgs) in enumerate(timeline):
            step_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{conv_id}:step:{idx}"))
            iso_ts = datetime.fromtimestamp(ts, timezone.utc).isoformat()

            saved_imgs: list[tuple[str, str, str, bytes]] = []
            for img_i, (raw_bytes, mime) in enumerate(imgs, start=1):
                ext = _ext_for_mime(mime)
                fname = f"s{idx:04d}_{img_i:02d}{ext}"
                dst_img = media_dir / fname
                dst_img.write_bytes(raw_bytes)
                images_copied += 1
                saved_imgs.append((mime, fname, dst_img.as_posix(), raw_bytes))

            if kind == "user":
                last_user_idx = idx
                last_user_sec = ts
                prior_user_prompts.append(text)
                step_type = 14
                meta_pb = _build_metadata(ts, 4, step_uuid, traj_id, idx, conv_id)
                payload_pb = _build_user_payload(
                    meta_pb,
                    text,
                    str(uuid.uuid5(uuid.NAMESPACE_URL, f"{conv_id}:u:{idx}")),
                    media_items=saved_imgs,
                )
                t_rec: dict = {
                    "step_index": idx,
                    "source": "USER_EXPLICIT",
                    "type": "USER_INPUT",
                    "status": "DONE",
                    "created_at": iso_ts,
                    "content": text,
                }
                if saved_imgs:
                    t_rec["media"] = [{"mime_type": m, "uri": u} for m, _fn, u, _rb in saved_imgs]
                t_lines.append(json.dumps(t_rec, ensure_ascii=False))
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
                asst_text = text
                if saved_imgs:
                    md_refs = "\n".join(
                        f"![OMP image {i}]({u})"
                        for i, (_m, _fn, u, _rb) in enumerate(saved_imgs, start=1)
                    )
                    asst_text = f"{asst_text}\n\n{md_refs}".strip()
                meta_pb = _build_metadata(ts, 2, step_uuid, traj_id, idx, conv_id)
                payload_pb = _build_assistant_payload(meta_pb, asst_text)
                t_lines.append(
                    json.dumps(
                        {
                            "step_index": idx,
                            "source": "MODEL",
                            "type": "PLANNER_RESPONSE",
                            "status": "DONE",
                            "created_at": iso_ts,
                            "content": asst_text,
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
        "images_copied": images_copied,
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
    """Import multiple recent OMP sessions (skipping empty or [AGY]-prefixed ones)."""
    omp_agent_root = ctxbridge.omp_root()
    files = sorted(ctxbridge.omp_session_files(), key=lambda p: p.stat().st_mtime, reverse=True)
    db_titles = ctxbridge.omp_db_titles()

    imported: list[dict] = []
    skipped = 0

    for f in files:
        if not all_sessions and len(imported) >= limit:
            break
        meta, timeline = _parse_omp_timeline(f, db_titles=db_titles, omp_agent_root=omp_agent_root)
        if not timeline or str(meta.get("title") or "").startswith("[AGY]"):
            skipped += 1
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
        "skipped": skipped,
        "sessions": imported,
    }


# ---------------------------------------------------------------- AGY -> OMP importer

def _omp_cwd_slug(cwd: str) -> str:
    if not cwd:
        return "-tmp"
    low = cwd.replace("/", "\\").lower()
    if "\\appdata\\local\\temp" in low or low in ("/tmp", "tmp"):
        return "-tmp"
    slug = re.sub(r"[:\\/]+", "-", cwd.strip()).strip("-")
    return f"--{slug}--" if slug else "-tmp"


def _resolve_local_image_path(uri_or_path: str, brain_dir: Path) -> Path | None:
    s = (uri_or_path or "").strip()
    if not s or s.startswith(("http://", "https://", "data:")):
        return None
    p = ctxbridge.file_uri_to_path(s) if s.lower().startswith("file:") else Path(s)
    if p is None:
        return None
    if not p.is_absolute():
        p = brain_dir / p
    try:
        if p.is_file() and p.suffix.lower() in EXT_TO_MIME:
            return p
    except OSError:
        return None
    return None


def _parse_agy_timeline(
    cid: str, raw_summary_val, agy_root: Path
) -> list[tuple[str, int, str, list[tuple[bytes, str]]]]:
    """Read an Antigravity conversation transcript chronologically."""
    brain_dir = agy_root / "brain" / cid
    tpath = None
    logs_dir = brain_dir / ".system_generated" / "logs"
    for name in ("transcript_full.jsonl", "transcript.jsonl"):
        cand = logs_dir / name
        if cand.is_file():
            tpath = cand
            break

    now_sec = int(time.time())
    timeline: list[tuple[str, int, str, list[tuple[bytes, str]]]] = []

    if tpath is not None:
        for obj in ctxbridge.read_jsonl(tpath):
            if not isinstance(obj, dict):
                continue
            typ = obj.get("type")
            ts = _parse_iso_epoch(obj.get("created_at"), now_sec)
            raw_content = obj.get("content")
            text = ctxbridge.redact(ctxbridge.clean_agy(raw_content)) if isinstance(raw_content, str) else ""

            imgs: list[tuple[bytes, str]] = []
            seen_paths: set[str] = set()
            if isinstance(obj.get("media"), list):
                for m in obj["media"]:
                    if not isinstance(m, dict):
                        continue
                    lp = _resolve_local_image_path(str(m.get("uri") or ""), brain_dir)
                    if lp and str(lp) not in seen_paths:
                        seen_paths.add(str(lp))
                        try:
                            imgs.append((lp.read_bytes(), str(m.get("mime_type") or _mime_for_path(lp))))
                        except OSError:
                            pass
            if isinstance(raw_content, str):
                for m_ref in MD_IMG_RX.findall(raw_content):
                    lp = _resolve_local_image_path(m_ref, brain_dir)
                    if lp and str(lp) not in seen_paths:
                        seen_paths.add(str(lp))
                        try:
                            imgs.append((lp.read_bytes(), _mime_for_path(lp)))
                        except OSError:
                            pass

            if typ == "USER_INPUT":
                if text or imgs:
                    timeline.append(("user", ts, text or "[Image]", imgs))
            elif typ == "CHECKPOINT":
                if text:
                    timeline.append(("checkpoint", ts, text, []))
            elif typ == "PLANNER_RESPONSE":
                if text or imgs:
                    if timeline and timeline[-1][0] == "assistant":
                        pk, _pts, pt, pimgs = timeline[-1]
                        joined = "\n\n".join(x for x in (pt, text) if x)
                        timeline[-1] = (pk, ts, _cap_text(joined), pimgs + imgs)
                    else:
                        timeline.append(("assistant", ts, text, imgs))

    if not timeline and isinstance(raw_summary_val, str) and raw_summary_val.strip():
        timeline.append(("checkpoint", now_sec, _cap_text(ctxbridge.redact(raw_summary_val.strip())), []))

    return timeline


def import_agy_to_omp(
    ident: str = "latest",
    apply: bool = True,
    omp_dir: Path | None = None,
    agy_dir: Path | None = None,
) -> dict:
    """Convert an Antigravity conversation into a native OMP v3 .jsonl session + history.db entry."""
    old_agy = os.environ.get("CTXBRIDGE_AGY_DIR")
    if agy_dir is not None:
        os.environ["CTXBRIDGE_AGY_DIR"] = str(agy_dir)
    try:
        rows = ctxbridge.agy_rows()
    finally:
        if agy_dir is not None:
            if old_agy is None:
                os.environ.pop("CTXBRIDGE_AGY_DIR", None)
            else:
                os.environ["CTXBRIDGE_AGY_DIR"] = old_agy

    if not rows:
        raise ctxbridge.NotFound("antigravity konuşması bulunamadı")

    if ident == "latest":
        non_omp = [r for r in rows if not str(r[1] or "").startswith("[OMP]")]
        row = non_omp[0] if non_omp else rows[0]
    else:
        if len(ident) < 8:
            raise ctxbridge.CtxError("konuşma kimliği en az 8 karakter olmalı (ya da 'latest')")
        matches = [r for r in rows if str(r[0]).startswith(ident)]
        if not matches:
            raise ctxbridge.NotFound(f"antigravity konuşması bulunamadı: {ident}")
        if len(matches) > 1:
            raise ctxbridge.CtxError(f"kimlik belirsiz ({len(matches)} eşleşme): {ident}")
        row = matches[0]

    cid, raw_title, mtime_str, _steps, workspace_raw, raw_summary = row
    cid = str(cid)
    agy_root_path = Path(agy_dir) if agy_dir else ctxbridge.agy_root()
    omp_agent_root = Path(omp_dir) if omp_dir else ctxbridge.omp_root()

    timeline = _parse_agy_timeline(cid, raw_summary, agy_root_path)
    if not timeline:
        timeline = [("user", int(time.time()), f"[Imported Antigravity Session {cid}]", [])]

    title_clean = ctxbridge.redact(str(raw_title or f"AGY {cid[:8]}")).strip()
    title = f"[AGY] {title_clean}" if not title_clean.startswith("[AGY]") else title_clean

    parsed_ws = ctxbridge.parse_workspace_uris(workspace_raw)
    cwd_path = ctxbridge.file_uri_to_path(parsed_ws[0]) if parsed_ws else None
    cwd_str = str(cwd_path) if cwd_path else str(ctxbridge.user_home() / "AppData" / "Local" / "Temp")

    omp_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, f"ctxbridge-agy:{cid}"))
    slug = _omp_cwd_slug(cwd_str)
    first_ts = timeline[0][1]
    last_ts = timeline[-1][1]
    stamp_file = datetime.fromtimestamp(first_ts, timezone.utc).strftime("%Y-%m-%dT%H-%M-%S-000Z")
    session_dir = omp_agent_root / "sessions" / slug
    target_jsonl = session_dir / f"{stamp_file}_{omp_uuid}.jsonl"

    images_total = sum(len(item[3]) for item in timeline)
    if not apply:
        return {
            "status": "dry_run",
            "agy_id": cid,
            "omp_session_id": omp_uuid,
            "title": title,
            "entry_count": len(timeline),
            "images_copied": images_total,
            "target_file": ctxbridge.scrub_paths(str(target_jsonl)),
        }

    session_dir.mkdir(parents=True, exist_ok=True)
    for old_f in session_dir.glob(f"*_{omp_uuid}.jsonl"):
        if old_f != target_jsonl:
            old_f.unlink(missing_ok=True)

    now_iso = datetime.fromtimestamp(last_ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    created_iso = datetime.fromtimestamp(first_ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    lines_out: list[str] = [
        json.dumps(
            {
                "type": "title",
                "v": 1,
                "title": title,
                "source": "auto",
                "updatedAt": now_iso,
            },
            ensure_ascii=False,
        ),
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": omp_uuid,
                "timestamp": created_iso,
                "cwd": cwd_str,
                "title": title,
                "titleSource": "auto",
            },
            ensure_ascii=False,
        ),
    ]

    parent_id: str | None = None
    images_copied = 0
    recap_text = ""

    for idx, (kind, ts, text, imgs) in enumerate(timeline):
        entry_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{omp_uuid}:e:{idx}").hex[:8]
        iso_ts = datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

        if kind == "checkpoint":
            recap_text = text
            next_id = uuid.uuid5(uuid.NAMESPACE_URL, f"{omp_uuid}:e:{idx + 1}").hex[:8]
            entry = {
                "type": "compaction",
                "id": entry_id,
                "parentId": parent_id,
                "timestamp": iso_ts,
                "summary": text,
                "shortSummary": "Antigravity checkpoint",
                "firstKeptEntryId": next_id if idx + 1 < len(timeline) else entry_id,
            }
        else:
            content_blocks: list[dict] = []
            if text:
                content_blocks.append({"type": "text", "text": text})
            for raw_bytes, mime in imgs:
                blob_ref = _store_omp_blob(raw_bytes, mime, omp_agent_root)
                images_copied += 1
                content_blocks.append({"type": "image", "data": blob_ref, "mimeType": mime})
            msg_obj: dict = {
                "role": kind,
                "content": content_blocks,
                "timestamp": ts * 1000,
            }
            if kind == "user":
                msg_obj["attribution"] = "user"
            else:
                msg_obj["stopReason"] = "stop"
            entry = {
                "type": "message",
                "id": entry_id,
                "parentId": parent_id,
                "timestamp": iso_ts,
                "message": msg_obj,
            }

        lines_out.append(json.dumps(entry, ensure_ascii=False))
        parent_id = entry_id

    target_jsonl.write_text("\n".join(lines_out) + "\n", encoding="utf-8")

    # Update OMP history.db cleanly
    history_db_path = omp_agent_root / "history.db"
    history_updated = False
    history_warning = None
    try:
        hcon = sqlite3.connect(str(history_db_path), timeout=5.0)
        try:
            hcon.execute(
                "CREATE TABLE IF NOT EXISTS session_titles ("
                "session_id TEXT PRIMARY KEY, title TEXT NOT NULL, updated_at INTEGER NOT NULL DEFAULT 0)"
            )
            hcon.execute(
                "INSERT OR REPLACE INTO session_titles (session_id, title, updated_at) VALUES (?, ?, ?)",
                (omp_uuid, title, last_ts),
            )
            has_recaps = hcon.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='session_recaps'"
            ).fetchone()
            if has_recaps and recap_text:
                hcon.execute("DELETE FROM session_recaps WHERE session_id = ?", (omp_uuid,))
                hcon.execute(
                    "INSERT INTO session_recaps (session_id, cwd, recap, created_at) VALUES (?, ?, ?, ?)",
                    (omp_uuid, cwd_str, recap_text, last_ts),
                )
            hcon.commit()
            history_updated = True
        finally:
            hcon.close()
    except sqlite3.Error as exc:
        history_warning = str(exc)

    out = {
        "status": "imported",
        "agy_id": cid,
        "omp_session_id": omp_uuid,
        "title": title,
        "entry_count": len(timeline),
        "images_copied": images_copied,
        "history_db_updated": history_updated,
        "target_file": ctxbridge.scrub_paths(str(target_jsonl)),
    }
    if history_warning:
        out["history_warning"] = history_warning
    return out


def import_agy_batch(
    limit: int = 20,
    all_sessions: bool = False,
    apply: bool = True,
    omp_dir: Path | None = None,
    agy_dir: Path | None = None,
) -> dict:
    """Import Antigravity conversations into OMP (skipping [OMP]-prefixed and empty ones)."""
    old_agy = os.environ.get("CTXBRIDGE_AGY_DIR")
    if agy_dir is not None:
        os.environ["CTXBRIDGE_AGY_DIR"] = str(agy_dir)
    try:
        rows = ctxbridge.agy_rows()
    finally:
        if agy_dir is not None:
            if old_agy is None:
                os.environ.pop("CTXBRIDGE_AGY_DIR", None)
            else:
                os.environ["CTXBRIDGE_AGY_DIR"] = old_agy

    agy_root_path = Path(agy_dir) if agy_dir else ctxbridge.agy_root()
    imported: list[dict] = []
    skipped = 0

    for row in rows:
        if not all_sessions and len(imported) >= limit:
            break
        cid, raw_title, _mt, _st, _ws, raw_sum = row
        if str(raw_title or "").startswith("[OMP]"):
            skipped += 1
            continue
        timeline = _parse_agy_timeline(str(cid), raw_sum, agy_root_path)
        if not timeline:
            skipped += 1
            continue
        res = import_agy_to_omp(ident=str(cid), apply=apply, omp_dir=omp_dir, agy_dir=agy_dir)
        imported.append(res)

    return {
        "status": "imported" if apply else "dry_run",
        "imported_count": len(imported),
        "skipped": skipped,
        "sessions": imported,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Two-way native conversation importer (OMP <-> Antigravity)")
    parser.add_argument("--to", choices=["agy", "omp"], default="agy", help="Target harness: 'agy' or 'omp'")
    parser.add_argument("--id", default="latest", help="Source session/conversation ID prefix or 'latest'")
    parser.add_argument("--limit", type=int, default=0, help="Batch-import top N newest non-empty sessions")
    parser.add_argument("--all", action="store_true", help="Batch-import all non-empty sessions")
    parser.add_argument("--max-messages", type=int, default=0, help="Max recent messages for --to agy (0 = full)")
    parser.add_argument("--apply", action="store_true", help="Write DB/JSONL files (default: dry-run)")
    args = parser.parse_args()

    if args.to == "omp":
        if args.all or args.limit > 0:
            res = import_agy_batch(
                limit=max(1, args.limit) if args.limit > 0 else 20,
                all_sessions=args.all,
                apply=args.apply,
            )
        else:
            res = import_agy_to_omp(ident=args.id, apply=args.apply)
    else:
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
