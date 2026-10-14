"""The window itself, driven for real.

These exist because a layout bug shipped: the option boxes collapsed to nine
pixels tall at the default window size, and a window one pixel too short hid
the Run button, because Tk's grid clips rather than shrinks. Nothing in the
suite was looking at the window, so nothing caught it.

Everything runs inside a real main loop. That is not a detail: Tk only accepts
after() from a worker thread while the loop is running, so a test that pumps
update() in a while-loop instead makes the analysis thread die on a
RuntimeError and proves nothing. Each test builds a window, hands the driver a
chain of steps, and the loop ends when the last step calls done().

Dialogs are stubbed so nothing blocks, and settings.json is pointed at a
temporary file so the developer's own settings are left alone.
"""
from __future__ import annotations

import gc
import os
import shutil
import sys
import tempfile
import time
import tkinter as tk
import unittest

import fixtures  # noqa: E402  (inserts the repo root on sys.path)

import i18n  # noqa: E402
import unlock  # noqa: E402
import update_check  # noqa: E402

try:
    _probe = tk.Tk()
    _probe.destroy()
    HAS_DISPLAY = True
except Exception:  # pragma: no cover - headless machine
    HAS_DISPLAY = False

import gui  # noqa: E402
from version import APP_VERSION  # noqa: E402


def update_check_version() -> str:
    """The version the window reports, and shows in "(현재 ...)"."""
    return APP_VERSION


# Frames and scrolling widgets may be smaller than they asked for; they scroll
# or they are just containers. Everything else must fit.
ELASTIC = {"Text", "Listbox", "TFrame", "Frame", "Tk", "Toplevel"}
TIMEOUT = 30.0


