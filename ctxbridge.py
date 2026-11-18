#!/usr/bin/env python3
"""ctxbridge: omp <-> Google Antigravity context bridge. Stdlib only.

Read-only against tool data dirs. Pack files are Markdown with a small
front matter block. See DESIGN.md and FORMATS.md.
"""
import argparse
import contextlib
import difflib
import functools
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit


def user_home() -> Path:
    """Kullanıcı ana dizini. Windows'ta USERPROFILE'a öncelik verilir (testte geçersiz kılınabilir)."""
    up = os.environ.get("USERPROFILE")
    return Path(up) if up else Path.home()


def omp_root() -> Path:
    env = os.environ.get("CTXBRIDGE_OMP_DIR")
    return Path(env) if env else user_home() / ".omp" / "agent"


AGY_OVERRIDE: Path | None = None  # GUI'den elle seçilen kök; CTXBRIDGE_AGY_DIR'den sonra gelir


def agy_root_candidates() -> list:
    """~/.gemini altındaki antigravity* dizinlerinden konuşma özet veritabanı içerenler."""
    try:
        found = sorted((user_home() / ".gemini").glob("antigravity*"))
    except OSError:
        return []
    return [p for p in found if (p / "conversation_summaries.db").is_file()]


def _db_mtime(root: Path) -> float:
    try:
        return (root / "conversation_summaries.db").stat().st_mtime
    except OSError:
        return 0.0


def agy_root_info() -> tuple:
    """(kök, kaynak). Kaynak: CTXBRIDGE_AGY_DIR | elle | otomatik (en yeni conversation_summaries.db'li kök)."""
    env = os.environ.get("CTXBRIDGE_AGY_DIR")
    if env:
        return Path(env), "CTXBRIDGE_AGY_DIR"
    if AGY_OVERRIDE is not None:
        return AGY_OVERRIDE, "elle"
    cands = agy_root_candidates()
    if cands:
        return max(cands, key=_db_mtime), "otomatik"
    return user_home() / ".gemini" / "antigravity", "otomatik"


def agy_root() -> Path:
    return agy_root_info()[0]


def agy_global_base() -> Path:
    """Antigravity genel bağlam klasörü: CTXBRIDGE_AGY_GLOBAL_DIR, yoksa agy kökünün üstü (~/.gemini)."""
    env = os.environ.get("CTXBRIDGE_AGY_GLOBAL_DIR")
    return Path(env) if env else agy_root().parent


def global_target(tool: str) -> Path:
    """Aracın genel bağlam dosyası: omp -> <omp_root>/AGENTS.md, agy -> <agy_global_base>/GEMINI.md."""
    if tool == "omp":
        return omp_root() / "AGENTS.md"
    return agy_global_base() / "GEMINI.md"


def set_agy_override(path: Path | None) -> None:
    global AGY_OVERRIDE
    AGY_OVERRIDE = path


def omp_sessions_dir() -> Path:
    return omp_root() / "sessions"


def omp_history_path() -> Path:
    return omp_root() / "history.db"


def agy_summaries_path() -> Path:
    return agy_root() / "conversation_summaries.db"


def agy_brain_dir() -> Path:
    return agy_root() / "brain"
BRIDGE_DIR = Path(__file__).resolve().parent
OUT_DIR = BRIDGE_DIR / "out"

SUMMARY_SHARE = 0.4
TRUNC_MARK = "…[kırpıldı]"
DEFAULT_BUDGET = 4000
MIN_BUDGET = 200


class CtxError(Exception):
    exit_code = 1


class NotFound(CtxError):
    exit_code = 2


class TargetExists(CtxError):
    exit_code = 3


# ---------------------------------------------------------------- redaction

REDACTIONS = [
    ("pem", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S)),
    ("openai_key", re.compile(r"sk-[A-Za-z0-9_\-]{16,}")),
    ("google_key", re.compile(r"AIza[0-9A-Za-z_\-]{30,}")),
    ("github_token", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("github_pat", re.compile(r"github_pat_[A-Za-z0-9_]{20,}")),
    ("bearer", re.compile(r"Bearer\s+[A-Za-z0-9._~+/\-]{16,}=*", re.I)),
    ("assignment", re.compile(r"((?:password|passwd|token|api[_-]?key|secret)\s*[=:]\s*)(\S+)", re.I)),
]


