#!/usr/bin/env python3
"""ctxbridge GUI (tkinter, stdlib). Reuses ctxbridge.py; no logic duplicated.

Usage: python ctxbridge_gui.py            # open window
       python ctxbridge_gui.py --selftest # build window, load list via same path, print count, exit
"""
import argparse
import os
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter import scrolledtext

import ctxbridge as cb

SOURCES = ("omp", "agy")
POLL_MS = 60
LIMIT_MIN, LIMIT_MAX = 1, 500
AGY_SRC_TEXT = {"otomatik": "otomatik", "elle": "elle", "CTXBRIDGE_AGY_DIR": "ortam değişkeni"}


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
        self.pack_text: str | None = None
        self.auto = AutoSelect()
        root.title("ctxbridge")
        root.geometry("1000x720")
        self._build()
        self.refresh()

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
        self.agy_combo = ttk.Combobox(top, textvariable=self.agy_root_var, width=48, state="readonly")
        self.agy_combo.pack(side="left", padx=4)
        self.agy_combo.bind("<<ComboboxSelected>>", lambda _e: self._guard(self.pick_agy_root))
        self.agy_src, self.agy_path, self.agy_paths = "", Path(), [None]

        mid = ttk.Frame(self.root, padding=(6, 0))
        mid.pack(fill="both", expand=True)
        cols = ("date", "id", "project", "title")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=10, selectmode="browse")
        for col, text, width in (("date", "Tarih", 150), ("id", "Kimlik", 290), ("project", "Proje/Çalışma alanı", 230), ("title", "Başlık", 300)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._guard(self.on_select))

        exp = ttk.Frame(self.root, padding=6)
        exp.pack(fill="x")
        ttk.Label(exp, text="Token bütçesi:").pack(side="left")
        self.budget_var = tk.StringVar(value=str(cb.DEFAULT_BUDGET))
        ttk.Entry(exp, textvariable=self.budget_var, width=8).pack(side="left", padx=4)
        ttk.Button(exp, text="Export", command=lambda: self._guard(self.export)).pack(side="left", padx=4)
        ttk.Button(exp, text="Farklı kaydet...", command=lambda: self._guard(self.save)).pack(side="left", padx=4)
        ttk.Button(exp, text="Kopyala", command=lambda: self._guard(self.copy)).pack(side="left", padx=4)

        self.pack_view = scrolledtext.ScrolledText(self.root, height=12, wrap="word", state="disabled")
        self.pack_view.pack(fill="both", expand=True, padx=6)

        inj = ttk.LabelFrame(self.root, text="Inject", padding=6)
        inj.pack(fill="x", padx=6, pady=6)
        ttk.Label(inj, text="Proje klasörü:").grid(row=0, column=0, sticky="w")
        self.project_var = tk.StringVar()
        ttk.Entry(inj, textvariable=self.project_var, width=70).grid(row=0, column=1, sticky="we", padx=4)
        ttk.Button(inj, text="Seç...", command=lambda: self._guard(self.pick_project)).grid(row=0, column=2)
        self.auto_var = tk.StringVar(value="")
        ttk.Label(inj, textvariable=self.auto_var, foreground="#555555").grid(row=0, column=3, sticky="w", padx=4)
        ttk.Label(inj, text="Hedef araç:").grid(row=1, column=0, sticky="w", pady=4)
        self.target_var = tk.StringVar(value="omp")
        ttk.Combobox(inj, textvariable=self.target_var, values=SOURCES, width=6, state="readonly").grid(row=1, column=1, sticky="w", padx=4)
        scope_fr = ttk.Frame(inj)
        scope_fr.grid(row=1, column=2, columnspan=2, sticky="w")
        self.scope_var = tk.StringVar(value="auto")
        for val, text in (("auto", "Otomatik"), ("project", "Proje"), ("global", "Global (araç klasörü)")):
            ttk.Radiobutton(scope_fr, text=text, value=val, variable=self.scope_var).pack(side="left", padx=(0, 6))
        btns = ttk.Frame(inj)
        btns.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
        ttk.Button(btns, text="Kuru çalıştırma önizleme", command=lambda: self._guard(self.dry_run)).pack(side="left")
        ttk.Button(btns, text="Uygula...", command=lambda: self._guard(self.apply)).pack(side="left", padx=6)
        inj.columnconfigure(1, weight=1)

        self.preview = scrolledtext.ScrolledText(self.root, height=5, wrap="word", state="disabled")
        self.preview.pack(fill="x", padx=6)

        self.status_var = tk.StringVar(value="Hazır")
        self.status_lbl = tk.Label(self.root, textvariable=self.status_var, anchor="w", padx=6, pady=3)
        self.status_lbl.pack(fill="x", side="bottom")

    # ------------------------------------------------------------ helpers
    def status(self, text: str, error: bool = False):
        self.status_var.set(text)
        self.status_lbl.config(fg="#b00020" if error else "black")
        self.last_error = text if error else None

    def _guard(self, fn):
        try:
            fn()
        except Exception as exc:  # shown in status bar, never a traceback
            self.status(err_text(exc), error=True)

    def _run(self, fn, on_done, label: str):
        """Runs fn in a worker thread; on_done runs on the Tk thread via after() polling."""
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

    def _selected_id(self) -> str:
        sel = self.tree.selection()
        if not sel:
            return "latest"
        return self.tree.item(sel[0], "values")[1]

    # ------------------------------------------------------------ actions
    def refresh(self):
        limit = self._limit()
        src = self.source_var.get()
        self.refresh_agy_roots()
        self._run(lambda: fetch_rows(src, limit), lambda rows: self._fill_tree(src, rows), "Liste yükleniyor")

    def refresh_agy_roots(self):
        """Antigravity kök etiketi (salt okunur) ve açılır liste; kaynak: ortam değişkeni, elle ya da otomatik."""
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
        """Builds the inject plan from the current pack via plan_inject (temp file, removed after)."""
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
