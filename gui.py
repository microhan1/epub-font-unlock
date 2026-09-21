"""tkinter GUI for epub-font-unlock.

The flow the PRD asks for: drop a book, read what is pinned down in it, tick
what to release, then run. Nothing is written to disk until Run is pressed --
adding a file only reads it.
"""
from __future__ import annotations

import dataclasses
import os
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import i18n
import unlock
from i18n import t
from unlock import Options

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _HAS_DND = True
except Exception:  # pragma: no cover - optional dependency
    _HAS_DND = False


def _saved_options(settings: dict) -> Options:
    """Options from settings.json, trusting nothing about its types. The file
    sits beside the exe where anyone can edit it; one wrong value must not stop
    the window from opening."""
    raw = settings.get("options")
    raw = raw if isinstance(raw, dict) else {}
    default = Options()

    def flag(key: str) -> bool:
        value = raw.get(key)
        return value if isinstance(value, bool) else getattr(default, key)

    return Options(font=flag("font"), size=flag("size"), line=flag("line"),
                   margin=flag("margin"),
                   remove_font_files=flag("remove_font_files")).validated()


class App:
    def __init__(self, initial_files: list[str] | None = None) -> None:
        self.root = TkinterDnD.Tk() if _HAS_DND else tk.Tk()
        self.root.geometry("880x660")
        self.root.minsize(760, 560)

        self.files: list[str] = []
        self.analyses: dict[str, unlock.Analysis] = {}
        self.outputs: list[str] = []
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.analyzer: threading.Thread | None = None
        self.analyze_gen = 0
        self._texts: list[tuple[tk.Misc, str, str]] = []
        self._status_key = ""
        self._status_kwargs: dict = {}
        self._closing = False

        saved = _saved_options(i18n.load_settings())
        self.var_font = tk.BooleanVar(value=saved.font)
        self.var_size = tk.BooleanVar(value=saved.size)
        self.var_line = tk.BooleanVar(value=saved.line)
        self.var_margin = tk.BooleanVar(value=saved.margin)
        self.var_remove_files = tk.BooleanVar(value=saved.remove_font_files)
        self.var_lang = tk.StringVar(value=i18n.LANG_NAMES[i18n.current_lang()])

        self._build()
        self._apply_texts()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        if initial_files:
            self.root.after(100, lambda: self.add_paths(initial_files))

    # ------------------------------------------------------------ building
    def _reg(self, widget: tk.Misc, key: str, attr: str = "text") -> tk.Misc:
        self._texts.append((widget, key, attr))
        return widget

    def _build(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=1)

        # ---- header
        head = ttk.Frame(root, padding=(12, 10, 12, 4))
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        self._reg(ttk.Label(head, font=("", 15, "bold")), "app_title").grid(row=0, column=0, sticky="w")
        self._reg(ttk.Label(head), "lbl_language").grid(row=0, column=1, padx=(0, 6))
        self.cmb_lang = ttk.Combobox(
            head, state="readonly", width=10, textvariable=self.var_lang,
            values=[i18n.LANG_NAMES[c] for c in i18n.LANGS],
        )
        self.cmb_lang.grid(row=0, column=2)
        self.cmb_lang.bind("<<ComboboxSelected>>", self._on_lang)

        # ---- files
        files = self._reg(ttk.LabelFrame(root, padding=8), "lbl_files")
        files.grid(row=1, column=0, sticky="ew", padx=12, pady=4)
        files.columnconfigure(0, weight=1)
        self.lbl_drop = ttk.Label(files, anchor="center", relief="groove", padding=6, foreground="#555")
        self._reg(self.lbl_drop, "drop_hint")
        self.lbl_drop.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.lst_files = tk.Listbox(files, height=5, activestyle="none")
        self.lst_files.grid(row=1, column=0, sticky="nsew")
        btns = ttk.Frame(files)
        btns.grid(row=1, column=1, sticky="ns", padx=(8, 0))
        self.btn_add = self._reg(ttk.Button(btns, command=self._add_files_dialog), "btn_add_files")
        self.btn_add.pack(fill="x")
        self.btn_add_dir = self._reg(ttk.Button(btns, command=self._add_folder_dialog), "btn_add_folder")
        self.btn_add_dir.pack(fill="x", pady=4)
        self.btn_clear = self._reg(ttk.Button(btns, command=self.clear_files), "btn_clear")
        self.btn_clear.pack(fill="x")
        if _HAS_DND:
            for w in (root, self.lbl_drop, self.lst_files):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self._on_drop)

        # ---- middle: analysis above, options below
        mid = ttk.Frame(root)
        mid.grid(row=2, column=0, sticky="nsew", padx=12, pady=4)
        mid.columnconfigure(0, weight=1)
        mid.rowconfigure(1, weight=1)

        analysis = self._reg(ttk.LabelFrame(mid, padding=8), "lbl_analysis")
        analysis.grid(row=0, column=0, sticky="ew")
        analysis.columnconfigure(0, weight=1)
        self.lbl_summary = ttk.Label(analysis, anchor="w", font=("", 11))
        self.lbl_summary.grid(row=0, column=0, sticky="ew")
        self.lbl_extra = ttk.Label(analysis, anchor="w", foreground="#555")
        self.lbl_extra.grid(row=1, column=0, sticky="ew", pady=(2, 0))

        opts = self._reg(ttk.LabelFrame(mid, padding=8), "lbl_options")
        opts.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self._reg(ttk.Checkbutton(opts, variable=self.var_font), "opt_font").pack(anchor="w")
        self._reg(ttk.Checkbutton(opts, variable=self.var_size), "opt_size").pack(anchor="w")
        self._reg(ttk.Checkbutton(opts, variable=self.var_line), "opt_line").pack(anchor="w")
        row_margin = ttk.Frame(opts)
        row_margin.pack(anchor="w", fill="x")
        self._reg(ttk.Checkbutton(row_margin, variable=self.var_margin), "opt_margin").pack(side="left")
        self._reg(ttk.Label(row_margin, foreground="#777"), "opt_margin_hint").pack(side="left", padx=(8, 0))
        self._reg(ttk.Checkbutton(opts, variable=self.var_remove_files), "opt_remove_files").pack(anchor="w")

        # ---- log
        logf = self._reg(ttk.LabelFrame(root, padding=4), "lbl_log")
        logf.grid(row=3, column=0, sticky="ew", padx=12, pady=4)
        logf.columnconfigure(0, weight=1)
        self.txt_log = tk.Text(logf, height=6, state="disabled", wrap="none")
        self.txt_log.grid(row=0, column=0, sticky="ew")
        sb = ttk.Scrollbar(logf, command=self.txt_log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.txt_log.configure(yscrollcommand=sb.set)

        # ---- bottom
        bot = ttk.Frame(root, padding=(12, 4, 12, 10))
        bot.grid(row=4, column=0, sticky="ew")
        bot.columnconfigure(0, weight=1)
        self.progress = ttk.Progressbar(bot, mode="determinate")
        self.progress.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 6))
        self.lbl_status = ttk.Label(bot)
        self.lbl_status.grid(row=1, column=0, sticky="w")
        self.btn_open = self._reg(ttk.Button(bot, command=self.open_result, state="disabled"), "btn_open_result")
        self.btn_open.grid(row=1, column=1, padx=4)
        self.btn_cancel = self._reg(ttk.Button(bot, command=self.cancel, state="disabled"), "btn_cancel")
        self.btn_cancel.grid(row=1, column=2, padx=4)
        self.btn_run = self._reg(ttk.Button(bot, command=self.run), "btn_run")
        self.btn_run.grid(row=1, column=3, padx=(4, 0))

        self._set_status("status_ready")

    def _apply_texts(self) -> None:
        self.root.title(t("app_title"))
        for widget, key, attr in self._texts:
            try:
                widget.configure(**{attr: t(key)})
            except tk.TclError:
                pass
        self._update_analysis_labels()
        if self._status_key:
            self._set_status(self._status_key, **self._status_kwargs)

    # ------------------------------------------------------------ helpers
    def _set_status(self, key: str, **kwargs) -> None:
        self._status_key, self._status_kwargs = key, kwargs
        self.lbl_status.configure(text=t(key, **kwargs))

    def log(self, key: str, **kwargs) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", t(key, **kwargs) + "\n")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def options(self) -> Options:
        return Options(font=self.var_font.get(), size=self.var_size.get(),
                       line=self.var_line.get(), margin=self.var_margin.get(),
                       remove_font_files=self.var_remove_files.get()).validated()

    def _save_options(self) -> None:
        settings = i18n.load_settings()
        settings["options"] = dataclasses.asdict(self.options())
        i18n.save_settings(settings)

    def _on_lang(self, _event=None) -> None:
        index = self.cmb_lang.current()
        if index >= 0:
            i18n.set_lang(i18n.LANGS[index])
        self._apply_texts()

    def _totals(self) -> unlock.Counts:
        total = unlock.Counts()
        for path in self.files:
            analysis = self.analyses.get(path)
            if analysis is None:
                continue
            c = analysis.counts
            total.fonts += c.fonts
            total.sizes += c.sizes
            total.lines += c.lines
            total.margins += c.margins
            total.inline += c.inline
            total.font_files += c.font_files
            total.font_faces += c.font_faces
            total.families |= c.families
        return total

    def _update_analysis_labels(self) -> None:
        if self.analyzer is not None and self.analyzer.is_alive():
            self.lbl_summary.configure(text=t("analysis_running"))
            self.lbl_extra.configure(text="")
            return
        if not self.files:
            self.lbl_summary.configure(text=t("analysis_empty"))
            self.lbl_extra.configure(text="")
            return
        total = self._totals()
        if not total.any_change():
            self.lbl_summary.configure(text=t("msg_already"))
            self.lbl_extra.configure(text="")
            return
        self.lbl_summary.configure(text=t("analysis_summary", fonts=total.family_count,
                                          sizes=total.sizes, lines=total.lines))
        self.lbl_extra.configure(text=t("analysis_extra", margins=total.margins,
                                        inline=total.inline, font_files=total.font_files))

    # ------------------------------------------------------------ files
    def _on_drop(self, event) -> None:
        self.add_paths(list(self.root.tk.splitlist(event.data)))

    def _add_files_dialog(self) -> None:
        paths = filedialog.askopenfilenames(filetypes=[(t("file_dialog_epub"), "*.epub")])
        if paths:
            self.add_paths(list(paths))

    def _add_folder_dialog(self) -> None:
        folder = filedialog.askdirectory()
        if folder:
            self.add_paths([folder])

    def add_paths(self, paths: list[str]) -> None:
        """Analysis reads the whole book, so it runs off the UI thread. Files
        appear in the list only once they have been read successfully."""
        found = [p for p in unlock.collect_epubs(paths) if p not in self.files]
        if not found:
            self.log("err_no_epub_found")
            return
        if self.analyzer is not None and self.analyzer.is_alive():
            return
        self.analyze_gen += 1
        gen = self.analyze_gen
        self._set_controls(busy=True)

        def work() -> None:
            for path in found:
                if gen != self.analyze_gen:
                    return
                try:
                    analysis = unlock.analyze_epub(path)
                except unlock.DrmProtected:
                    self.root.after(0, lambda p=path: self._add_failed(p, "err_drm", "log_drm"))
                    continue
                except unlock.BrokenArchive:
                    self.root.after(0, lambda p=path: self._add_failed(p, "err_open_failed", "log_skipped"))
                    continue
                except Exception:
                    self.root.after(0, lambda p=path: self._add_failed(p, "err_open_failed", "log_skipped"))
                    continue
                self.root.after(0, lambda a=analysis: self._add_done(gen, a))
            self.root.after(0, lambda: self._analysis_finished(gen))

        self.analyzer = threading.Thread(target=work, daemon=True)
        self.analyzer.start()
        self._update_analysis_labels()

    def _add_failed(self, path: str, err_key: str, log_key: str) -> None:
        name = os.path.basename(path)
        messagebox.showerror(t("dlg_error"), t(err_key, name=name), parent=self.root)
        self.log(log_key, name=name)

    def _add_done(self, gen: int, analysis: unlock.Analysis) -> None:
        if gen != self.analyze_gen:
            return
        path = analysis.path
        self.files.append(path)
        self.analyses[path] = analysis
        name = os.path.basename(path)
        counts = analysis.counts
        self.lst_files.insert("end", f"{name}  ({t('analysis_summary', fonts=counts.family_count, sizes=counts.sizes, lines=counts.lines)})")
        self.log("log_added", name=name)
        for item, error in analysis.failed:
            self.log("err_css_failed", name=item, error=error)

    def _analysis_finished(self, gen: int) -> None:
        if gen != self.analyze_gen:
            return
        self._set_controls(busy=False)
        self._update_analysis_labels()

    def clear_files(self) -> None:
        self.analyze_gen += 1  # any analysis still running now belongs to nothing
        self.files.clear()
        self.analyses.clear()
        self.lst_files.delete(0, "end")
        self._update_analysis_labels()

    # ------------------------------------------------------------ running
    def run(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if not self.files:
            messagebox.showinfo(t("app_title"), t("msg_no_files"), parent=self.root)
            return
        opts = self.options()
        if not opts.any_on():
            messagebox.showinfo(t("app_title"), t("msg_no_options"), parent=self.root)
            return
        self._save_options()
        self.cancel_event.clear()
        self.outputs = []
        files = list(self.files)
        total_items = sum(max(1, self.analyses[p].css_files + self.analyses[p].xhtml_files) for p in files)
        self.progress.configure(maximum=total_items, value=0)
        self._set_controls(busy=True, running=True)

        def work() -> None:
            done = 0
            count = skipped = 0
            totals = unlock.Counts()
            for index, path in enumerate(files, 1):
                name = os.path.basename(path)
                base = done

                def progress(i: int, n: int, _item: str, _base=base, _idx=index, _name=name) -> None:
                    self.root.after(0, lambda: self._on_progress(_base + i, _idx, len(files), _name))

                try:
                    result = unlock.process_epub(path, opts, progress=progress, cancel=self.cancel_event)
                except unlock.Cancelled:
                    self.root.after(0, lambda _n=name: self.log("log_cancelled", name=_n))
                    self.root.after(0, lambda: self._finished(True, count, skipped, totals))
                    return
                except unlock.DrmProtected:
                    self.root.after(0, lambda _n=name: self.log("log_drm", name=_n))
                    skipped += 1
                    continue
                except Exception as exc:  # one bad book must not end the batch
                    self.root.after(0, lambda _n=name, _e=exc: self.log("err_file_failed", name=_n, error=str(_e)))
                    skipped += 1
                    continue
                finally:
                    done = base + max(1, self.analyses[path].css_files + self.analyses[path].xhtml_files)
                    self.root.after(0, lambda _d=done: self.progress.configure(value=_d))
                for item, error in result.failed:
                    self.root.after(0, lambda _i=item, _e=error: self.log("err_css_failed", name=_i, error=_e))
                if result.already_unlocked:
                    self.root.after(0, lambda _n=name: self.log("log_already", name=_n))
                    skipped += 1
                    continue
                c = result.counts
                totals.fonts += c.fonts
                totals.sizes += c.sizes
                totals.lines += c.lines
                totals.margins += c.margins
                count += 1
                self.outputs.append(result.output_path)
                self.root.after(0, lambda _p=result.output_path: self.log("log_saved", path=_p))
            self.root.after(0, lambda: self._finished(False, count, skipped, totals))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _on_progress(self, done: int, file_index: int, files: int, name: str) -> None:
        self.progress.configure(value=done)
        self._set_status("status_processing", file=file_index, files=files, name=name)

    def _finished(self, cancelled: bool, count: int, skipped: int, totals: unlock.Counts) -> None:
        self._set_controls(busy=False, running=False)
        if cancelled or self._closing:
            self._set_status("status_cancelled")
            return
        self._set_status("status_done")
        self.progress.configure(value=self.progress["maximum"])
        if self.outputs:
            self.btn_open.configure(state="normal")
        lines = [t("msg_done", count=count)]
        if count:
            lines.append(t("msg_changes", fonts=totals.fonts, sizes=totals.sizes,
                           lines=totals.lines, margins=totals.margins))
        if skipped:
            lines.append(t("msg_skipped", count=skipped))
        messagebox.showinfo(t("app_title"), "\n".join(lines), parent=self.root)

    def cancel(self) -> None:
        self.cancel_event.set()
        self.btn_cancel.configure(state="disabled")

    def _set_controls(self, busy: bool, running: bool = False) -> None:
        for w in (self.btn_run, self.btn_add, self.btn_add_dir, self.btn_clear):
            w.configure(state="disabled" if busy else "normal")
        self.cmb_lang.configure(state="disabled" if busy else "readonly")
        self.btn_cancel.configure(state="normal" if running else "disabled")
        if busy:
            self.btn_open.configure(state="disabled")

    def open_result(self) -> None:
        if not self.outputs:
            return
        target = self.outputs[0]
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(target)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", target])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(target)])
        except OSError:
            pass

    def _on_close(self) -> None:
        self._closing = True
        self.cancel_event.set()
        self.analyze_gen += 1
        try:
            self._save_options()
        except Exception:
            pass
        self._close_when_idle()

    def _close_when_idle(self) -> None:
        """Wait for the worker before tearing down. It is a daemon thread, and
        exiting under it mid-write would leave a half-written epub behind."""
        for thread in (self.worker, self.analyzer):
            if thread is not None and thread.is_alive():
                self.root.after(100, self._close_when_idle)
                return
        self.root.destroy()

    def mainloop(self) -> None:
        self.root.mainloop()


def launch(initial_files: list[str] | None = None) -> None:
    App(initial_files).mainloop()