@functools.lru_cache(maxsize=8)
def _home_rx(home_posix: str) -> re.Pattern:
    return re.compile(
        r"(?:file:///?)?" + re.escape(home_posix).replace("/", r"[\\/]") + r"(?![A-Za-z0-9_.\-])",
        re.I,
    )


GENERIC_HOME_RX = re.compile(r"(?:[A-Za-z]:[\\/]+Users[\\/]+|/Users/|/home/)[^\\/\s\"'<>|*?]+", re.I)


def scrub_paths(text: str) -> str:
    """Masks the current user's home as ~ and any other user's home as <home>."""
    text = _home_rx(user_home().as_posix()).sub("~", text)
    return GENERIC_HOME_RX.sub("<home>", text)


def redact(text: str) -> str:
    text = scrub_paths(text)
    for kind, rx in REDACTIONS:
        if kind == "assignment":
            text = rx.sub(lambda m: m.group(1) + "[REDACTED:assignment]", text)
        else:
            text = rx.sub(f"[REDACTED:{kind}]", text)
    return text


# ---------------------------------------------------------------- budgeting

def est_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def truncate_tokens(text: str, tokens: int) -> str:
    cap = max(0, tokens * 4)
    if len(text) <= cap:
        return text
    keep = max(0, cap - len(TRUNC_MARK))
    return text[:keep] + TRUNC_MARK


def render_front(meta: dict) -> str:
    lines = ["---"]
    for key, value in meta.items():
        if isinstance(value, str):
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def render_pack(meta: dict, summary, messages, budget: int) -> str:
    """messages: chronological list of (role, redacted text)."""
    summary = redact(summary or "").strip()
    summary = truncate_tokens(summary, int(budget * SUMMARY_SHARE)) if summary else ""
    summary_block = f"## Özet\n{summary}\n\n" if summary else ""
    head = render_front(meta) + "\n" + summary_block + "## Son konuşma\n"
    remaining = budget - est_tokens(head)
    kept = []
    for role, text in reversed(messages):
        line = f"- [{role}] {text}".rstrip()
        cost = est_tokens(line) + 1
        if cost <= remaining:
            kept.append(line)
            remaining -= cost
            continue
        if not kept and remaining > 1:
            kept.append(truncate_tokens(line, remaining - 1))
        break
    kept.reverse()
    body = "\n".join(kept) if kept else "(sığan mesaj yok)"
    return head + body + "\n"


# ---------------------------------------------------------------- sqlite (read-only)

