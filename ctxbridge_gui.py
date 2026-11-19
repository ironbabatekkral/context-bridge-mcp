#!/usr/bin/env python3
"""ctxbridge GUI (tkinter, stdlib).

Provides a clean 3-tab interface over ctxbridge.py and ctxbridge_import.py:
  1. Aktarım & Ayarlar (two-way OMP <-> Antigravity sync, persistent settings, safe copy delete)
  2. MCP & Durum (one-click MCP wiring for Antigravity & OMP + live stdio test)
  3. Klasik Export / Inject (original pack export and rule-block inject)

Usage: python ctxbridge_gui.py            # open window
       python ctxbridge_gui.py --selftest # build window, load list, print count, exit
"""
import argparse
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import ctxbridge as cb
import ctxbridge_import as cbi

SOURCES = ("omp", "agy")
POLL_MS = 60
LIMIT_MIN, LIMIT_MAX = 1, 500
AGY_SRC_TEXT = {"otomatik": "otomatik", "elle": "elle", "CTXBRIDGE_AGY_DIR": "ortam değişkeni"}

DEFAULT_SETTINGS = {
    "direction": "omp_to_agy",
    "scope": "batch",
    "batch_limit": 20,
    "max_messages": 0,
    "include_images": True,
    "include_checkpoints": True,
    "skip_imported": True,
    "confirm_delete": True,
}


def settings_path() -> Path:
    return cb.user_home() / ".ctxbridge" / "settings.json"


def load_settings() -> dict:
    cfg = dict(DEFAULT_SETTINGS)
    p = settings_path()
    if p.is_file():
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update({k: raw[k] for k in DEFAULT_SETTINGS if k in raw})
        except (OSError, json.JSONDecodeError):
            pass
    return cfg


def save_settings(cfg: dict) -> Path:
    p = settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def fetch_rows(source: str, limit: int) -> list:
    """Uniform rows: (date, id, project, title)."""
    if source == "omp":
        return cb.omp_list_rows(limit)
    return [(m, c, w, t) for m, c, _steps, w, t in cb.agy_list_rows(limit)]


