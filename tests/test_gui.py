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

try:
    _probe = tk.Tk()
    _probe.destroy()
    HAS_DISPLAY = True
except Exception:  # pragma: no cover - headless machine
    HAS_DISPLAY = False

import gui  # noqa: E402

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
            self.assertEqual(len(boxes), 5)
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