@contextlib.contextmanager
def ro_db(path: Path):
    """Open a SQLite DB read-only via a temp copy (WAL-safe). Never touches the original."""
    if not path.is_file():
        raise NotFound(f"veritabanı yok: {path}")
    with tempfile.TemporaryDirectory(prefix="ctxbridge_") as tmp:
        dst = Path(tmp) / path.name
        try:
            for suffix in ("", "-wal", "-shm"):
                src = Path(str(path) + suffix)
                if src.exists():
                    shutil.copy2(src, Path(str(dst) + suffix))
        except OSError as exc:
            raise CtxError(f"veritabanı kopyalanamadı ({path.name}): {exc}") from exc
        try:
            con = sqlite3.connect(f"file:{dst.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            raise CtxError(f"veritabanı açılamadı ({path.name}): {exc}") from exc
        try:
            yield con
        except sqlite3.DatabaseError as exc:
            raise CtxError(f"veritabanı okunamadı ({path.name}): {exc}") from exc
        finally:
            con.close()


# ---------------------------------------------------------------- omp

def read_jsonl(path: Path, max_lines=None):
    with open(path, encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh):
            if max_lines is not None and i >= max_lines:
                break
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def omp_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(p for p in parts if p)
    return ""


def omp_session_files():
    sessions = omp_sessions_dir()
    if not sessions.is_dir():
        raise NotFound(f"omp oturum dizini yok: {sessions}")
    return list(sessions.glob("*/*.jsonl"))


def omp_db_titles() -> dict:
    if not omp_history_path().is_file():
        return {}
    with ro_db(omp_history_path()) as con:
        rows = con.execute("SELECT session_id, title FROM session_titles").fetchall()
    return {sid: title for sid, title in rows}


def omp_db_recap(sid: str):
    if not omp_history_path().is_file():
        return None
    with ro_db(omp_history_path()) as con:
        row = con.execute(
            "SELECT recap FROM session_recaps WHERE session_id = ? ORDER BY created_at DESC LIMIT 1",
            (sid,),
        ).fetchone()
    return row[0] if row else None


def omp_header(path: Path):
    sid, cwd, title = None, "", ""
    for o in read_jsonl(path, max_lines=40):
        t = o.get("type")
        if t == "session":
            sid = o.get("id")
            cwd = o.get("cwd") or ""
        elif t in ("title", "title_change") and o.get("title"):
            title = o["title"]
        if sid and title:
            break
    return sid or path.stem, cwd, title


def omp_list_rows(limit: int) -> list:
    titles = omp_db_titles()
    rows = []
    for f in omp_session_files():
        sid, cwd, title = omp_header(f)
        rows.append((f.stat().st_mtime, sid, cwd, titles.get(sid) or title))
    rows.sort(reverse=True)
    out = []
    for mtime, sid, cwd, title in rows[:limit]:
        when = datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
        out.append((when, sid, cwd, redact(title or "")))
    return out


def cmd_list_omp(limit: int) -> None:
    for when, sid, cwd, title in omp_list_rows(limit):
        print(f"{when}  {sid}  {cwd}  {title}")


def find_omp_session(ident: str) -> Path:
    files = omp_session_files()
    if not files:
        raise NotFound("omp oturumu bulunamadı")
    if ident == "latest":
        return max(files, key=lambda p: p.stat().st_mtime)
    if len(ident) < 8:
        raise CtxError("oturum kimliği en az 8 karakter olmalı (ya da 'latest')")
    matches = []
    for f in files:
        sid, _, _ = omp_header(f)
        if sid.startswith(ident) or f.stem.split("_")[-1].startswith(ident):
            matches.append(f)
    if not matches:
        raise NotFound(f"omp oturumu bulunamadı: {ident}")
    if len(matches) > 1:
        raise CtxError(f"kimlik belirsiz ({len(matches)} eşleşme): {ident}")
    return matches[0]


def export_omp(ident: str, budget: int) -> tuple:
    path = find_omp_session(ident)
    sid, cwd, title = omp_header(path)
    summary = None
    messages = []
    for o in read_jsonl(path):
        t = o.get("type")
        if t == "compaction" and isinstance(o.get("summary"), str):
            summary = o["summary"]
        elif t == "message":
            m = o.get("message") or {}
            role = m.get("role")
            if role in ("user", "assistant"):
                text = omp_text(m.get("content")).strip()
                if text:
                    messages.append((role, redact(text)))
    if summary is None:
        summary = omp_db_recap(sid)
    titles = omp_db_titles()
    title = titles.get(sid) or title
    meta = {
        "ctxbridge": "1",
        "source": "omp",
        "id": sid,
        "title": redact(title or ""),
        "project": cwd,
        "updated": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
        "budget_tokens": budget,
    }
    return meta, summary, messages


# ---------------------------------------------------------------- antigravity

def agy_rows(limit=None):
    if not agy_summaries_path().is_file():
        raise NotFound(f"antigravity özet veritabanı yok: {agy_summaries_path()}")
    sql = (
        "SELECT conversation_id, title, last_modified_time, step_count, workspace_uris, raw_summary "
        "FROM conversation_summaries ORDER BY last_modified_time DESC"
    )
    with ro_db(agy_summaries_path()) as con:
        rows = con.execute(sql).fetchall()
    return rows[:limit] if limit is not None else rows


def agy_list_rows(limit=None) -> list:
    out = []
    for cid, title, mtime, steps, workspace, _ in agy_rows(limit):
        out.append((str(mtime), str(cid), steps, redact(str(workspace or "")), redact(str(title or ""))))
    return out


def cmd_list_agy(limit: int) -> None:
    for mtime, cid, steps, workspace, title in agy_list_rows(limit):
        print(f"{mtime}  {cid}  steps={steps}  {workspace}  {title}")


def agy_transcript(cid: str):
    logs = agy_brain_dir() / cid / ".system_generated" / "logs"
    for name in ("transcript_full.jsonl", "transcript.jsonl"):
        p = logs / name
        if p.is_file():
            return p
    return None


AGY_NOISE = re.compile(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", re.S)
AGY_WRAP = re.compile(r"</?USER_REQUEST>")


def clean_agy(text: str) -> str:
    text = AGY_NOISE.sub("", text)
    text = AGY_WRAP.sub("", text)
    return text.strip()


def export_agy(ident: str, budget: int) -> tuple:
    rows = agy_rows()
    if not rows:
        raise NotFound("antigravity konuşması bulunamadı")
    if ident == "latest":
        row = rows[0]
    else:
        if len(ident) < 8:
            raise CtxError("konuşma kimliği en az 8 karakter olmalı (ya da 'latest')")
        matches = [r for r in rows if str(r[0]).startswith(ident)]
        if not matches:
            raise NotFound(f"antigravity konuşması bulunamadı: {ident}")
        if len(matches) > 1:
            raise CtxError(f"kimlik belirsiz ({len(matches)} eşleşme): {ident}")
        row = matches[0]
    cid, title, mtime, _steps, workspace, raw_summary = row
    summary = raw_summary if isinstance(raw_summary, str) else None
    messages = []
    checkpoint = None
    tpath = agy_transcript(cid)
    if tpath is not None:
        for o in read_jsonl(tpath):
            typ = o.get("type")
            content = o.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            if typ == "USER_INPUT":
                messages.append(("user", redact(clean_agy(content))))
            elif typ == "PLANNER_RESPONSE":
                messages.append(("assistant", redact(clean_agy(content))))
            elif typ == "CHECKPOINT":
                checkpoint = content
    if summary is None:
        summary = checkpoint
    meta = {
        "ctxbridge": "1",
        "source": "agy",
        "id": str(cid),
        "title": redact(str(title or "")),
        "project": str(workspace or ""),
        "updated": str(mtime),
        "budget_tokens": budget,
    }
    return meta, summary, messages

# ---------------------------------------------------------------- project detection

def file_uri_to_path(uri: str) -> Path | None:
    """file:/// URI'sini yerel yola çevirir (%xx kodları, sürücü harfi, UNC)."""
    parts = urlsplit(uri.strip())
    if parts.scheme.lower() != "file":
        return None
    path = unquote(parts.path)
    if re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    if parts.netloc and parts.netloc.lower() != "localhost":
        path = f"//{parts.netloc}{path}"
    return Path(path) if path else None


def parse_workspace_uris(raw) -> list:
    """workspace_uris alanı: JSON listesi ya da tek değer olabilir."""
    text = "" if raw is None else str(raw).strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return []
        return [u.strip() for u in data if isinstance(u, str) and u.strip()] if isinstance(data, list) else []
    return [text]


AGY_PATH_KEYS = ("Cwd", "TargetFile", "AbsolutePath", "Path", "DirectoryPath", "SearchDirectory", "SearchPath")
PROJECT_MARKERS = (".git", "package.json", "pyproject.toml", "Cargo.toml", "go.mod", "setup.py", "pom.xml", "build.gradle", "CMakeLists.txt", "Makefile")
SYSTEM_ENV = ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramData")
URI_RX = re.compile(r"""file:///[^\s"'<>\])]+""", re.I)
WIN_RX = re.compile(r"""(?<![A-Za-z])[A-Za-z]:[\\/][^\s"'<>\])*|`]+""")
ABS_RX = re.compile(r"""^([A-Za-z]:[\\/]|\\\\|/)""")


@dataclass
class Detection:
    dir: Path | None
    via: str  # workspace_uris | fts | izler(argüman) | izler(metin) | omp cwd


def abs_path(value) -> Path | None:
    """Yalnızca mutlak yol ya da file:/// URI'si kabul edilir; göreli ve kimlik benzeri değerler elenir."""
    text = "" if value is None else str(value).strip()
    if text.lower().startswith("file:"):
        return file_uri_to_path(text)
    return Path(text) if ABS_RX.match(text) else None


def noise_dir(p: Path) -> bool:
    """Ev dizini, sürücü kökü, sistem, agy/omp veri ve bağımlılık dizinleri proje sayılmaz."""
    home = user_home().resolve()
    if p.anchor == str(p) or p == home or "node_modules" in (x.lower() for x in p.parts):
        return True
    banned = [agy_root(), omp_root(), home / "AppData", home / ".gemini", home / ".omp"]
    banned += [Path(os.environ[k]) for k in SYSTEM_ENV if os.environ.get(k)]
    return any(p == b.resolve() or b.resolve() in p.parents for b in banned)


def project_dir(raw: Path) -> Path | None:
    """Yolu varolan dizine indirir; ev dizinini aşmadan en yakın .git ya da proje işaretçisini seçer, yoksa dizinin kendisi."""
    try:
        d = raw.resolve()
        if not d.is_dir():
            d = d.parent
        if not d.is_dir() or noise_dir(d):
            return None
        home = user_home().resolve()
        for cand in [d, *d.parents]:
            if cand == home or cand.anchor == str(cand):
                break
            if any((cand / m).exists() for m in PROJECT_MARKERS):
                return cand
        return d
    except OSError:
        return None


def best_project(paths) -> Path | None:
    """Adaylardan en sık görülen proje kökü; geçerli aday yoksa None."""
    roots = Counter(r for r in (project_dir(p) for p in paths) if r is not None)
    return roots.most_common(1)[0][0] if roots else None


def agy_row_for(ident: str):
    rows = agy_rows()
    if not rows:
        return None
    if ident == "latest":
        return rows[0]
    matches = [r for r in rows if str(r[0]).startswith(ident)]
    return matches[0] if len(matches) == 1 else None


def agy_fts_paths(cid: str) -> list:
    with ro_db(agy_summaries_path()) as con:
        if con.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'conversations_fts'").fetchone() is None:
            return []
        rows = con.execute(
            "SELECT workspace, project FROM conversations_fts WHERE conversation_id = ?", (cid,)
        ).fetchall()
    return [p for row in rows for p in map(abs_path, row) if p is not None]


def agy_arg_paths(cid: str) -> list:
    """Araç çağrılarının yapısal argümanlarındaki mutlak yollar (ör. Cwd)."""
    path = agy_transcript(cid)
    out = []
    for rec in (read_jsonl(path) if path else []):
        if not isinstance(rec, dict):
            continue
        calls = rec.get("tool_calls") or []
        for call in calls if isinstance(calls, list) else []:
            if not isinstance(call, dict):
                continue
            args = call.get("args", call.get("arguments"))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    continue
            if isinstance(args, dict):
                out.extend(p for p in map(abs_path, (args.get(k) for k in AGY_PATH_KEYS)) if p is not None)
    return out


def agy_text_paths(cid: str) -> list:
    """Transkript ve brain artefaktlarındaki file:/// URI'leri ile mutlak Windows yolları."""
    texts = []
    path = agy_transcript(cid)
    for rec in (read_jsonl(path) if path else []):
        if isinstance(rec, dict) and isinstance(rec.get("content"), str):
            texts.append(rec["content"])
    brain = agy_brain_dir() / cid
    if brain.is_dir():
        for f in brain.rglob("*.md"):
            try:
                texts.append(f.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
    blob = "\n".join(texts)
    out = [p for p in map(file_uri_to_path, URI_RX.findall(blob)) if p is not None]
    out += [Path(m.rstrip(".,;:")) for m in WIN_RX.findall(blob)]
    return out


def detect_agy(row) -> Detection:
    cid = str(row[0])
    steps = (
        ("workspace_uris", lambda: [p for p in map(abs_path, parse_workspace_uris(row[4])) if p is not None]),
        ("fts", lambda: agy_fts_paths(cid)),
        ("izler(argüman)", lambda: agy_arg_paths(cid)),
        ("izler(metin)", lambda: agy_text_paths(cid)),
    )
    for via, candidates in steps:
        found = best_project(candidates())
        if found is not None:
            return Detection(found, via)
    return Detection(None, "")


def detect_project(source: str, ident: str | None) -> Detection:
    """Oturumun çalışma dizini ve bulunma kaynağı. Bulunamazsa ya da okunamazsa Detection(None, '')."""
    empty = Detection(None, "")
    if not ident:
        return empty
    try:
        if source == "omp":
            cwd = omp_header(find_omp_session(ident))[1]
            return Detection(Path(cwd), "omp cwd") if cwd else empty
        if source == "agy":
            row = agy_row_for(ident)
            return detect_agy(row) if row is not None else empty
    except (CtxError, OSError, sqlite3.Error):
        return empty
    raise CtxError(f"bilinmeyen kaynak: {source}")


def as_dir(value) -> Path | None:
    """Boş olmayan değeri (yol ya da file URI) Path'e çevirir; boşsa None."""
    text = "" if value is None else str(value).strip()
    if not text:
        return None
    if text.lower().startswith("file:"):
        return file_uri_to_path(text)
    return Path(text)


@dataclass
class ProjectChoice:
    source: str  # workspace_uris | fts | izler(argüman) | izler(metin) | omp cwd | pack | elle
    dir: Path
    root: Path  # üstteki .git dizini; yoksa dir


def find_git_root(start: Path) -> Path | None:
    for p in [start, *start.parents]:
        if (p / ".git").exists():
            return p
    return None


def temp_roots() -> list[Path]:
    """Geçici klasör kökleri: tempfile, TEMP/TMP ve <ev>/AppData/Local/Temp."""
    names = [tempfile.gettempdir(), os.environ.get("TEMP"), os.environ.get("TMP"), str(user_home() / "AppData" / "Local" / "Temp")]
    return [Path(n).resolve() for n in names if n]


def project_problem(d: Path) -> str | None:
    """Otomatik kapsamda proje olarak kullanılamayan dizinin nedeni; uygunsa None."""
    if not d.is_dir():
        return "dizin yok"
    home = user_home().resolve()
    if d == home:
        return "ev dizini"
    if d in home.parents:
        return "ev dizininin üst dizini"
    if d.parent == d:
        return "sürücü kökü"
    if any(d == t or t in d.parents for t in temp_roots()):
        return "geçici klasör"
    return None


def project_candidates(meta: dict, project: str | None) -> list:
    """Sıra: elle verilen proje; yoksa pack kaynağının algılanan dizini, sonra pack 'project' alanı."""
    if project:
        return [("elle", Path(project))]
    cands = []
    kind = meta.get("source")
    if kind in ("omp", "agy"):
        det = detect_project(kind, meta.get("id"))
        if det.dir is not None:
            cands.append((det.via, det.dir))
    pk = as_dir(meta.get("project"))
    if pk is not None:
        cands.append(("pack", pk))
    return cands


def pick_project(cands: list, scope: str) -> tuple:
    """İlk uygun aday için (ProjectChoice, etiket). auto'da yok/ev/geçici adaylar atlanır; uygun yoksa (None, nedenler)."""
    notes = []
    for label, raw in cands:
        d = raw.resolve()
        problem = project_problem(d) if scope == "auto" else (None if d.is_dir() else "dizin yok")
        if problem:
            notes.append(f"{label}: {problem}")
            continue
        root = d if label == "elle" else (find_git_root(d) or d)
        if scope == "auto" and project_problem(root):
            root = d
        return ProjectChoice(label, d, root), label
    return None, "; ".join(notes) or "proje algılanamadı"


def other_tool(source: str) -> str:
    return "agy" if source == "omp" else "omp"


# ---------------------------------------------------------------- commands

def cmd_list(source: str, limit: int) -> None:
    if limit < 1:
        raise CtxError("--limit en az 1 olmalı")
    if source == "omp":
        cmd_list_omp(limit)
    else:
        cmd_list_agy(limit)


def export_text(source: str, ident: str, budget: int) -> str:
    if budget < MIN_BUDGET:
        raise CtxError(f"--budget-tokens en az {MIN_BUDGET} olmalı")
    if source == "omp":
        meta, summary, messages = export_omp(ident, budget)
    else:
        meta, summary, messages = export_agy(ident, budget)
    return redact(render_pack(meta, summary, messages, budget))


def cmd_export(source: str, ident: str, budget: int, out: str | None) -> None:
    pack = export_text(source, ident, budget)
    if out:
        Path(out).write_text(pack, encoding="utf-8", newline="\n")
        print(f"yazıldı: {out} (~{est_tokens(pack)} token)")
    else:
        sys.stdout.write(pack)


def split_front(text: str):
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n---", 1)
    if len(parts) != 2:
        return {}, text
    meta = {}
    for line in parts[0].splitlines()[1:]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if value.startswith('"'):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                pass
        meta[key.strip()] = value
    body = parts[1].split("\n", 1)[1] if "\n" in parts[1] else ""
    return meta, body


def safe_id(raw: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-")[:40]
    return cleaned or "pack"


@dataclass
class InjectPlan:
    target: Path
    mode: str
    new_content: str
    old_content: str | None
    fallback: Path
    pack_id: str
    diff: str
    scope: str = "project"
    reason: str = ""
    backup: Path | None = None

    @property
    def exists(self) -> bool:
        return self.old_content is not None


MODE_TEXT = {
    "create": "yeni dosya oluşturulacak",
    "append": "mevcut dosyanın sonuna ctxbridge bloğu eklenecek (mevcut içerik korunur)",
    "replace": "mevcut ctxbridge bloğu değiştirilecek (bloğun dışı aynı kalır)",
    "unchanged": "değişiklik yok",
    "fallback": "hedef zaten var; üzerine yazılmaz, yedek konuma yazılacak",
}
BLOCK_END = "<!-- ctxbridge:end -->"


def read_existing(path: Path) -> str | None:
    if not path.is_file():
        return None
    with open(path, "r", encoding="utf-8", newline="") as fh:
        return fh.read()


def block_begin(pid: str) -> str:
    return f"<!-- ctxbridge:begin id={pid} -->"


def agents_block(pid: str, body: str, nl: str) -> str:
    return block_begin(pid) + nl + body.rstrip("\n") + nl + BLOCK_END + nl


def merge_agents(old: str | None, pid: str, body: str) -> tuple[str, str]:
    """Returns (mode, new_content). Touches only the ctxbridge block with this id."""
    if old is None:
        return "create", agents_block(pid, body, "\n")
    nl = "\r\n" if "\r\n" in old else "\n"
    block = agents_block(pid, body, nl)
    if not old:
        return "append", block
    start = old.find(block_begin(pid))
    if start == -1:
        head = old if old.endswith("\n") else old + nl
        return "append", head + nl + block
    end_at = old.find(BLOCK_END, start)
    if end_at == -1:
        raise CtxError("AGENTS.md içinde kapanmamış ctxbridge bloğu var; elle düzeltin")
    end = end_at + len(BLOCK_END)
    if old.startswith(nl, end):
        end += len(nl)
    new = old[:start] + block + old[end:]
    return ("unchanged" if new == old else "replace"), new


def make_diff(old: str, new: str, name: str = "AGENTS.md") -> str:
    return "\n".join(
        difflib.unified_diff(old.splitlines(), new.splitlines(), f"{name} (mevcut)", f"{name} (yeni)", lineterm="")
    )


SCOPES = ("auto", "project", "global")


def plan_inject(source: str, pack: str, project: str | None = None, scope: str = "auto") -> InjectPlan:
    """scope: auto (geçerli proje varsa proje, yoksa global) | project | global."""
    if scope not in SCOPES:
        raise CtxError(f"geçersiz kapsam: {scope}")
    pack_path = Path(pack)
    if not pack_path.is_file():
        raise NotFound(f"pack yok: {pack}")
    meta, body = split_front(pack_path.read_text(encoding="utf-8"))
    if meta.get("ctxbridge") != "1":
        raise CtxError("geçersiz pack (ctxbridge: 1 front matter yok)")
    pid = safe_id(meta.get("id", "pack"))
    payload = redact(body)
    fallback = OUT_DIR / f"{source}-{pid}.md"
    choice, why = None, "elle: global"
    if scope != "global":
        choice, why = pick_project(project_candidates(meta, project), scope)
        if scope == "project" and choice is None:
            raise CtxError(f"proje klasörü bulunamadı: {why}")
    if choice is not None:
        target = choice.root / ".omp" / "AGENTS.md" if source == "omp" else choice.root / "AGENTS.md"
        kind, reason = "project", f"proje ({choice.source})"
    else:
        target = global_target(source)
        kind = "global"
        reason = why if scope == "global" else f"geçerli proje yok ({why}) -> global"
    old = read_existing(target)
    if choice is not None and source == "omp":
        if old is not None:
            return InjectPlan(target, "fallback", payload, old, fallback, pid, "", kind, reason)
        return InjectPlan(target, "create", payload, None, fallback, pid, make_diff("", payload, target.name), kind, reason)
    mode, new = merge_agents(old, pid, payload)
    diff = "" if mode == "unchanged" else make_diff(old or "", new, target.name)
    backup = backup_path(target) if mode in ("append", "replace") else None
    return InjectPlan(target, mode, new, old, fallback, pid, diff, kind, reason, backup)


def describe_plan(plan: InjectPlan) -> str:
    lines = [f"kapsam: {plan.scope} ({plan.reason})", f"hedef: {scrub_paths(str(plan.target))}", MODE_TEXT[plan.mode]]
    if plan.mode == "fallback":
        lines.append(f"yedek konum: {scrub_paths(str(plan.fallback))}")
    if plan.backup is not None:
        lines.append(f"yedek (yazılırsa): {scrub_paths(str(plan.backup))}")
    if plan.diff:
        lines += ["önizleme (diff):", plan.diff]
    return "\n".join(lines)


def backup_path(target: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    return OUT_DIR / f"{target.name}.bak-{stamp}"


def apply_inject(plan: InjectPlan) -> str:
    """Hedef dosya dışındaki bayt yazılmaz; her değişiklik öncesi tam yedek alınır."""
    if plan.mode == "fallback":
        write_new(plan.fallback, plan.new_content)
        return f"yazıldı (yedek): {plan.fallback}"
    if plan.mode == "unchanged":
        return f"değişiklik yok: {plan.target}"
    if plan.mode == "create":
        try:
            write_new(plan.target, plan.new_content)
        except FileExistsError as exc:
            raise TargetExists(f"hedef oluşturulurken çakıştı: {plan.target}") from exc
        return f"yazıldı: {plan.target}"
    if read_existing(plan.target) != plan.old_content:
        raise TargetExists(f"dosya önizlemeden sonra değişti, yeniden önizleyin: {plan.target}")
    write_new(plan.backup, plan.old_content)
    with open(plan.target, "w", encoding="utf-8", newline="") as fh:
        fh.write(plan.new_content)
    return f"güncellendi: {plan.target} (yedek: {plan.backup})"


def cmd_inject(source: str, pack: str, project: str | None, apply: bool, scope: str = "auto") -> None:
    plan = plan_inject(source, pack, project, scope)
    print(describe_plan(plan))
    if not apply:
        print("kuru çalıştırma; yazmak için --apply ekleyin")
        return
    print(apply_inject(plan))


def write_new(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8", newline="\n") as fh:
        fh.write(payload)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ctxbridge", description="omp <-> Antigravity bağlam köprüsü")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="son oturumları listele (salt okuma)")
    pl.add_argument("source", choices=["omp", "agy"])
    pl.add_argument("--limit", type=int, default=20)

    pe = sub.add_parser("export", help="oturumu pack (.md) olarak dışa aktar")
    pe.add_argument("source", choices=["omp", "agy"])
    pe.add_argument("ident", help="oturum/konuşma kimliği (en az 8 karakter) veya 'latest'")
    pe.add_argument("--budget-tokens", type=int, default=DEFAULT_BUDGET)
    pe.add_argument("-o", "--out", default=None)

    pi = sub.add_parser("inject", help="pack'i proje yükleme konumuna yaz (varsayılan: kuru çalıştırma)")
    pi.add_argument("source", choices=["omp", "agy"])
    pi.add_argument("pack")
    pi.add_argument("--project", default=None)
    pi.add_argument("--apply", action="store_true")
    pi.add_argument("--scope", choices=SCOPES, default="auto",
                    help="auto: geçerli proje varsa proje, yoksa global; project; global")
    return p


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "list":
            cmd_list(args.source, args.limit)
        elif args.cmd == "export":
            cmd_export(args.source, args.ident, args.budget_tokens, args.out)
        elif args.cmd == "inject":
            cmd_inject(args.source, args.pack, args.project, args.apply, args.scope)
    except CtxError as exc:
        print(f"hata: {exc}", file=sys.stderr)
        return exc.exit_code
    except OSError as exc:
        print(f"hata: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