@unittest.skipUnless(HAS_DISPLAY, "no display")
class GuiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="efu_gui_")
        self.addCleanup(shutil.rmtree, self.dir, True)

        # Beside the books, not among them: listed() must show only .epub files.
        config = os.path.join(self.dir, "config")
        os.makedirs(config)
        self.settings_path = os.path.join(config, "settings.json")
        original = i18n.SETTINGS_PATH
        i18n.SETTINGS_PATH = self.settings_path
        self.addCleanup(setattr, i18n, "SETTINGS_PATH", original)

        self.dialogs: list[tuple[str, str]] = []
        for name, answer in (("showinfo", None), ("showerror", None), ("askyesno", True)):
            self.addCleanup(setattr, gui.messagebox, name, getattr(gui.messagebox, name))

            def stub(*args, _name=name, _answer=answer, **kwargs):
                self.dialogs.append((_name, str(args[1]) if len(args) > 1 else ""))
                return _answer

            setattr(gui.messagebox, name, stub)

        # The new-version check: no window here talks to the real server. What the
        # "server" answers is set per test; by default it does not answer at all.
        self.asked: list[tuple[str, str]] = []
        self.answer: dict | None = None
        self.opened: list[str] = []

        def fake_fetch(slug, version, **kwargs):
            self.asked.append((slug, version))
            return self.answer

        self.addCleanup(setattr, update_check, "fetch", update_check.fetch)
        update_check.fetch = fake_fetch
        self.addCleanup(setattr, gui.webbrowser, "open", gui.webbrowser.open)
        gui.webbrowser.open = lambda url, *a, **k: self.opened.append(url) or True
        fixtures.NETWORK_ATTEMPTS.clear()
        self.addCleanup(lambda: self.assertEqual(fixtures.NETWORK_ATTEMPTS, []))

        i18n.init("ko")
        self.addCleanup(i18n.init, "en")
        self._failure: BaseException | None = None

    # -------------------------------------------------------------- driver
    def app(self, files: list[str] | None = None) -> gui.App:
        app = gui.App(files)
        self.addCleanup(self.destroy, app)
        return app

    def destroy(self, app: gui.App) -> None:
        """Tear a window down without leaving a thread behind.

        A worker that calls after() needs the main loop of its own interpreter
        to be running; once the window is gone it blocks instead, holding the
        lock that _tkinter shares across every interpreter in the process. One
        such thread left over from an earlier test hangs the next one. So the
        loop is run a little longer, on purpose, until the threads are done.
        """
        app._closing = True
        app.cancel_event.set()
        app.analyze_gen += 1

        def working() -> bool:
            return any(t is not None and t.is_alive() for t in (app.analyzer, app.worker))

        if working():
            deadline = time.time() + 15

            def tick() -> None:
                if not working() or time.time() > deadline:
                    try:
                        app.root.quit()
                    except tk.TclError:
                        pass
                    return
                app.root.after(20, tick)

            try:
                app.root.after(0, tick)
                app.root.mainloop()
            except tk.TclError:
                pass
        for thread in (app.analyzer, app.worker):
            if thread is not None:
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive(), "a worker outlived its window")
        # Drop the tk Variables while their interpreter is still alive. Left to
        # the garbage collector they are finalized later, from whichever thread
        # happens to be collecting, and Tcl kills the process for it.
        for name in ("var_font", "var_size", "var_line", "var_margin",
                     "var_remove_files", "var_lang"):
            setattr(app, name, None)
        app._texts.clear()
        try:  # pending after() callbacks would fire into a dead interpreter
            for ident in app.root.tk.splitlist(app.root.tk.call("after", "info")):
                app.root.after_cancel(ident)
        except tk.TclError:
            pass
        gc.collect()
        try:
            app.root.destroy()
        except tk.TclError:
            pass

    def done(self, app: gui.App) -> None:
        try:
            app.root.quit()
        except tk.TclError:
            pass

    def guard(self, app: gui.App, fn) -> None:
        """Run a step, keeping any assertion failure for after the loop."""
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised in loop()
            self._failure = exc
            self.done(app)

    def loop(self, app: gui.App, first) -> None:
        """Run the main loop, starting with `first`, until a step calls done."""
        deadline = time.time() + TIMEOUT

        def watchdog() -> None:
            if time.time() > deadline:
                self._failure = self._failure or AssertionError("timed out")
                self.done(app)
                return
            try:
                if app.root.winfo_exists():
                    app.root.after(100, watchdog)
            except tk.TclError:
                pass

        app.root.after(0, lambda: self.guard(app, first))
        app.root.after(100, watchdog)
        app.root.mainloop()
        if self._failure is not None:
            raise self._failure

    def snapshot(self, app: gui.App) -> str:
        def alive(thread):
            return None if thread is None else thread.is_alive()
        try:
            state = str(app.btn_run["state"])
            status = app.lbl_status.cget("text")
        except tk.TclError:
            state = status = "<destroyed>"
        return (f"analyzer={alive(app.analyzer)} worker={alive(app.worker)} "
                f"gen={app.analyze_gen} files={len(app.files)} run_btn={state} "
                f"status={status!r} dialogs={self.dialogs}")

    def wait(self, app: gui.App, ready, then, what: str = "condition") -> None:
        """Poll `ready` from inside the loop, then run `then`."""
        deadline = time.time() + TIMEOUT

        def tick() -> None:
            try:
                if ready():
                    self.guard(app, then)
                    return
                if time.time() > deadline:
                    self._failure = AssertionError(
                        f"timed out waiting for {what}: {self.snapshot(app)}")
                    self.done(app)
                    return
                app.root.after(20, tick)
            except tk.TclError:
                pass

        tick()

    def after_analysis(self, app: gui.App, then) -> None:
        """Wait for _analysis_finished, not just for the thread.

        The thread dying only means the last after() has been queued; the test
        must see the callback that consumed it. _analysis_finished is the one
        that re-enables the buttons, so that is the signal.
        """
        def ready() -> bool:
            return (app.analyzer is not None and not app.analyzer.is_alive()
                    and str(app.btn_run["state"]) == "normal")

        self.wait(app, ready, lambda: self.guard(app, then), "the analysis")

    def after_run(self, app: gui.App, then) -> None:
        """Wait for _finished, which is the last callback the worker posts."""
        finished = {i18n.t("status_done"), i18n.t("status_cancelled")}

        def ready() -> bool:
            return (app.worker is not None and not app.worker.is_alive()
                    and app.lbl_status.cget("text") in finished)

        self.wait(app, ready, lambda: self.guard(app, then), "the run")

    # -------------------------------------------------------------- helpers
    @staticmethod
    def widgets(app: gui.App):
        def walk(widget):
            yield widget
            for child in widget.winfo_children():
                yield from walk(child)
        return walk(app.root)

    def clipping(self, app: gui.App, label: str) -> list[str]:
        app.root.update_idletasks()
        bad = []
        for w in self.widgets(app):
            try:
                got_w, got_h = w.winfo_width(), w.winfo_height()
                need_w, need_h = w.winfo_reqwidth(), w.winfo_reqheight()
                text = str(w.cget("text")) if "text" in w.keys() else ""
            except tk.TclError:
                continue
            if got_w <= 1 or got_h <= 1:
                continue
            if need_w > got_w + 1 and text:
                bad.append(f"{label}: {w.winfo_class()} {text!r} width {got_w} < {need_w}")
            if need_h > got_h + 1 and w.winfo_class() not in ELASTIC:
                bad.append(f"{label}: {w.winfo_class()} {text!r} height {got_h} < {need_h}")
        return bad

    def book(self, name: str = "book.epub") -> str:
        return fixtures.standard(os.path.join(self.dir, name))

    def listed(self) -> list[str]:
        return sorted(n for n in os.listdir(self.dir) if n != "config")

    # ---------------------------------------------------------------- layout
    def test_nothing_is_clipped_in_any_language_at_either_size(self) -> None:
        app = self.app([self.book()])

        def check() -> None:
            bad: list[str] = []
            sizes = [None, "%dx%d" % app.root.minsize()]
            for size in sizes:
                if size:
                    app.root.geometry(size)
                for index, lang in enumerate(i18n.LANGS):
                    app.cmb_lang.current(index)
                    app._on_lang()
                    bad += self.clipping(app, f"{size or 'default'}/{lang}")
            self.assertEqual(bad, [])
            self.done(app)

        self.loop(app, lambda: self.after_analysis(app, check))

    def test_every_option_box_is_visible(self) -> None:
        """The bug that started this file: the whole option group collapsed."""
        app = self.app()

        def check() -> None:
            app.root.update_idletasks()
            boxes = [w for w in self.widgets(app) if w.winfo_class() == "TCheckbutton"]
            # five things to unlock, and the new-version checkbox in the header
            self.assertEqual(len(boxes), 6)
            self.assertIn(app.chk_update, boxes)
            for box in boxes:
                self.assertTrue(box.winfo_ismapped(), box.cget("text"))
                self.assertGreater(box.winfo_height(), 5, box.cget("text"))
                self.assertGreaterEqual(box.winfo_height(), box.winfo_reqheight(),
                                        box.cget("text"))
            self.done(app)

        self.loop(app, check)

    def test_the_window_is_never_shorter_than_its_content(self) -> None:
        app = self.app()

        def check() -> None:
            app.root.update_idletasks()
            min_w, min_h = app.root.minsize()
            self.assertGreaterEqual(min_h, app.root.winfo_reqheight())
            self.assertGreaterEqual(min_w, app.root.winfo_reqwidth())
            self.done(app)

        self.loop(app, check)

    def test_the_run_button_stays_on_screen_at_the_minimum_size(self) -> None:
        app = self.app()

        def check() -> None:
            app.root.geometry("%dx%d" % app.root.minsize())
            app.root.update_idletasks()
            bottom = app.btn_run.winfo_rooty() + app.btn_run.winfo_height()
            self.assertLessEqual(bottom, app.root.winfo_rooty() + app.root.winfo_height())
            self.done(app)

        self.loop(app, check)

    def test_a_longer_translation_keeps_the_minimum_size_honest(self) -> None:
        app = self.app()

        def check() -> None:
            for index in range(len(i18n.LANGS)):
                app.cmb_lang.current(index)
                app._on_lang()
                app.root.update_idletasks()
                self.assertGreaterEqual(app.root.minsize()[1], app.root.winfo_reqheight())
                self.assertGreaterEqual(app.root.minsize()[0], app.root.winfo_reqwidth())
            self.done(app)

        self.loop(app, check)

    def test_nothing_is_clipped_at_any_display_scale(self) -> None:
        """Windows scales text 100-200% depending on the screen. `tk scaling` is
        points-to-pixels, so setting it re-lays the window out the way that
        scale would, whatever this machine happens to be set to."""
        app = self.app([self.book()])

        def check() -> None:
            bad: list[str] = []
            for percent, scaling in ((100, 96 / 72), (125, 120 / 72), (150, 144 / 72),
                                     (200, 192 / 72)):
                app.root.tk.call("tk", "scaling", scaling)
                for index, lang in enumerate(i18n.LANGS):
                    app.cmb_lang.current(index)
                    app._on_lang()  # relabels and refits the window
                    app.root.update_idletasks()
                    label = f"{percent}%/{lang}"
                    bad += self.clipping(app, label)
                    self.assertGreaterEqual(app.root.winfo_height(), app.root.winfo_reqheight(),
                                            f"{label}: window shorter than its content")
                    bottom = app.btn_run.winfo_rooty() + app.btn_run.winfo_height()
                    self.assertLessEqual(bottom, app.root.winfo_rooty() + app.root.winfo_height(),
                                         f"{label}: Run button pushed off the window")
            self.assertEqual(bad, [])
            self.done(app)

        self.loop(app, lambda: self.after_analysis(app, check))

    def test_the_window_asks_windows_for_real_pixels(self) -> None:
        """An unaware window is drawn small and stretched, so it comes out blurry
        on a scaled display. The shipped exe was exactly that."""
        if sys.platform != "win32":
            self.skipTest("Windows only")
        import ctypes
        import ctypes.wintypes as wt
        app = self.app()

        def check() -> None:
            # Typed explicitly: the pseudo-handle for "this process" is -1, and
            # ctypes would pass it as a 32-bit int, which the call then rejects.
            kernel32, shcore = ctypes.windll.kernel32, ctypes.windll.shcore
            kernel32.GetCurrentProcess.restype = wt.HANDLE
            shcore.GetProcessDpiAwareness.argtypes = [wt.HANDLE, ctypes.POINTER(ctypes.c_int)]
            awareness = ctypes.c_int(-1)
            result = shcore.GetProcessDpiAwareness(kernel32.GetCurrentProcess(),
                                                   ctypes.byref(awareness))
            self.assertEqual(result, 0, f"the awareness query itself failed: {result:#x}")
            self.assertGreaterEqual(awareness.value, 1, "the process is DPI unaware")
            self.done(app)

        self.loop(app, check)

    # -------------------------------------------------------------- language
    def test_switching_language_relabels_everything(self) -> None:
        app = self.app()

        def check() -> None:
            titles = {}
            for index, lang in enumerate(i18n.LANGS):
                app.cmb_lang.current(index)
                app._on_lang()
                app.root.update_idletasks()
                titles[lang] = app.root.title()
                for widget, key, attr in app._texts:
                    value = str(widget.cget(attr))
                    self.assertNotEqual(value, key, f"{lang}: {key} never translated")
                    self.assertTrue(value.strip(), f"{lang}: {key} is empty")
            self.assertEqual(len(set(titles.values())), len(i18n.LANGS), titles)
            self.done(app)

        self.loop(app, check)

    def test_the_language_choice_is_remembered(self) -> None:
        app = self.app()

        def check() -> None:
            app.cmb_lang.current(i18n.LANGS.index("ja"))
            app._on_lang()
            self.done(app)

        self.loop(app, check)
        self.assertEqual(i18n.load_settings().get("lang"), "ja")

    # ----------------------------------------------------------------- files
    def test_a_book_shows_its_own_analysis_in_the_list(self) -> None:
        app = self.app([self.book()])

        def check() -> None:
            self.assertEqual(len(app.files), 1)
            line = app.lst_files.get(0)
            self.assertIn("book.epub", line)
            self.assertIn("고정 글꼴", line)
            self.assertIn("고정 글꼴", app.lbl_summary.cget("text"))
            self.assertIn("인라인 style", app.lbl_extra.cget("text"))
            self.assertIn("추가: book.epub", app.txt_log.get("1.0", "end"))
            self.done(app)

        self.loop(app, lambda: self.after_analysis(app, check))

    def test_two_books_are_summed_in_the_analysis(self) -> None:
        app = self.app([self.book("one.epub"), self.book("two.epub")])

        def check() -> None:
            self.assertEqual(len(app.files), 2)
            one = unlock.analyze_epub(app.files[0]).counts
            self.assertIn(str(one.sizes * 2), app.lbl_summary.cget("text"))
            self.done(app)

        self.loop(app, lambda: self.after_analysis(app, check))

    def test_a_drm_book_is_refused_with_a_dialog(self) -> None:
        drm = fixtures.build(os.path.join(self.dir, "drm.epub"),
                             {"OEBPS/style.css": fixtures.CSS, "OEBPS/ch1.xhtml": fixtures.XHTML},
                             encryption=True)
        app = self.app([drm])

        def check() -> None:
            self.assertEqual(app.files, [])
            self.assertTrue(any(kind == "showerror" and "DRM" in text
                                for kind, text in self.dialogs), self.dialogs)
            self.assertEqual(self.listed(), ["drm.epub"])
            self.done(app)

        self.loop(app, lambda: self.after_analysis(app, check))

    def test_a_broken_book_is_refused_with_a_dialog(self) -> None:
        broken = os.path.join(self.dir, "broken.epub")
        open(broken, "wb").close()
        app = self.app([broken])

        def check() -> None:
            self.assertEqual(app.files, [])
            self.assertTrue(any(kind == "showerror" for kind, _ in self.dialogs), self.dialogs)
            self.done(app)

        self.loop(app, lambda: self.after_analysis(app, check))

    def test_a_good_book_still_loads_when_a_bad_one_is_dropped_with_it(self) -> None:
        broken = os.path.join(self.dir, "broken.epub")
        open(broken, "wb").close()
        app = self.app([broken, self.book("good.epub")])

        def check() -> None:
            self.assertEqual([os.path.basename(p) for p in app.files], ["good.epub"])
            self.done(app)

        self.loop(app, lambda: self.after_analysis(app, check))

    def test_clearing_forgets_the_files_and_the_analysis(self) -> None:
        app = self.app([self.book()])

        def check() -> None:
            app.clear_files()
            app.root.update_idletasks()
            self.assertEqual(app.files, [])
            self.assertEqual(app.analyses, {})
            self.assertEqual(app.lst_files.size(), 0)
            self.assertEqual(app.lbl_summary.cget("text"), i18n.t("analysis_empty"))
            self.done(app)

        self.loop(app, lambda: self.after_analysis(app, check))

    def test_adding_a_folder_finds_the_books_in_it(self) -> None:
        os.makedirs(os.path.join(self.dir, "sub"))
        fixtures.standard(os.path.join(self.dir, "sub", "in_folder.epub"))
        app = self.app()

        def start() -> None:
            app.add_paths([os.path.join(self.dir, "sub")])
            self.after_analysis(app, check)

        def check() -> None:
            self.assertEqual([os.path.basename(p) for p in app.files], ["in_folder.epub"])
            self.done(app)

        self.loop(app, start)

    def test_the_same_book_is_not_added_twice(self) -> None:
        book = self.book()
        app = self.app([book])

        def again() -> None:
            app.add_paths([book])
            self.after_analysis(app, check)

        def check() -> None:
            self.assertEqual(len(app.files), 1)
            self.done(app)

        self.loop(app, lambda: self.after_analysis(app, again))

    # -------------------------------------------------------------- the run
    def test_running_writes_the_output_and_says_so(self) -> None:
        app = self.app([self.book()])

        def start() -> None:
            self.assertEqual(self.listed(), ["book.epub"])  # nothing written yet
            app.run()
            self.after_run(app, check)

        def check() -> None:
            self.assertEqual(len(app.outputs), 1)
            self.assertTrue(os.path.exists(app.outputs[0]))
            self.assertEqual(os.path.basename(app.outputs[0]), "book_unlocked.epub")
            self.assertTrue(any(kind == "showinfo" and "완료" in text
                                for kind, text in self.dialogs), self.dialogs)
            self.assertEqual(str(app.btn_open["state"]), "normal")
            self.assertEqual(app.lbl_status.cget("text"), i18n.t("status_done"))
            self.done(app)

        self.loop(app, lambda: self.after_analysis(app, start))

    def test_the_summary_dialog_counts_what_was_removed(self) -> None:
        app = self.app([self.book()])

        def start() -> None:
            app.var_margin.set(True)
            app.run()
            self.after_run(app, check)

        def check() -> None:
            counts = unlock.analyze_epub(app.files[0]).counts
            wanted = i18n.t("msg_changes", fonts=counts.fonts, sizes=counts.sizes,
                            lines=counts.lines, margins=counts.margins)
            info = [text for kind, text in self.dialogs if kind == "showinfo"]
            self.assertTrue(any(wanted in text for text in info), (wanted, info))
            self.done(app)

        self.loop(app, lambda: self.after_analysis(app, start))

    def test_an_already_unlocked_book_produces_no_file(self) -> None:
        fixtures.already_unlocked(os.path.join(self.dir, "plain.epub"))
        app = self.app([os.path.join(self.dir, "plain.epub")])

        def start() -> None:
            app.run()
            self.after_run(app, check)

        def check() -> None:
            self.assertEqual(app.outputs, [])
            self.assertEqual(self.listed(), ["plain.epub"])
            self.assertIn(i18n.t("msg_already"), app.lbl_summary.cget("text"))
            self.done(app)

        self.loop(app, lambda: self.after_analysis(app, start))

    def test_running_with_nothing_added_asks_for_files(self) -> None:
        app = self.app()

        def check() -> None:
            app.run()
            self.assertIsNone(app.worker)
            self.assertTrue(any(i18n.t("msg_no_files") in text for _, text in self.dialogs),
                            self.dialogs)
            self.done(app)

        self.loop(app, check)

    def test_running_with_every_option_off_refuses(self) -> None:
        app = self.app([self.book()])

        def check() -> None:
            for var in (app.var_font, app.var_size, app.var_line, app.var_margin,
                        app.var_remove_files):
                var.set(False)
            app.run()
            self.assertIsNone(app.worker)
            self.assertTrue(any(i18n.t("msg_no_options") in text for _, text in self.dialogs),
                            self.dialogs)
            self.assertEqual(self.listed(), ["book.epub"])
            self.done(app)

        self.loop(app, lambda: self.after_analysis(app, check))

    def test_controls_are_locked_while_the_analysis_runs(self) -> None:
        app = self.app()

        def start() -> None:
            app.add_paths([self.book()])
            self.assertEqual(str(app.btn_run["state"]), "disabled")
            self.assertEqual(str(app.cmb_lang["state"]), "disabled")
            self.after_analysis(app, check)

        def check() -> None:
            self.assertEqual(str(app.btn_run["state"]), "normal")
            self.assertEqual(str(app.cmb_lang["state"]), "readonly")
            self.done(app)

        self.loop(app, start)

    def test_a_cancelled_run_writes_nothing(self) -> None:
        """Cancel pressed while a book is being processed.

        Setting the flag before Run would prove nothing: run() clears it, as it
        must. So the flag is set from inside, on the first item of the first
        book, which is what the Cancel button does.
        """
        app = self.app([self.book()])
        real = unlock.process_epub

        def cancel_midway(path, opts, output_path=None, progress=None, cancel=None):
            def spy(index, total, item):
                cancel.set()
                if progress is not None:
                    progress(index, total, item)

            return real(path, opts, output_path=output_path, progress=spy, cancel=cancel)

        unlock.process_epub = cancel_midway
        self.addCleanup(setattr, unlock, "process_epub", real)

        def start() -> None:
            app.run()
            self.after_run(app, check)

        def check() -> None:
            self.assertEqual(app.outputs, [])
            self.assertEqual(self.listed(), ["book.epub"])
            self.assertEqual(app.lbl_status.cget("text"), i18n.t("status_cancelled"))
            self.assertIn(i18n.t("log_cancelled", name="book.epub"),
                          app.txt_log.get("1.0", "end"))
            self.done(app)

        self.loop(app, lambda: self.after_analysis(app, start))

    def test_cancel_sets_the_flag_and_disables_the_button(self) -> None:
        app = self.app()

        def check() -> None:
            app.btn_cancel.configure(state="normal")
            app.cancel()
            self.assertTrue(app.cancel_event.is_set())
            self.assertEqual(str(app.btn_cancel["state"]), "disabled")
            self.done(app)

        self.loop(app, check)

    # ------------------------------------------- dropping files while busy
    # The buttons lock while work is going on, but drag and drop does not, so a
    # drop can arrive at any moment. Each of these was a real failure.
    def gate(self, name: str):
        """Hold unlock.<name> until the test lets it go, so a run or an analysis
        can be kept in flight while the window is poked at."""
        import threading
        release = threading.Event()
        real = getattr(unlock, name)

        def held(*args, **kwargs):
            release.wait(20)
            return real(*args, **kwargs)

        setattr(unlock, name, held)
        self.addCleanup(setattr, unlock, name, real)
        self.addCleanup(release.set)
        return release

    def test_a_drop_during_a_run_leaves_cancel_working(self) -> None:
        app = self.app([self.book("one.epub")])
        release = self.gate("process_epub")
        other = self.book("two.epub")

        def start() -> None:
            app.run()
            app.add_paths([other])  # arrives while the run is in flight
            app.root.after(300, check)

        def check() -> None:
            self.assertTrue(app.worker.is_alive())
            self.assertEqual(str(app.btn_cancel["state"]), "normal", "Cancel was switched off")
            for button in (app.btn_run, app.btn_clear, app.btn_add, app.btn_add_dir):
                self.assertEqual(str(button["state"]), "disabled")
            self.assertEqual(str(app.cmb_lang["state"]), "disabled")
            release.set()
            self.after_run(app, finish)

        def finish() -> None:
            self.assertEqual(len(app.outputs), 1)
            self.done(app)

        self.loop(app, lambda: self.after_analysis(app, start))

    def test_a_drop_during_a_run_is_added_once_the_run_ends(self) -> None:
        app = self.app([self.book("one.epub")])
        release = self.gate("process_epub")
        other = self.book("two.epub")

        def start() -> None:
            app.run()
            app.add_paths([other])
            app.root.after(200, release.set)
            self.after_run(app, wait_for_it)

        def wait_for_it() -> None:
            self.wait(app, lambda: len(app.files) == 2 and str(app.btn_run["state"]) == "normal",
                      check, "the queued book to be analysed")

        def check() -> None:
            self.assertEqual([os.path.basename(p) for p in app.files], ["one.epub", "two.epub"])
            self.assertEqual(len(app.outputs), 1)  # the second was not part of that run
            self.assertEqual(self.listed(), ["one.epub", "one_unlocked.epub", "two.epub"])
            self.done(app)

        self.loop(app, lambda: self.after_analysis(app, start))

    def test_clearing_during_a_run_cannot_break_the_worker(self) -> None:
        """Clear used to come back on mid-run and empty the table the worker was
        still reading, killing it with a KeyError before it reported."""
        app = self.app([self.book("one.epub")])
        release = self.gate("process_epub")
        crashes: list = []
        import threading
        previous = threading.excepthook
        threading.excepthook = lambda args: crashes.append(args.exc_value)
        self.addCleanup(setattr, threading, "excepthook", previous)

        def start() -> None:
            app.run()
            app.add_paths([self.book("two.epub")])
            app.root.after(300, poke)

        def poke() -> None:
            app.clear_files()  # a press of the button, if it were enabled
            release.set()
            self.after_run(app, check)

        def check() -> None:
            self.assertEqual(crashes, [], "the worker thread died")
            self.assertEqual(len(app.outputs), 1)
            self.assertTrue(any(kind == "showinfo" and i18n.t("msg_done", count=1) in text
                                for kind, text in self.dialogs), self.dialogs)
            self.done(app)

        self.loop(app, lambda: self.after_analysis(app, start))

    def test_a_drop_during_the_analysis_is_not_lost(self) -> None:
        app = self.app()
        release = self.gate("analyze_epub")
        second = self.book("second.epub")

        def start() -> None:
            app.add_paths([self.book("first.epub")])
            app.add_paths([second])  # the first is still being read
            app.root.after(200, release.set)
            self.wait(app, lambda: len(app.files) == 2 and str(app.btn_run["state"]) == "normal",
                      check, "both books")

        def check() -> None:
            self.assertEqual(sorted(os.path.basename(p) for p in app.files),
                             ["first.epub", "second.epub"])
            self.done(app)

        self.loop(app, start)

    def test_a_queued_drop_says_so(self) -> None:
        app = self.app()
        release = self.gate("analyze_epub")

        def start() -> None:
            app.add_paths([self.book("first.epub")])
            app.add_paths([self.book("second.epub")])
            log = app.txt_log.get("1.0", "end")
            self.assertIn(i18n.t("log_queued", count=1), log)
            release.set()
            self.wait(app, lambda: len(app.files) == 2, lambda: self.done(app), "both books")

        self.loop(app, start)

    def test_the_same_book_dropped_twice_in_a_row_is_added_once(self) -> None:
        app = self.app()
        release = self.gate("analyze_epub")
        book = self.book()

        def start() -> None:
            app.add_paths([book])
            app.add_paths([book])  # while the first is still in flight
            release.set()
            self.wait(app, lambda: str(app.btn_run["state"]) == "normal" and app.files, check,
                      "the analysis")

        def check() -> None:
            app.root.after(300, lambda: self.guard(app, verify))

        def verify() -> None:
            self.assertEqual(len(app.files), 1)
            self.done(app)

        self.loop(app, start)

    def test_a_folder_of_refused_books_gives_one_dialog_not_one_each(self) -> None:
        for i in range(12):
            fixtures.build(os.path.join(self.dir, f"drm{i:02d}.epub"),
                           {"OEBPS/style.css": fixtures.CSS, "OEBPS/ch1.xhtml": fixtures.XHTML},
                           encryption=True)
        app = self.app([self.dir])

        def check() -> None:
            errors = [text for kind, text in self.dialogs if kind == "showerror"]
            self.assertEqual(len(errors), 1, f"{len(errors)} dialogs for 12 books")
            self.assertIn("drm00.epub", errors[0])
            self.assertEqual(app.txt_log.get("1.0", "end").count(i18n.t("log_drm", name="drm")[:4]), 12)
            self.assertEqual(app.files, [])
            self.done(app)

        self.loop(app, lambda: self.after_analysis(app, check))

    def test_a_few_refused_books_are_all_named_in_the_one_dialog(self) -> None:
        for name in ("a.epub", "b.epub"):
            fixtures.build(os.path.join(self.dir, name),
                           {"OEBPS/style.css": fixtures.CSS, "OEBPS/ch1.xhtml": fixtures.XHTML},
                           encryption=True)
        app = self.app([self.dir])

        def check() -> None:
            errors = [text for kind, text in self.dialogs if kind == "showerror"]
            self.assertEqual(len(errors), 1)
            self.assertIn("a.epub", errors[0])
            self.assertIn("b.epub", errors[0])
            self.done(app)

        self.loop(app, lambda: self.after_analysis(app, check))

    def test_a_good_book_among_refused_ones_still_loads_with_one_dialog(self) -> None:
        fixtures.build(os.path.join(self.dir, "drm.epub"),
                       {"OEBPS/style.css": fixtures.CSS, "OEBPS/ch1.xhtml": fixtures.XHTML},
                       encryption=True)
        open(os.path.join(self.dir, "broken.epub"), "wb").close()
        self.book("good.epub")
        app = self.app([self.dir])

        def check() -> None:
            self.assertEqual([os.path.basename(p) for p in app.files], ["good.epub"])
            self.assertEqual(len([1 for kind, _ in self.dialogs if kind == "showerror"]), 1)
            self.done(app)

        self.loop(app, lambda: self.after_analysis(app, check))

    # ------------------------------------------------------ new-version notice
    SITE_URL = ("https://chaekgalpi.co.kr/tools/epubfont?utm_source=app&utm_medium=update"
                "&utm_campaign=tool_update&utm_content=epubfont")

    def news(self, latest: str = "0.3.0") -> dict:
        return {"slug": "epubfont", "latest": latest, "updateAvailable": True, "url": self.SITE_URL}

    def info(self, latest: str = "0.3.0") -> update_check.UpdateInfo:
        return update_check.UpdateInfo(latest, update_check_version(), self.SITE_URL)

    def notice_shown(self, app: gui.App) -> bool:
        return bool(app.frm_update.winfo_manager())

    def positions(self, app: gui.App) -> dict:
        """Where every widget outside the notice sits, relative to the window."""
        app.root.update()
        left, top = app.root.winfo_rootx(), app.root.winfo_rooty()
        notice = str(app.frm_update)
        found = {}
        for w in self.widgets(app):
            name = str(w)
            if name == "." or name.startswith(notice):
                continue
            found[name] = (w.winfo_rootx() - left, w.winfo_rooty() - top,
                           w.winfo_width(), w.winfo_height())
        return found

    def test_the_check_runs_by_itself_and_shows_the_notice(self) -> None:
        self.answer = self.news()
        app = self.app()

        def check() -> None:
            self.assertEqual(self.asked, [("epubfont", update_check_version())])
            self.assertTrue(self.notice_shown(app))
            self.assertEqual(app.lbl_update.cget("text"),
                             f"새 버전이 나왔습니다: 0.3.0 (현재 {update_check_version()})")
            self.assertEqual(i18n.load_settings()["update_check_last"], update_check.today())
            self.done(app)

        self.loop(app, lambda: self.wait(app, lambda: self.notice_shown(app), check, "the notice"))

    def test_no_notice_when_the_server_does_not_answer_and_the_day_stays_free(self) -> None:
        self.answer = None
        app = self.app()

        def check() -> None:
            app.root.after(300, lambda: self.guard(app, verify))

        def verify() -> None:
            self.assertFalse(self.notice_shown(app))
            self.assertEqual(i18n.load_settings().get("update_check_last", ""), "")
            self.done(app)

        self.loop(app, lambda: self.wait(app, lambda: bool(self.asked), check, "the question"))

    def test_the_server_saying_no_news_shows_nothing_but_uses_up_the_day(self) -> None:
        self.answer = dict(self.news(), updateAvailable=False)
        app = self.app()

        def check() -> None:
            app.root.after(300, lambda: self.guard(app, verify))

        def verify() -> None:
            self.assertFalse(self.notice_shown(app))
            self.assertEqual(i18n.load_settings()["update_check_last"], update_check.today())
            self.done(app)

        self.loop(app, lambda: self.wait(app, lambda: bool(self.asked), check, "the question"))

    def test_switched_off_nobody_is_asked(self) -> None:
        with open(self.settings_path, "w", encoding="utf-8") as f:
            f.write('{"update_check": false}')
        self.answer = self.news()
        app = self.app()

        def check() -> None:
            self.assertEqual(self.asked, [])
            self.assertFalse(app.var_update_check.get())
            self.assertFalse(self.notice_shown(app))
            self.done(app)

        self.loop(app, lambda: app.root.after(2300, lambda: self.guard(app, check)))

    def test_asked_today_already_nobody_is_asked(self) -> None:
        with open(self.settings_path, "w", encoding="utf-8") as f:
            f.write('{"update_check_last": "%s"}' % update_check.today())
        self.answer = self.news()
        app = self.app()

        def check() -> None:
            self.assertEqual(self.asked, [])
            self.done(app)

        self.loop(app, lambda: app.root.after(2300, lambda: self.guard(app, check)))

    def test_the_notice_is_not_a_dialog(self) -> None:
        app = self.app()

        def check() -> None:
            app._on_update_result(self.info(), True)
            app.root.update()
            self.assertTrue(self.notice_shown(app))
            self.assertEqual(self.dialogs, [])
            self.done(app)

        self.loop(app, check)

    def test_update_opens_only_the_tools_page_and_clears_the_notice(self) -> None:
        app = self.app()

        def check() -> None:
            app._on_update_result(self.info(), True)
            app._update_open()
            self.assertEqual(self.opened, [self.SITE_URL])
            self.assertFalse(self.notice_shown(app))
            self.done(app)

        self.loop(app, check)

    SITE_URL_EN = SITE_URL.replace("/tools/epubfont", "/en/tools/epubfont")

    def english_info(self) -> update_check.UpdateInfo:
        return update_check.UpdateInfo("0.3.0", update_check_version(), self.SITE_URL, self.SITE_URL_EN)

    def test_update_opens_the_page_for_the_language_on_screen(self) -> None:
        """Korean gets the Korean page; English, Japanese and Chinese get the
        English one the server named."""
        app = self.app()

        def check() -> None:
            wanted = {"ko": self.SITE_URL, "en": self.SITE_URL_EN,
                      "ja": self.SITE_URL_EN, "zh-CN": self.SITE_URL_EN}
            for index, lang in enumerate(i18n.LANGS):
                app.cmb_lang.current(index)
                app._on_lang()
                self.opened.clear()
                app._update_info = self.english_info()
                app._show_update_notice()
                app._update_open()
                self.assertEqual(self.opened, [wanted[lang]], lang)
                self.assertFalse(self.notice_shown(app), lang)
            self.done(app)

        self.loop(app, check)

    def test_the_language_at_the_moment_of_the_click_decides(self) -> None:
        """A notice that came up in Korean and then had the window switched to
        English opens the English page."""
        app = self.app()

        def check() -> None:
            app._on_update_result(self.english_info(), True)
            app.cmb_lang.current(i18n.LANGS.index("en"))
            app._on_lang()
            app._update_open()
            self.assertEqual(self.opened, [self.SITE_URL_EN])
            self.done(app)

        self.loop(app, check)

    def test_a_server_without_an_english_page_gives_every_language_the_korean_one(self) -> None:
        app = self.app()

        def check() -> None:
            for index, lang in enumerate(i18n.LANGS):
                app.cmb_lang.current(index)
                app._on_lang()
                self.opened.clear()
                app._update_info = self.info()  # no url_en
                app._show_update_notice()
                app._update_open()
                self.assertEqual(self.opened, [self.SITE_URL], lang)
            self.done(app)

        self.loop(app, check)

    def test_an_english_page_off_the_site_is_never_opened_and_falls_back(self) -> None:
        app = self.app()

        def check() -> None:
            app.cmb_lang.current(i18n.LANGS.index("en"))
            app._on_lang()
            # straight from a server answer, as the check delivers it
            data = dict(self.news(), urlEn="https://evil.example/en/tools/epubfont")
            app._on_update_result(update_check.interpret(data, update_check_version()), True)
            app._update_open()
            self.assertEqual(self.opened, [self.SITE_URL])
            self.done(app)

        self.loop(app, check)

    def test_a_forged_english_page_set_on_the_notice_itself_is_still_refused(self) -> None:
        """Even if something put a foreign address straight into the notice, the
        window only opens pages of the site."""
        app = self.app()

        def check() -> None:
            app.cmb_lang.current(i18n.LANGS.index("en"))
            app._on_lang()
            app._update_info = update_check.UpdateInfo("9.9.9", update_check_version(),
                                                       "https://evil.example/", "https://evil.example/en")
            app._show_update_notice()
            app._update_open()
            self.assertEqual(self.opened, [])
            self.done(app)

        self.loop(app, check)

    def test_a_link_off_the_site_is_never_opened(self) -> None:
        app = self.app()

        def check() -> None:
            for url in ("https://evil.example/", "http://chaekgalpi.co.kr/", "javascript:alert(1)",
                        "https://chaekgalpi.co.kr.evil.example/"):
                app._update_info = update_check.UpdateInfo("9.9.9", update_check_version(), url)
                app._show_update_notice()
                app._update_open()
            self.assertEqual(self.opened, [])
            self.done(app)

        self.loop(app, check)

    def test_later_is_remembered_and_that_version_is_not_shown_again(self) -> None:
        app = self.app()

        def first() -> None:
            app._on_update_result(self.info("0.3.0"), True)
            app._update_later()
            self.assertFalse(self.notice_shown(app))
            self.assertEqual(i18n.load_settings()["update_check_skipped"], "0.3.0")
            self.done(app)

        self.loop(app, first)
        self.destroy(app)

        # a later start, a new day: the same version stays quiet
        settings = i18n.load_settings()
        settings["update_check_last"] = "2000-01-01"
        i18n.save_settings(settings)
        self.answer = self.news("0.3.0")
        self.asked.clear()
        again = self.app()

        def second() -> None:
            again.root.after(300, lambda: self.guard(again, verify))

        def verify() -> None:
            self.assertFalse(self.notice_shown(again))
            self.done(again)

        self.loop(again, lambda: self.wait(again, lambda: bool(self.asked), second, "the question"))

    def test_later_does_not_hide_a_newer_version(self) -> None:
        settings = {"update_check_skipped": "0.3.0", "update_check_last": "2000-01-01"}
        with open(self.settings_path, "w", encoding="utf-8") as f:
            import json
            json.dump(settings, f)
        self.answer = self.news("0.3.1")
        app = self.app()

        def check() -> None:
            self.assertIn("0.3.1", app.lbl_update.cget("text"))
            self.done(app)

        self.loop(app, lambda: self.wait(app, lambda: self.notice_shown(app), check, "the notice"))

    def test_unchecking_clears_the_notice_and_is_remembered(self) -> None:
        app = self.app()

        def first() -> None:
            app._on_update_result(self.info(), True)
            self.assertTrue(self.notice_shown(app))
            app.var_update_check.set(False)
            app._on_update_toggle()
            self.assertFalse(self.notice_shown(app))
            self.assertIs(i18n.load_settings()["update_check"], False)
            self.done(app)

        self.loop(app, first)
        self.destroy(app)

        self.answer = self.news()
        self.asked.clear()
        again = self.app()

        def second() -> None:
            self.assertFalse(again.var_update_check.get())
            self.assertEqual(self.asked, [])
            self.done(again)

        self.loop(again, lambda: again.root.after(2300, lambda: self.guard(again, second)))

    def test_a_notice_that_arrives_with_the_box_unchecked_is_not_shown(self) -> None:
        app = self.app()

        def check() -> None:
            app.var_update_check.set(False)
            app._on_update_result(self.info(), True)
            self.assertFalse(self.notice_shown(app))
            self.done(app)

        self.loop(app, check)

    def test_an_answer_after_the_window_is_closing_is_ignored(self) -> None:
        app = self.app()

        def check() -> None:
            app._closing = True
            app._on_update_result(self.info(), True)
            self.assertFalse(self.notice_shown(app))
            self.assertNotIn("update_check_last", i18n.load_settings())
            self.done(app)

        self.loop(app, check)

    def test_the_checkbox_is_on_by_default_and_worded_in_every_language(self) -> None:
        app = self.app()

        def check() -> None:
            self.assertTrue(app.var_update_check.get())
            expected = {"ko": "새 버전 확인", "en": "Check for updates",
                        "ja": "新しいバージョンを確認", "zh-CN": "检查新版本"}
            for index, lang in enumerate(i18n.LANGS):
                app.cmb_lang.current(index)
                app._on_lang()
                self.assertEqual(app.chk_update.cget("text"), expected[lang])
            self.done(app)

        self.loop(app, check)

    def test_the_notice_and_its_buttons_are_worded_in_every_language(self) -> None:
        app = self.app()

        def check() -> None:
            app._on_update_result(self.info("0.3.0"), True)
            current = update_check_version()
            expected = {
                "ko": (f"새 버전이 나왔습니다: 0.3.0 (현재 {current})", "업데이트", "다음에"),
                "en": (f"Version 0.3.0 is available (current {current})", "Update", "Later"),
                "ja": (f"新しいバージョン 0.3.0 があります（現在 {current}）", "更新", "後で"),
                "zh-CN": (f"有新版本 0.3.0（当前 {current}）", "更新", "以后"),
            }
            for index, lang in enumerate(i18n.LANGS):
                app.cmb_lang.current(index)
                app._on_lang()  # rewords a notice that is already up
                self.assertEqual((app.lbl_update.cget("text"), app.btn_update.cget("text"),
                                  app.btn_update_later.cget("text")), expected[lang], lang)
            self.done(app)

        self.loop(app, check)

    def test_the_notice_changes_nothing_above_it(self) -> None:
        app = self.app()

        def check() -> None:
            app.root.update()
            before = self.positions(app)
            height = app.root.winfo_height()

            app._on_update_result(self.info(), True)
            app.root.update()
            self.assertEqual(self.positions(app), before, "something moved when the notice appeared")
            grown = app.root.winfo_height() - height
            self.assertAlmostEqual(grown, app.frm_update.winfo_reqheight(), delta=2)

            app._update_later()
            app.root.update()
            self.assertEqual(self.positions(app), before, "something moved when the notice went")
            self.assertEqual(app.root.winfo_height(), height)
            self.done(app)

        self.loop(app, check)

    def test_the_notice_never_clips_at_any_scale_language_or_size(self) -> None:
        app = self.app()

        def check() -> None:
            app._on_update_result(self.info("10.20.30"), True)
            app.root.update()  # let the window manager finish growing the window
            bad: list[str] = []
            for percent, scaling in ((100, 96 / 72), (125, 120 / 72), (150, 144 / 72),
                                     (200, 192 / 72)):
                app.root.tk.call("tk", "scaling", scaling)
                for index, lang in enumerate(i18n.LANGS):
                    app.cmb_lang.current(index)
                    app._on_lang()
                    app.root.update()  # real events, not only idle tasks: a resize is one
                    label = f"{percent}%/{lang}"
                    bad += self.clipping(app, label)
                    bottom = app.root.winfo_rooty() + app.root.winfo_height()
                    for button in (app.btn_update, app.btn_update_later, app.btn_run):
                        self.assertTrue(button.winfo_ismapped(), f"{label}: {button}")
                        self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), bottom,
                                             f"{label}: {button} pushed off the window")
                    self.assertGreaterEqual(app.root.winfo_height(), app.root.winfo_reqheight(), label)
            self.assertEqual(bad, [])
            self.done(app)

        self.loop(app, check)

    def test_the_header_fits_the_checkbox_beside_the_language_choice(self) -> None:
        app = self.app()

        def check() -> None:
            app.root.update()
            self.assertLess(app.chk_update.winfo_rootx() + app.chk_update.winfo_width(),
                            app.cmb_lang.winfo_rootx())
            self.assertGreater(app.chk_update.winfo_rootx(), app.root.winfo_rootx())
            self.done(app)

        self.loop(app, check)

    def test_the_other_settings_survive_the_update_keys(self) -> None:
        with open(self.settings_path, "w", encoding="utf-8") as f:
            f.write('{"lang": "ja", "options": {"font": false, "margin": true}, "extra": 7}')
        i18n.init(None)
        app = self.app()

        def check() -> None:
            app._on_update_result(self.info(), True)
            app._update_later()
            stored = i18n.load_settings()
            self.assertEqual(stored["lang"], "ja")
            self.assertEqual(stored["options"], {"font": False, "margin": True})
            self.assertEqual(stored["extra"], 7)
            self.assertEqual(stored["update_check_skipped"], "0.3.0")
            self.done(app)

        self.loop(app, check)

    def test_the_window_opens_with_nonsense_in_the_update_settings(self) -> None:
        with open(self.settings_path, "w", encoding="utf-8") as f:
            f.write('{"update_check": "no", "update_check_last": 20261013, "update_check_skipped": [1]}')
        app = self.app()
        self.loop(app, lambda: self.done(app))
        self.assertTrue(app.var_update_check.get())

    def test_closing_the_window_while_the_request_is_out_survives_three_times(self) -> None:
        """The crash this guards against takes down the whole process
        (Tcl_AsyncDelete: async handler deleted by the wrong thread), so
        surviving is the assertion. The request is held in flight, the window is
        closed under it and every reference the test has is dropped, so if the
        worker thread were still holding the App it would be the last owner and
        free it off the main thread when the request finishes."""
        import gc
        import threading as th

        started, release = th.Event(), th.Event()

        def blocking(slug, version, **kwargs):
            started.set()
            release.wait(15)
            return None

        update_check.fetch = blocking  # setUp puts the original back afterwards
        self.addCleanup(release.set)
        self.addCleanup(setattr, update_check, "DELAY_MS", update_check.DELAY_MS)
        update_check.DELAY_MS = 50

        def one_round() -> None:
            started.clear()
            release.clear()
            app = gui.App()
            root = app.root

            def wait(tries: int = 0) -> None:
                if started.is_set() or tries > 300:
                    app._on_close()  # the request is out: close the window now
                    return
                root.after(20, lambda: wait(tries + 1))

            root.after(20, wait)
            root.mainloop()  # returns when the window has been destroyed
            self.assertTrue(started.is_set(), "the check never started")
            del app, root, wait
            gc.collect()  # the main thread lets go of what it can, as it would for real
            release.set()  # the request now finishes with nobody left to tell
            for thread in th.enumerate():
                if thread.name == "update-check":
                    thread.join(10)
                    self.assertFalse(thread.is_alive(), "the check thread did not end")

        for _ in range(3):
            one_round()

        # the Tcl interpreter is still sound afterwards
        probe = tk.Tk()
        probe.update()
        probe.destroy()

    def test_running_a_conversion_does_not_disturb_the_notice(self) -> None:
        app = self.app([self.book()])

        def start() -> None:
            app._on_update_result(self.info(), True)
            app.run()
            self.after_run(app, check)

        def check() -> None:
            self.assertTrue(self.notice_shown(app))
            self.assertEqual(len(app.outputs), 1)
            self.done(app)

        self.loop(app, lambda: self.after_analysis(app, start))

    # ------------------------------------------------------------- settings
    def test_options_survive_a_restart(self) -> None:
        app = self.app()

        def check() -> None:
            app.var_margin.set(True)
            app.var_size.set(False)
            app._save_options()
            self.done(app)

        self.loop(app, check)
        self.destroy(app)

        again = self.app()
        self.loop(again, lambda: self.done(again))
        self.assertTrue(again.var_margin.get())
        self.assertFalse(again.var_size.get())

    def test_a_corrupt_settings_file_still_opens_the_window(self) -> None:
        with open(self.settings_path, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        app = self.app()
        self.loop(app, lambda: self.done(app))
        self.assertTrue(app.var_font.get())
        self.assertFalse(app.var_margin.get())

    def test_settings_with_wrong_types_fall_back_to_defaults(self) -> None:
        with open(self.settings_path, "w", encoding="utf-8") as f:
            f.write('{"options": {"font": "yes", "margin": 3}, "lang": 7}')
        app = self.app()
        self.loop(app, lambda: self.done(app))
        self.assertTrue(app.var_font.get())
        self.assertFalse(app.var_margin.get())

    def test_defaults_are_the_three_the_prd_asks_for(self) -> None:
        app = self.app()
        self.loop(app, lambda: self.done(app))
        self.assertEqual(
            (app.var_font.get(), app.var_size.get(), app.var_line.get(),
             app.var_margin.get(), app.var_remove_files.get()),
            (True, True, True, False, False))

    # --------------------------------------------------------------- closing
    def test_closing_saves_the_options_and_tears_the_window_down(self) -> None:
        app = self.app([self.book()])

        def check() -> None:
            app.var_margin.set(True)
            app._on_close()

        # _on_close destroys the window, which is what ends the main loop.
        self.loop(app, lambda: self.after_analysis(app, check))
        self.assertTrue(i18n.load_settings()["options"]["margin"])
        with self.assertRaises(tk.TclError):
            app.root.winfo_exists()


if __name__ == "__main__":
    sys.exit(unittest.main())
