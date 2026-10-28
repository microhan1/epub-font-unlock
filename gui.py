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
import webbrowser
from tkinter import filedialog, messagebox, ttk

import i18n
import unlock
import update_check
from i18n import t
from unlock import Options
from version import APP_VERSION, UPDATE_SLUG

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    _HAS_DND = True
except Exception:  # pragma: no cover - optional dependency
    _HAS_DND = False


def _enable_dpi_awareness() -> None:
    """Tell Windows this program draws at the display's real resolution.

    Without it the window is laid out at 96 DPI and then stretched by Windows,
    so on a 125-200% display (any laptop, any 4K screen) every glyph comes out
    blurred. System-aware is chosen over per-monitor on purpose: Tk 8.6 does not
    follow a window across monitors of different density, so per-monitor buys
    nothing here and risks a half-scaled window.

    It can only be set once per process, and must be set before the first
    window exists; a second call is refused, which is fine.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # Windows 7 and older
        except Exception:
            pass


LINK_FOCUS_BG = "#d3e3fd"  # behind [Send feedback] while it has the keyboard focus


def _screen_area(widget: tk.Misc) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) of the work area of the monitor the widget is on,
    taskbar left out. A second monitor can sit left of or above the main one, at
    negative coordinates, so 0 is not the screen's edge. Without the Windows call,
    the main screen as Tk knows it."""
    if sys.platform == "win32":
        try:
            import ctypes
            import ctypes.wintypes as wt

            class MonitorInfo(ctypes.Structure):
                _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT),
                            ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]

            user32 = ctypes.windll.user32
            user32.MonitorFromPoint.restype = wt.HANDLE
            user32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
            user32.GetMonitorInfoW.argtypes = [wt.HANDLE, ctypes.POINTER(MonitorInfo)]
            point = wt.POINT(widget.winfo_rootx() + widget.winfo_width() // 2,
                             widget.winfo_rooty() + widget.winfo_height() // 2)
            monitor = user32.MonitorFromPoint(point, 2)  # MONITOR_DEFAULTTONEAREST
            info = MonitorInfo()
            info.cbSize = ctypes.sizeof(MonitorInfo)
            if monitor and user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
                work = info.rcWork
                return work.left, work.top, work.right, work.bottom
        except Exception:
            pass
    return 0, 0, widget.winfo_screenwidth(), widget.winfo_screenheight()


def _tip_position(link_x: int, link_y: int, link_w: int, link_h: int, tip_w: int, tip_h: int,
                  area: tuple[int, int, int, int],
                  window: tuple[int, int] | None = None) -> tuple[int, int]:
    """Where the link's tooltip goes: under the link, ending where the link ends
    (the sentence is longer than the link, which sits near the window's right
    side), kept within the window's (left, right) when it fits there, then pushed
    back inside the monitor's work area: off neither side, and above the link
    when there is no room below it. The monitor has the last word."""
    left, top, right, bottom = area
    x = min(link_x, link_x + link_w - tip_w)
    if window is not None and tip_w <= window[1] - window[0]:
        x = max(window[0], min(x, window[1] - tip_w))
    x = max(left, min(x, right - tip_w))
    y = link_y + link_h + 4
    if y + tip_h > bottom:
        y = link_y - 4 - tip_h
    y = max(top, y)
    return x, y


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
        _enable_dpi_awareness()  # before the first window, or it is too late
        self.root = TkinterDnD.Tk() if _HAS_DND else tk.Tk()
        # The real size is worked out from the built layout in _fit_window();
        # see there for why it is not a fixed number.
        self.root.minsize(820, 600)

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
        # What the window is busy with is kept as explicit flags, set and cleared
        # on the UI thread. Asking a thread whether it is alive does not do: it
        # can still be alive for a moment after posting its last callback, which
        # would leave the buttons locked for good.
        self._analyzing = False
        self._running = False
        # Paths dropped while either was going on. Drag and drop is not locked
        # with the buttons, so they used to start a second analysis mid-run
        # (switching Cancel off) or, during an analysis, vanish without a word.
        self._pending: list[str] = []

        saved = _saved_options(i18n.load_settings())
        self.var_font = tk.BooleanVar(value=saved.font)
        self.var_size = tk.BooleanVar(value=saved.size)
        self.var_line = tk.BooleanVar(value=saved.line)
        self.var_margin = tk.BooleanVar(value=saved.margin)
        self.var_remove_files = tk.BooleanVar(value=saved.remove_font_files)
        self.var_lang = tk.StringVar(value=i18n.LANG_NAMES[i18n.current_lang()])
        # The new-version check: what is remembered, the checkbox, and the notice
        # that is on screen (None when there is none).
        self.update_prefs = update_check.load_prefs(i18n.load_settings())
        self.var_update_check = tk.BooleanVar(value=self.update_prefs.update_check)
        self._update_info: update_check.UpdateInfo | None = None
        self._update_extra = 0  # how much taller the window got when the notice appeared
        self._feedback_tip: tk.Toplevel | None = None  # the link's tooltip while hovered

        self._build()
        self._apply_texts()
        self._fit_window(initial=True)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # Once a day, 1.5 s after the window is up, on a background thread, silent
        # on any failure. The window only: the command line never asks.
        update_check.check_async(self.root, UPDATE_SLUG, APP_VERSION, self.update_prefs,
                                 self._on_update_result)
        if initial_files:
            self.root.after(100, lambda: self.add_paths(initial_files))

    # ------------------------------------------------------------ building
    def _reg(self, widget: tk.Misc, key: str, attr: str = "text") -> tk.Misc:
        self._texts.append((widget, key, attr))
        return widget

    def _build(self) -> None:
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(2, weight=0)  # analysis and options keep their size
        root.rowconfigure(3, weight=1)  # the log takes the slack instead

        # ---- header
        head = ttk.Frame(root, padding=(12, 10, 12, 4))
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        self._reg(ttk.Label(head, font=("", 15, "bold")), "app_title").grid(row=0, column=0, sticky="w")
        # A link, not a form: it opens the feedback box on the tools page in the
        # browser and sends nothing itself. There is no menu bar to put it in.
        # Its font is the default one, underlined, made as a Tcl named font so it
        # scales with the display like the default does. Not a tkinter Font
        # object: that is deleted from whichever thread garbage-collects it, and
        # Tcl aborts the process for that (see the Variable teardown in the tests).
        root.tk.call("font", "create", "EfuLinkFont", *root.tk.splitlist(
            root.tk.call("font", "configure", "TkDefaultFont")))
        root.tk.call("font", "configure", "EfuLinkFont", "-underline", 1)
        ttk.Style(root).configure("Link.TLabel", foreground="#0b57d0", font="EfuLinkFont")
        # Reached with Tab, it has to show it: a label draws no focus ring, so the
        # background lights up instead. A colour only, never padding or a border,
        # so the header does not move when the focus comes and goes.
        ttk.Style(root).map("Link.TLabel", background=[("focus", LINK_FOCUS_BG)])
        self.lnk_feedback = ttk.Label(head, style="Link.TLabel", cursor="hand2", takefocus=True)
        self._reg(self.lnk_feedback, "btn_feedback")
        self.lnk_feedback.grid(row=0, column=1, padx=(0, 18))
        self.lnk_feedback.bind("<Button-1>", lambda _e: self.open_feedback())
        self.lnk_feedback.bind("<Return>", lambda _e: self.open_feedback())
        self.lnk_feedback.bind("<space>", lambda _e: self.open_feedback())
        self.lnk_feedback.bind("<Enter>", self._show_feedback_tip)
        self.lnk_feedback.bind("<Leave>", self._hide_feedback_tip)
        self.chk_update = ttk.Checkbutton(head, variable=self.var_update_check,
                                          command=self._on_update_toggle)
        self._reg(self.chk_update, "opt_update_check")
        self.chk_update.grid(row=0, column=2, padx=(0, 18))
        self._reg(ttk.Label(head), "lbl_language").grid(row=0, column=3, padx=(0, 6))
        self.cmb_lang = ttk.Combobox(
            head, state="readonly", width=10, textvariable=self.var_lang,
            values=[i18n.LANG_NAMES[c] for c in i18n.LANGS],
        )
        self.cmb_lang.grid(row=0, column=4)
        self.cmb_lang.bind("<<ComboboxSelected>>", self._on_lang)

        # ---- files
        files = self._reg(ttk.LabelFrame(root, padding=8), "lbl_files")
        files.grid(row=1, column=0, sticky="ew", padx=12, pady=4)
        files.columnconfigure(0, weight=1)
        self.lbl_drop = ttk.Label(files, anchor="center", relief="groove", padding=6, foreground="#555")
        self._reg(self.lbl_drop, "drop_hint")
        self.lbl_drop.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        self.lst_files = tk.Listbox(files, height=4, activestyle="none")
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

        analysis = self._reg(ttk.LabelFrame(mid, padding=8), "lbl_analysis")
        analysis.grid(row=0, column=0, sticky="ew")
        analysis.columnconfigure(0, weight=1)
        self.lbl_summary = ttk.Label(analysis, anchor="w", font=("", 11))
        self.lbl_summary.grid(row=0, column=0, sticky="ew")
        self.lbl_extra = ttk.Label(analysis, anchor="w", foreground="#555")
        self.lbl_extra.grid(row=1, column=0, sticky="ew", pady=(2, 0))

        opts = self._reg(ttk.LabelFrame(mid, padding=8), "lbl_options")
        opts.grid(row=1, column=0, sticky="ew", pady=(8, 0))
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
        logf.grid(row=3, column=0, sticky="nsew", padx=12, pady=4)
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(0, weight=1)
        self.txt_log = tk.Text(logf, height=5, state="disabled", wrap="none")
        self.txt_log.grid(row=0, column=0, sticky="nsew")
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

        # ---- new-version notice: one line under everything else, never a dialog.
        # It is built here but only gridded when there is news, so the layout above
        # it is the same with or without it; the window just grows by this row.
        self.frm_update = ttk.Frame(root, padding=(12, 0, 12, 10))
        self.frm_update.columnconfigure(0, weight=1)
        self.lbl_update = ttk.Label(self.frm_update, anchor="w")
        self.lbl_update.grid(row=0, column=0, sticky="ew")
        self.btn_update = ttk.Button(self.frm_update, command=self._update_open)
        self._reg(self.btn_update, "btn_update")
        self.btn_update.grid(row=0, column=1, padx=(8, 4))
        self.btn_update_later = ttk.Button(self.frm_update, command=self._update_later)
        self._reg(self.btn_update_later, "btn_update_later")
        self.btn_update_later.grid(row=0, column=2)

        self._set_status("status_ready")

    def _fit_window(self, initial: bool = False, shrink: int = 0) -> None:
        """Keep the window at least as big as the layout needs.

        Tk's grid does not shrink a row below what it asked for; it lets the
        content run off the bottom edge instead. A window one pixel too short
        therefore hides the Run button rather than tightening up, so the
        minimum size is taken from the built layout instead of guessed. The cap
        is for short screens, where something has to give.

        `shrink` gives back height a row added earlier (the new-version notice),
        never going below what the layout now needs.
        """
        self.root.update_idletasks()
        need_w = max(820, self.root.winfo_reqwidth())
        need_h = min(self.root.winfo_reqheight(), int(self.root.winfo_screenheight() * 0.92))
        self.root.minsize(need_w, need_h)
        if initial:
            self.root.geometry(f"{need_w}x{need_h}")
            return
        grow_w = max(need_w, self.root.winfo_width())
        grow_h = max(need_h, self.root.winfo_height() - shrink)
        if (grow_w, grow_h) != (self.root.winfo_width(), self.root.winfo_height()):
            self.root.geometry(f"{grow_w}x{grow_h}")

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
        if self._update_info is not None:  # the notice is worded in the current language
            self.lbl_update.configure(text=t("msg_update_available", latest=self._update_info.latest,
                                             current=self._update_info.current))

    # ------------------------------------------------------------ helpers
    def _post(self, fn, *args, **kwargs) -> None:
        """Hand a piece of work back to the UI thread.

        Tk only accepts after() from another thread while the main loop is
        running; once the window is on its way out it raises instead. A worker
        finishing its last book must not die on that, so the call is guarded
        and the result simply goes nowhere.
        """
        try:
            self.root.after(0, lambda: fn(*args, **kwargs))
        except (RuntimeError, tk.TclError):  # pragma: no cover - teardown only
            pass

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
        self._fit_window()  # a longer translation must not push Run off the edge

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
        if self._analyzing:  # the flag, not the thread: it can outlive its last callback
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

    # ------------------------------------------------------- new version notice
    def _save_update_prefs(self) -> None:
        """Write the three update keys into settings.json, leaving every other key
        (language, options) as it is."""
        self.update_prefs.update_check = bool(self.var_update_check.get())
        settings = i18n.load_settings()
        update_check.store_prefs(self.update_prefs, settings)
        i18n.save_settings(settings)

    def _on_update_result(self, info: update_check.UpdateInfo | None, answered: bool) -> None:
        """Runs on the Tk thread. The day is recorded only when the server
        answered, so an offline start asks again next time."""
        if self._closing:
            return
        if answered:
            self.update_prefs.update_check_last = update_check.today()
            self._save_update_prefs()
        if info is not None and self.var_update_check.get():
            self._update_info = info
            self._show_update_notice()

    def _show_update_notice(self) -> None:
        info = self._update_info
        if info is None:
            self._hide_update_notice()
            return
        self.lbl_update.configure(text=t("msg_update_available", latest=info.latest,
                                         current=info.current))
        if self.frm_update.winfo_manager():
            return
        self.frm_update.grid(row=5, column=0, sticky="ew")
        self.root.update_idletasks()
        self._update_extra = self.frm_update.winfo_reqheight()
        self._fit_window()  # the window grows by this row; nothing above it moves

    def _hide_update_notice(self) -> None:
        self._update_info = None
        if self.frm_update.winfo_manager():
            self.frm_update.grid_remove()
            extra, self._update_extra = self._update_extra, 0
            self._fit_window(shrink=extra)

    def _update_open(self) -> None:
        """The tools page, never a download: the site serves that itself. Only a
        page on the site is ever opened, whatever the server's answer held. A
        window in any language but Korean opens the English page when the server
        gave one; the language is the one showing at the moment of the click."""
        info = self._update_info
        if info is not None:
            url = update_check.page_for(info, i18n.current_lang())
            if url.startswith(update_check.SITE):
                try:
                    webbrowser.open(url)
                except Exception:
                    pass
        self._hide_update_notice()

    def _update_later(self) -> None:
        if self._update_info is not None:
            self.update_prefs.update_check_skipped = self._update_info.latest  # a newer one shows again
            self._save_update_prefs()
        self._hide_update_notice()

    def _on_update_toggle(self) -> None:
        self._save_update_prefs()
        if not self.var_update_check.get():
            self._hide_update_notice()

    # ------------------------------------------------------------ feedback
    def _show_feedback_tip(self, _event=None) -> None:
        """A tooltip under the link, while the pointer is on it. Its own little
        window: the status line is too short for the sentence, and it would push
        nothing around."""
        self._hide_feedback_tip()
        tip = tk.Toplevel(self.root)
        tip.wm_overrideredirect(True)
        ttk.Label(tip, text=t("tip_feedback"), padding=(6, 3), background="#ffffe1",
                  relief="solid", borderwidth=1).pack()
        tip.update_idletasks()
        link = self.lnk_feedback
        x, y = _tip_position(link.winfo_rootx(), link.winfo_rooty(), link.winfo_width(),
                             link.winfo_height(), tip.winfo_reqwidth(), tip.winfo_reqheight(),
                             _screen_area(link),
                             (self.root.winfo_rootx(), self.root.winfo_rootx() + self.root.winfo_width()))
        tip.wm_geometry(f"+{x}+{y}")
        self._feedback_tip = tip

    def _hide_feedback_tip(self, _event=None) -> None:
        tip, self._feedback_tip = self._feedback_tip, None
        if tip is not None:
            try:
                tip.destroy()
            except tk.TclError:
                pass

    def open_feedback(self) -> None:
        """The feedback box on the tools page, in the language showing at the
        moment of the click. When no browser opens, the address goes into the
        log, where it can be selected and copied, and a message says so."""
        self._hide_feedback_tip()
        url = update_check.feedback_url(UPDATE_SLUG, APP_VERSION, i18n.current_lang())
        try:
            opened = webbrowser.open(url)
        except Exception:  # webbrowser.Error, OSError from a broken handler ...
            opened = False
        if not opened:
            self.log("log_feedback_url", url=url)
            messagebox.showwarning(t("btn_feedback"), t("msg_feedback_failed", url=url),
                                   parent=self.root)

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

    @staticmethod
    def _same_file(a: str, b: str) -> bool:
        """Windows paths differ in case and slashes without being different files."""
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))

    def _is_known(self, path: str) -> bool:
        return any(self._same_file(path, other) for other in self.files + self._pending)

    def add_paths(self, paths: list[str]) -> None:
        """Analysis reads the whole book, so it runs off the UI thread. Files
        appear in the list only once they have been read successfully.

        While an analysis or a run is going on the paths are held and picked up
        afterwards; starting another analysis then would fight over the controls
        and the file table."""
        found = [p for p in unlock.collect_epubs(paths) if not self._is_known(p)]
        if not found:
            self.log("err_no_epub_found")
            return
        if self._analyzing or self._running:
            self._pending.extend(found)
            self.log("log_queued", count=len(found))
            return
        self._start_analysis(found)

    def _start_analysis(self, found: list[str]) -> None:
        self.analyze_gen += 1
        gen = self.analyze_gen
        self._analyzing = True
        self._refresh_controls()

        def work() -> None:
            refused: list[tuple[str, str]] = []  # (path, message key) for one summary
            for path in found:
                if gen != self.analyze_gen:
                    break
                try:
                    analysis = unlock.analyze_epub(path)
                except unlock.DrmProtected:
                    refused.append((path, "err_drm"))
                    self._post(self.log, "log_drm", name=os.path.basename(path))
                    continue
                except Exception:  # broken zip, unreadable file, out of memory ...
                    refused.append((path, "err_open_failed"))
                    self._post(self.log, "log_skipped", name=os.path.basename(path))
                    continue
                self._post(self._add_done, gen, analysis)
            self._post(self._analysis_finished, refused)

        self.analyzer = threading.Thread(target=work, daemon=True)
        self.analyzer.start()
        self._update_analysis_labels()

    # A folder can hold dozens of books that cannot be read. One dialog per book
    # meant dozens of presses of OK; the log has every name, the dialog a few.
    MAX_REFUSED_SHOWN = 8

    def _report_refused(self, refused: list[tuple[str, str]]) -> None:
        lines = []
        for path, key in refused[: self.MAX_REFUSED_SHOWN]:
            name = os.path.basename(path)
            lines.append(f"{name}: {t(key)}" if key == "err_drm" else t(key, name=name))
        if len(refused) > self.MAX_REFUSED_SHOWN:
            lines.append(f"... +{len(refused) - self.MAX_REFUSED_SHOWN}")
        messagebox.showerror(t("dlg_error"), "\n".join(lines), parent=self.root)

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

    def _analysis_finished(self, refused: list[tuple[str, str]]) -> None:
        # Unconditional: even an analysis whose results were thrown away has to
        # give the controls back, or the window stays locked.
        self._analyzing = False
        self._refresh_controls()
        self._update_analysis_labels()
        if refused and not self._closing:
            self._report_refused(refused)
        self._drain_pending()

    def _drain_pending(self) -> None:
        """Take up what was dropped while the window was busy, once it is not."""
        if self._pending and not (self._analyzing or self._running or self._closing):
            paths, self._pending = self._pending, []
            self.add_paths(paths)

    def clear_files(self) -> None:
        if self._running:
            return  # the worker is reading these; Clear is locked, and stays so
        self.analyze_gen += 1  # any analysis still running now belongs to nothing
        self._pending.clear()
        self.files.clear()
        self.analyses.clear()
        self.lst_files.delete(0, "end")
        self._update_analysis_labels()

    # ------------------------------------------------------------ running
    def run(self) -> None:
        if self._running or self._analyzing:
            return  # the list is still changing, or a run is already under way
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
        # The worker gets its own copy. Reading self.analyses from the thread
        # meant a Clear on the UI thread could empty it under the worker's feet.
        weights = {p: max(1, self.analyses[p].css_files + self.analyses[p].xhtml_files)
                   for p in files}
        self.progress.configure(maximum=sum(weights.values()), value=0)
        self._running = True
        self._refresh_controls()

        def work() -> None:
            done = 0
            count = skipped = 0
            totals = unlock.Counts()
            for index, path in enumerate(files, 1):
                name = os.path.basename(path)
                base = done

                def progress(i: int, n: int, _item: str, _base=base, _idx=index, _name=name) -> None:
                    self._post(self._on_progress, _base + i, _idx, len(files), _name)

                try:
                    result = unlock.process_epub(path, opts, progress=progress, cancel=self.cancel_event)
                except unlock.Cancelled:
                    self._post(self.log, "log_cancelled", name=name)
                    self._post(self._finished, True, count, skipped, totals)
                    return
                except unlock.DrmProtected:
                    self._post(self.log, "log_drm", name=name)
                    skipped += 1
                    continue
                except Exception as exc:  # one bad book must not end the batch
                    self._post(self.log, "err_file_failed", name=name, error=str(exc))
                    skipped += 1
                    continue
                finally:
                    done = base + weights[path]
                    self._post(self._set_progress, done)
                for item, error in result.failed:
                    self._post(self.log, "err_css_failed", name=item, error=error)
                if result.already_unlocked:
                    self._post(self.log, "log_already", name=name)
                    skipped += 1
                    continue
                c = result.counts
                totals.fonts += c.fonts
                totals.sizes += c.sizes
                totals.lines += c.lines
                totals.margins += c.margins
                count += 1
                self.outputs.append(result.output_path)
                self._post(self.log, "log_saved", path=result.output_path)
            self._post(self._finished, False, count, skipped, totals)

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _set_progress(self, value: int) -> None:
        self.progress.configure(value=value)

    def _on_progress(self, done: int, file_index: int, files: int, name: str) -> None:
        self.progress.configure(value=done)
        self._set_status("status_processing", file=file_index, files=files, name=name)

    def _finished(self, cancelled: bool, count: int, skipped: int, totals: unlock.Counts) -> None:
        self._running = False
        self._refresh_controls()
        if cancelled or self._closing:
            self._set_status("status_cancelled")
            self._drain_pending()
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
        self._drain_pending()

    def cancel(self) -> None:
        self.cancel_event.set()
        self.btn_cancel.configure(state="disabled")

    def _refresh_controls(self) -> None:
        """Set every button from what the window is doing -- never from what the
        last caller happened to pass, which is how Cancel got switched off in the
        middle of a run and Clear switched back on."""
        busy = self._analyzing or self._running
        for w in (self.btn_run, self.btn_add, self.btn_add_dir, self.btn_clear):
            w.configure(state="disabled" if busy else "normal")
        self.cmb_lang.configure(state="disabled" if busy else "readonly")
        cancellable = self._running and not self.cancel_event.is_set()
        self.btn_cancel.configure(state="normal" if cancellable else "disabled")
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
        self._pending.clear()
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