def err_text(exc: Exception) -> str:
    if isinstance(exc, cb.CtxError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


class AutoSelect:
    """Proje alanı: satır seçilince otomatik dolar; elle seçim, bir sonraki satır seçilene kadar korunur."""

    def __init__(self):
        self.manual = False

    def on_row(self):
        self.manual = False

    def on_pick(self):
        self.manual = True

    def fill_allowed(self) -> bool:
        return not self.manual


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.loaded = False
        self.last_error: str | None = None
        self.pack_text: str | None = None
        self.auto = AutoSelect()
        self.cfg = load_settings()

        root.title("context-bridge-mcp")
        root.geometry("1040x760")
        self._build()
        self.refresh()
        self.refresh_mcp_status()

    # ------------------------------------------------------------ layout
    def _build(self):
        top = ttk.Frame(self.root, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text="Kaynak:").pack(side="left")
        self.source_var = tk.StringVar(value="omp")
        src = ttk.Combobox(top, textvariable=self.source_var, values=SOURCES, width=6, state="readonly")
        src.pack(side="left", padx=4)
        src.bind("<<ComboboxSelected>>", lambda _e: self.refresh())

        ttk.Label(top, text="Limit:").pack(side="left", padx=(12, 0))
        self.limit_var = tk.StringVar(value="50")
        ttk.Spinbox(top, from_=LIMIT_MIN, to=LIMIT_MAX, textvariable=self.limit_var, width=6).pack(side="left", padx=4)
        ttk.Button(top, text="Listeyi yenile", command=lambda: self._guard(self.refresh)).pack(side="left", padx=4)

        ttk.Label(top, text="Antigravity kökü:").pack(side="left", padx=(12, 0))
        self.agy_root_var = tk.StringVar(value="")
        self.agy_combo = ttk.Combobox(top, textvariable=self.agy_root_var, width=46, state="readonly")
        self.agy_combo.pack(side="left", padx=4)
        self.agy_combo.bind("<<ComboboxSelected>>", lambda _e: self._guard(self.pick_agy_root))
        self.agy_src, self.agy_path, self.agy_paths = "", Path(), [None]

        # Shared sessions list
        mid = ttk.Frame(self.root, padding=(6, 0))
        mid.pack(fill="both", expand=True)
        cols = ("date", "id", "project", "title")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=9, selectmode="browse")
        for col, text, width in (
            ("date", "Tarih", 150),
            ("id", "Kimlik", 280),
            ("project", "Proje / Çalışma alanı", 220),
            ("title", "Başlık", 340),
        ):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._guard(self.on_select))

        # Tabs
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=6, pady=6)

        self._build_sync_tab()
        self._build_mcp_tab()
        self._build_classic_tab()

        self.status_var = tk.StringVar(value="Hazır")
        self.status_lbl = tk.Label(self.root, textvariable=self.status_var, anchor="w", padx=6, pady=3)
        self.status_lbl.pack(fill="x", side="bottom")

    def _build_sync_tab(self):
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="Aktarım & Ayarlar")

        # Direction & Scope
        row1 = ttk.LabelFrame(tab, text="Yön ve Kapsam", padding=6)
        row1.pack(fill="x", pady=(0, 6))

        self.dir_var = tk.StringVar(value=self.cfg["direction"])
        ttk.Radiobutton(row1, text="OMP → Antigravity", value="omp_to_agy", variable=self.dir_var).pack(side="left", padx=(0, 12))
        ttk.Radiobutton(row1, text="Antigravity → OMP", value="agy_to_omp", variable=self.dir_var).pack(side="left", padx=(0, 18))

        self.sync_scope_var = tk.StringVar(value=self.cfg["scope"])
        ttk.Radiobutton(row1, text="Seçili oturum", value="selected", variable=self.sync_scope_var).pack(side="left", padx=(0, 8))
        ttk.Radiobutton(row1, text="Son N:", value="batch", variable=self.sync_scope_var).pack(side="left")
        self.batch_limit_var = tk.StringVar(value=str(self.cfg["batch_limit"]))
        ttk.Spinbox(row1, from_=1, to=500, textvariable=self.batch_limit_var, width=5).pack(side="left", padx=4)
        ttk.Radiobutton(row1, text="Tümü", value="all", variable=self.sync_scope_var).pack(side="left", padx=(6, 0))

        # Settings
        cfg_box = ttk.LabelFrame(tab, text="Aktarım & Güvenlik Ayarları", padding=6)
        cfg_box.pack(fill="x", pady=(0, 6))

        self.inc_images_var = tk.BooleanVar(value=bool(self.cfg["include_images"]))
        self.inc_cp_var = tk.BooleanVar(value=bool(self.cfg["include_checkpoints"]))
        self.skip_imp_var = tk.BooleanVar(value=bool(self.cfg["skip_imported"]))
        self.confirm_del_var = tk.BooleanVar(value=bool(self.cfg["confirm_delete"]))
        self.max_msgs_var = tk.StringVar(value=str(self.cfg["max_messages"]))

        ttk.Checkbutton(cfg_box, text="Görselleri de kopyala (omp_media / blobs)", variable=self.inc_images_var).grid(row=0, column=0, sticky="w", padx=(0, 16))
        ttk.Checkbutton(cfg_box, text="Compaction / Checkpoint adımlarını koru", variable=self.inc_cp_var).grid(row=0, column=1, sticky="w", padx=(0, 16))
        ttk.Checkbutton(cfg_box, text="Döngüyü önle ([OMP]/[AGY] kopyaları atla)", variable=self.skip_imp_var).grid(row=1, column=0, sticky="w", padx=(0, 16), pady=(4, 0))
        ttk.Checkbutton(cfg_box, text="Kopya silerken onay penceresi göster", variable=self.confirm_del_var).grid(row=1, column=1, sticky="w", padx=(0, 16), pady=(4, 0))

        max_fr = ttk.Frame(cfg_box)
        max_fr.grid(row=0, column=2, rowspan=2, sticky="e")
        ttk.Label(max_fr, text="Maks. mesaj (0=tümü):").pack(side="left")
        ttk.Spinbox(max_fr, from_=0, to=5000, textvariable=self.max_msgs_var, width=6).pack(side="left", padx=4)
        cfg_box.columnconfigure(2, weight=1)

        note = ttk.Label(
            tab,
            text="Bilgi: Aktarılan konuşmalar bağımsız kopyadır — bir taraftan silince diğer taraftaki orijinal oturum silinmez. Secret/yol maskeleme her zaman aktiftir.",
            foreground="#444444",
        )
        note.pack(anchor="w", pady=(0, 6))

        # Action buttons
        btns = ttk.Frame(tab)
        btns.pack(fill="x", pady=(0, 6))
        ttk.Button(btns, text="Önizle (Dry-Run)", command=lambda: self._guard(lambda: self.run_sync(apply=False))).pack(side="left")
        ttk.Button(btns, text="Aktarımı Başlat", command=lambda: self._guard(lambda: self.run_sync(apply=True))).pack(side="left", padx=6)
        ttk.Button(btns, text="Ayarları Kaydet", command=lambda: self._guard(self.save_cfg_ui)).pack(side="left", padx=6)
        ttk.Button(btns, text="Seçili Kopyayı Sil", command=lambda: self._guard(self.delete_selected_copy)).pack(side="left", padx=6)
        ttk.Button(btns, text="Handoff Klasörünü Aç", command=lambda: self._guard(self.open_handoffs)).pack(side="right")

        self.sync_log = scrolledtext.ScrolledText(tab, height=10, wrap="word", state="disabled")
        self.sync_log.pack(fill="both", expand=True)

    def _build_mcp_tab(self):
        tab = ttk.Frame(self.nb, padding=8)
        self.nb.add(tab, text="MCP & Durum")

        self.mcp_status_var = tk.StringVar(value="Kontrol ediliyor...")
        ttk.Label(tab, textvariable=self.mcp_status_var, justify="left").pack(anchor="w", pady=(0, 8))

        btns = ttk.Frame(tab)
        btns.pack(fill="x", pady=(0, 8))
        ttk.Button(btns, text="Durumu Yenile", command=lambda: self._guard(self.refresh_mcp_status)).pack(side="left")
        ttk.Button(btns, text="Antigravity'ye Bağla", command=lambda: self._guard(lambda: self.wire_mcp("agy"))).pack(side="left", padx=6)
        ttk.Button(btns, text="OMP'ye Bağla", command=lambda: self._guard(lambda: self.wire_mcp("omp"))).pack(side="left", padx=6)
        ttk.Button(btns, text="MCP Bağlantı Testi (stdio)", command=lambda: self._guard(self.test_mcp_stdio)).pack(side="left", padx=6)

        self.mcp_log = scrolledtext.ScrolledText(tab, height=12, wrap="word", state="disabled")
        self.mcp_log.pack(fill="both", expand=True)

    def _build_classic_tab(self):
        tab = ttk.Frame(self.nb, padding=6)
        self.nb.add(tab, text="Klasik Export / Inject")

        exp = ttk.Frame(tab)
        exp.pack(fill="x", pady=(0, 4))
        ttk.Label(exp, text="Token bütçesi:").pack(side="left")
        self.budget_var = tk.StringVar(value=str(cb.DEFAULT_BUDGET))
        ttk.Entry(exp, textvariable=self.budget_var, width=8).pack(side="left", padx=4)
        ttk.Button(exp, text="Export", command=lambda: self._guard(self.export)).pack(side="left", padx=4)
        ttk.Button(exp, text="Farklı kaydet...", command=lambda: self._guard(self.save)).pack(side="left", padx=4)
        ttk.Button(exp, text="Kopyala", command=lambda: self._guard(self.copy)).pack(side="left", padx=4)

        self.pack_view = scrolledtext.ScrolledText(tab, height=7, wrap="word", state="disabled")
        self.pack_view.pack(fill="both", expand=True, pady=(0, 4))

        inj = ttk.LabelFrame(tab, text="Inject (Kural Dosyası)", padding=6)
        inj.pack(fill="x", pady=(0, 4))
        ttk.Label(inj, text="Proje klasörü:").grid(row=0, column=0, sticky="w")
        self.project_var = tk.StringVar()
        ttk.Entry(inj, textvariable=self.project_var, width=65).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(inj, text="Seç...", command=lambda: self._guard(self.pick_project)).grid(row=0, column=2)
        self.auto_var = tk.StringVar(value="")
        ttk.Label(inj, textvariable=self.auto_var, foreground="#555555").grid(row=0, column=3, sticky="w", padx=4)
        ttk.Label(inj, text="Hedef araç:").grid(row=1, column=0, sticky="w", pady=4)
        self.target_var = tk.StringVar(value="omp")
        ttk.Combobox(inj, textvariable=self.target_var, values=SOURCES, width=6, state="readonly").grid(row=1, column=1, sticky="w", padx=4)
        scope_fr = ttk.Frame(inj)
        scope_fr.grid(row=1, column=2, columnspan=2, sticky="w")
        self.scope_var = tk.StringVar(value="auto")
        for val, text in (("auto", "Otomatik"), ("project", "Proje"), ("global", "Global")):
            ttk.Radiobutton(scope_fr, text=text, value=val, variable=self.scope_var).pack(side="left", padx=(0, 6))
        btns = ttk.Frame(inj)
        btns.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Button(btns, text="Kuru çalıştırma önizleme", command=lambda: self._guard(self.dry_run)).pack(side="left")
        ttk.Button(btns, text="Uygula...", command=lambda: self._guard(self.apply)).pack(side="left", padx=6)
        inj.columnconfigure(1, weight=1)

        self.preview = scrolledtext.ScrolledText(tab, height=4, wrap="word", state="disabled")
        self.preview.pack(fill="x")

    # ------------------------------------------------------------ helpers
    def status(self, text: str, error: bool = False):
        self.status_var.set(text)
        self.status_lbl.config(fg="#b00020" if error else "black")
        self.last_error = text if error else None

    def _guard(self, fn):
        try:
            fn()
        except Exception as exc:
            self.status(err_text(exc), error=True)

    def _run(self, fn, on_done, label: str):
        if self.busy:
            self.status("Başka bir işlem sürüyor", error=True)
            return
        self.busy = True
        self.status(f"{label}...")

        def worker():
            try:
                self.q.put((on_done, fn(), None))
            except Exception as exc:
                self.q.put((None, None, exc))

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(POLL_MS, self._poll)

    def _poll(self):
        try:
            while True:
                on_done, res, exc = self.q.get_nowait()
                self.busy = False
                if exc is not None:
                    self.status(err_text(exc), error=True)
                else:
                    try:
                        on_done(res)
                    except Exception as cb_exc:
                        self.status(err_text(cb_exc), error=True)
        except queue.Empty:
            pass
        if self.busy:
            self.root.after(POLL_MS, self._poll)

    def _set_text(self, widget: scrolledtext.ScrolledText, text: str):
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.config(state="disabled")

    def _limit(self) -> int:
        try:
            n = int(self.limit_var.get())
        except ValueError:
            raise cb.CtxError("limit bir tam sayı olmalı")
        if not LIMIT_MIN <= n <= LIMIT_MAX:
            raise cb.CtxError(f"limit {LIMIT_MIN}-{LIMIT_MAX} aralığında olmalı")
        return n

    def _budget(self) -> int:
        try:
            n = int(self.budget_var.get())
        except ValueError:
            raise cb.CtxError("token bütçesi bir tam sayı olmalı")
        if n < cb.MIN_BUDGET:
            raise cb.CtxError(f"token bütçesi en az {cb.MIN_BUDGET} olmalı")
        return n

    def _selected_row(self):
        sel = self.tree.selection()
        if not sel:
            return None
        return self.tree.item(sel[0], "values")

    def _selected_id(self) -> str:
        row = self._selected_row()
        return row[1] if row else "latest"

    def _collect_cfg(self) -> dict:
        try:
            b_lim = max(1, min(500, int(self.batch_limit_var.get())))
        except ValueError:
            raise cb.CtxError("Son N değeri tam sayı olmalı")
        try:
            max_m = max(0, min(5000, int(self.max_msgs_var.get())))
        except ValueError:
            raise cb.CtxError("Maks. mesaj tam sayı olmalı")
        return {
            "direction": self.dir_var.get(),
            "scope": self.sync_scope_var.get(),
            "batch_limit": b_lim,
            "max_messages": max_m,
            "include_images": bool(self.inc_images_var.get()),
            "include_checkpoints": bool(self.inc_cp_var.get()),
            "skip_imported": bool(self.skip_imp_var.get()),
            "confirm_delete": bool(self.confirm_del_var.get()),
        }

    # ------------------------------------------------------------ sync & settings actions
    def save_cfg_ui(self):
        self.cfg = self._collect_cfg()
        path = save_settings(self.cfg)
        self.status(f"Ayarlar kaydedildi: {cb.scrub_paths(str(path))}")

    def run_sync(self, apply: bool):
        cfg = self._collect_cfg()
        if apply:
            save_settings(cfg)
        direction = cfg["direction"]
        scope = cfg["scope"]
        sel_id = self._selected_id()
        if scope == "selected" and not self._selected_row():
            raise cb.CtxError("Önce listeden bir oturum seçin (veya Son N / Tümü seçin)")

        def work():
            if direction == "omp_to_agy":
                if scope == "selected":
                    return cbi.import_omp_to_agy(
                        ident=sel_id,
                        max_messages=cfg["max_messages"],
                        apply=apply,
                    )
                return cbi.import_omp_batch(
                    limit=cfg["batch_limit"],
                    all_sessions=(scope == "all"),
                    max_messages=cfg["max_messages"],
                    apply=apply,
                )
            else:
                if scope == "selected":
                    return cbi.import_agy_to_omp(ident=sel_id, apply=apply)
                return cbi.import_agy_batch(
                    limit=cfg["batch_limit"],
                    all_sessions=(scope == "all"),
                    apply=apply,
                )

        def done(res: dict):
            self._set_text(self.sync_log, json.dumps(res, ensure_ascii=False, indent=2))
            if apply:
                cnt = res.get("imported_count", 1)
                self.status(f"Aktarım tamamlandı ({cnt} oturum). Antigravity'de görmek için pencereyi yenileyin/açıp kapayın.")
            else:
                self.status("Dry-run önizleme hazır (dosya yazılmadı)")

        self._run(work, done, "Aktarım çalışıyor" if apply else "Önizleme hazırlanıyor")

    def delete_selected_copy(self):
        row = self._selected_row()
        if not row:
            raise cb.CtxError("Silmek için önce listeden [OMP] veya [AGY] ön ekli bir kopya seçin")
        cid = str(row[1])
        title = str(row[3])
        src = self.source_var.get()

        if not (title.startswith("[OMP]") or title.startswith("[AGY]")):
            raise cb.CtxError("Güvenlik: Yalnızca [OMP] veya [AGY] ön ekli aktarılmış kopyalar buradan silinebilir; orijinal oturumlara dokunulmaz.")

        if self.confirm_del_var.get():
            if not messagebox.askyesno(
                "Kopyayı Sil",
                f"Seçili aktarılmış kopya sadece {src.upper()} tarafından silinecek:\n\n{title}\n({cid})\n\nKarşı taraftaki orijinal oturum korunur. Devam edilsin mi?",
            ):
                self.status("Silme iptal edildi")
                return

        if src == "agy":
            agy_dir = cb.agy_root()
            for p in (
                agy_dir / "conversations" / f"{cid}.db",
                agy_dir / "conversations" / f"{cid}.db-wal",
                agy_dir / "conversations" / f"{cid}.db-shm",
                agy_dir / "annotations" / f"{cid}.pbtxt",
            ):
                p.unlink(missing_ok=True)
            bdir = agy_dir / "brain" / cid
            if bdir.is_dir():
                shutil.rmtree(bdir, ignore_errors=True)
            sdb = agy_dir / "conversation_summaries.db"
            if sdb.is_file():
                con = sqlite3.connect(str(sdb), timeout=5.0)
                con.execute("DELETE FROM conversation_summaries WHERE conversation_id = ?", (cid,))
                con.commit()
                con.close()
        else:
            omp_dir = cb.omp_root()
            for f in (omp_dir / "sessions").rglob(f"*_{cid}.jsonl"):
                f.unlink(missing_ok=True)
            hdb = omp_dir / "history.db"
            if hdb.is_file():
                con = sqlite3.connect(str(hdb), timeout=5.0)
                con.execute("DELETE FROM session_titles WHERE session_id = ?", (cid,))
                con.execute("DELETE FROM session_recaps WHERE session_id = ?", (cid,))
                con.commit()
                con.close()

        self.status(f"Kopya silindi ({src}): {cid[:12]}")
        self.refresh()

    def open_handoffs(self):
        hdir = cb.user_home() / ".ctxbridge" / "handoffs"
        hdir.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(hdir))
        self.status(f"Handoff klasörü: {cb.scrub_paths(str(hdir))}")

    # ------------------------------------------------------------ MCP tab actions
    def _mcp_paths(self):
        agy_mcp = cb.user_home() / ".gemini" / "config" / "mcp_config.json"
        omp_mcp = cb.omp_root() / "mcp.json"
        return agy_mcp, omp_mcp

    def refresh_mcp_status(self):
        agy_mcp, omp_mcp = self._mcp_paths()
        lines = []
        for label, path in (("Antigravity", agy_mcp), ("OMP", omp_mcp)):
            if path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    has = "context-bridge" in (data.get("mcpServers") or {})
                    lines.append(f"{label}: {'BAĞLI' if has else 'BAĞLI DEĞİL'} ({cb.scrub_paths(str(path))})")
                except Exception as exc:
                    lines.append(f"{label}: OKUNAMADI ({exc})")
            else:
                lines.append(f"{label}: DOSYA YOK ({cb.scrub_paths(str(path))})")
        self.mcp_status_var.set("\n".join(lines))

    def wire_mcp(self, target: str):
        agy_mcp, omp_mcp = self._mcp_paths()
        path = agy_mcp if target == "agy" else omp_mcp
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"mcpServers": {}}
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8") or '{"mcpServers":{}}')
        data.setdefault("mcpServers", {})
        mcp_script = str(Path(__file__).resolve().parent / "ctxbridge_mcp.py")
        entry = {"command": sys.executable, "args": ["-u", mcp_script]}
        if target == "omp":
            entry = {"type": "stdio", **entry}
        data["mcpServers"]["context-bridge"] = entry
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        self.refresh_mcp_status()
        self.status(f"context-bridge {target.upper()} MCP ayarına eklendi")

    def test_mcp_stdio(self):
        mcp_script = Path(__file__).resolve().parent / "ctxbridge_mcp.py"
        reqs = "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26"}}),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
            ]
        ) + "\n"

        def work():
            p = subprocess.run(
                [sys.executable, "-u", str(mcp_script)],
                input=reqs,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
                check=True,
            )
            lines = [json.loads(x) for x in p.stdout.strip().splitlines() if x.strip()]
            tools = [t["name"] for t in lines[1]["result"]["tools"]]
            return {"protocolVersion": lines[0]["result"]["protocolVersion"], "tools": tools}

        def done(res):
            self._set_text(self.mcp_log, json.dumps(res, ensure_ascii=False, indent=2))
            self.status(f"MCP stdio testi OK ({len(res['tools'])} araç)")

        self._run(work, done, "MCP stdio test ediliyor")

    # ------------------------------------------------------------ classic actions
    def refresh(self):
        limit = self._limit()
        src = self.source_var.get()
        self.refresh_agy_roots()
        self._run(lambda: fetch_rows(src, limit), lambda rows: self._fill_tree(src, rows), "Liste yükleniyor")

    def refresh_agy_roots(self):
        path, src = cb.agy_root_info()
        self.agy_src, self.agy_path = src, path
        self.agy_paths = [None, *cb.agy_root_candidates()]
        self.agy_combo.config(values=["otomatik", *(str(p) for p in self.agy_paths[1:])])
        self.agy_combo.config(state="disabled" if src == "CTXBRIDGE_AGY_DIR" else "readonly")
        self.agy_root_var.set(f"{AGY_SRC_TEXT.get(src, src)}: {path}")

    def pick_agy_root(self):
        idx = self.agy_combo.current()
        if idx < 0:
            return
        cb.set_agy_override(self.agy_paths[idx])
        self.refresh()

    def _fill_tree(self, src: str, rows: list):
        self.tree.delete(*self.tree.get_children())
        for row in rows:
            self.tree.insert("", "end", values=row)
        self.loaded = True
        extra = f" · kök: {self.agy_path} ({self.agy_src})" if src == "agy" else ""
        self.status(f"{len(rows)} oturum listelendi ({src}){extra}")

    def export(self):
        budget = self._budget()
        src = self.source_var.get()
        ident = self._selected_id()

        def done(text: str):
            self.pack_text = text
            self._set_text(self.pack_view, text)
            self.status(f"Pack hazır (~{cb.est_tokens(text)} token)")

        self._run(lambda: cb.export_text(src, ident, budget), done, "Pack üretiliyor")

    def save(self):
        if not self.pack_text:
            raise cb.CtxError("önce Export yapın")
        path = filedialog.asksaveasfilename(defaultextension=".md", filetypes=[("Markdown", "*.md"), ("Tümü", "*.*")])
        if not path:
            return
        Path(path).write_text(self.pack_text, encoding="utf-8", newline="\n")
        self.status(f"kaydedildi: {path}")

    def copy(self):
        if not self.pack_text:
            raise cb.CtxError("önce Export yapın")
        self.root.clipboard_clear()
        self.root.clipboard_append(self.pack_text)
        self.status("pack panoya kopyalandı")

    def pick_project(self):
        path = filedialog.askdirectory()
        if path:
            self.auto.on_pick()
            self.project_var.set(path)
            self.auto_var.set("elle seçildi")
            self.status(f"proje elle seçildi: {path}")

    def on_select(self):
        sel = self.tree.selection()
        if not sel:
            return
        src = self.source_var.get()
        ident = self.tree.item(sel[0], "values")[1]
        self.auto.on_row()
        self.target_var.set(cb.other_tool(src))
        self.auto_var.set("algılanıyor...")
        self._run(lambda: cb.detect_project(src, ident), lambda det: self._apply_detection(det), "Proje algılanıyor")

    def _apply_detection(self, detected: cb.Detection):
        choice, why = cb.pick_project([(detected.via, detected.dir)] if detected.dir else [], "auto")
        shown = detected.dir if detected.dir else "yok"
        if not self.auto.fill_allowed():
            self.status(f"elle seçili proje korundu · algılanan: {shown}")
            return
        if choice is not None:
            self.project_var.set(str(choice.root))
            self.scope_var.set("auto")
            self.auto_var.set(f"otomatik ({choice.source})")
            self.status(f"proje kaynağı: {choice.source} · kök: {choice.root}")
            return
        gpath = cb.scrub_paths(str(cb.global_target("agy")))
        self.project_var.set("")
        self.scope_var.set("global")
        self.target_var.set("agy")
        self.auto_var.set(f"global -> {gpath}")
        self.status(f"geçerli proje yok ({why}) · kapsam: global · hedef: {gpath}")

    def _plan(self) -> cb.InjectPlan:
        if not self.pack_text:
            raise cb.CtxError("önce Export ile pack üretin")
        scope = self.scope_var.get()
        proj = self.project_var.get().strip()
        if scope == "project" and not proj:
            raise cb.CtxError("proje kapsamı için proje klasörü seçin")
        fd, tmp = tempfile.mkstemp(suffix=".md", prefix="ctxbridge-pack-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(self.pack_text)
            return cb.plan_inject(self.target_var.get(), tmp, proj or None, scope)
        finally:
            os.unlink(tmp)

    def _describe(self, plan: cb.InjectPlan) -> str:
        lines = cb.describe_plan(plan).splitlines()
        if len(lines) > 60:
            lines = lines[:60] + [f"... ({len(lines) - 60} satır daha)"]
        return "\n".join(lines)

    def dry_run(self):
        plan = self._plan()
        self._set_text(self.preview, "KURU ÇALIŞTIRMA (yazılmadı)\n" + self._describe(plan))
        self.status("önizleme hazır; henüz yazılmadı")

    def apply(self):
        plan = self._plan()
        if plan.mode == "unchanged":
            self._set_text(self.preview, self._describe(plan))
            self.status("değişiklik yok; yazılmadı")
            return
        dest = plan.fallback if plan.mode == "fallback" else plan.target
        msg = f"Dosya:\n{cb.scrub_paths(str(dest))}\n\nKapsam: {plan.scope} ({plan.reason})\nİşlem: {cb.MODE_TEXT[plan.mode]}\n"
        if plan.scope == "global" and plan.exists:
            msg += "Genel dosya zaten var: yalnızca ctxbridge bloğu eklenir/değiştirilir; blok dışı içerik aynı kalır.\n"
        if plan.backup is not None:
            msg += f"Mevcut dosya önce yedeklenir:\n{cb.scrub_paths(str(plan.backup))}\n"
        msg += "\nDevam edilsin mi?"
        if not messagebox.askyesno("Uygulama onayı", msg):
            self.status("uygulama iptal edildi")
            return
        result = cb.apply_inject(plan)
        self._set_text(self.preview, "UYGULANDI\n" + self._describe(plan))
        self.status(result)


def selftest() -> int:
    root = tk.Tk()
    app = App(root)
    print("pencere kuruldu")
    deadline = time.time() + 30
    while not app.loaded and app.last_error is None and time.time() < deadline:
        root.update()
        time.sleep(0.02)
    rows = len(app.tree.get_children())
    print(f"satır sayısı: {rows}")
    print(f"durum: {app.status_var.get()}")
    print(f"mcp: {app.mcp_status_var.get().replace(chr(10), ' | ')}")
    root.destroy()
    return 0 if app.loaded else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ctxbridge_gui", description="ctxbridge tkinter arayüzü")
    ap.add_argument("--selftest", action="store_true", help="pencereyi kur, listeyi yükle, satır sayısını yazdır, çık")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest()
    root = tk.Tk()
    App(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
