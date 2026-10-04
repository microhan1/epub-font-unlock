"""The entry point: which front end runs, what gets printed, what comes back.

run_cli's behaviour on bad books lives in test_exceptions. This file is about
main() itself -- the choice between the window and the command line, the exit
codes, and the language the output comes out in.
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import unittest

import fixtures  # noqa: E402  (inserts the repo root on sys.path)

import gui  # noqa: E402
import i18n  # noqa: E402
import main as cli  # noqa: E402
import unlock  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="efu_cli_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        settings = os.path.join(self.dir, "settings.json")
        original = i18n.SETTINGS_PATH
        i18n.SETTINGS_PATH = settings
        self.addCleanup(setattr, i18n, "SETTINGS_PATH", original)
        # main() picks the language from settings, then from the OS. Pin it, or
        # these tests read differently on a Korean machine than on an English one.
        i18n.save_settings({"lang": "en"})
        i18n.init("en")
        self.addCleanup(i18n.init, "en")

        self.launched: list = []
        self.addCleanup(setattr, gui, "launch", gui.launch)
        gui.launch = lambda files=None: self.launched.append(files)

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def book(self, name: str = "book.epub") -> str:
        return fixtures.standard(self.path(name))

    def main(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = cli.main(argv)
        finally:
            sys.stdout, sys.stderr = old
        return code, out.getvalue(), err.getvalue()

    def listed(self) -> list[str]:
        return sorted(n for n in os.listdir(self.dir) if n != "settings.json")


class WhichFrontEnd(Base):
    def test_no_arguments_opens_the_window(self) -> None:
        code, _, _ = self.main([])
        self.assertEqual(code, 0)
        self.assertEqual(self.launched, [None])

    def test_the_gui_flag_opens_the_window_with_the_files(self) -> None:
        book = self.book()
        code, _, _ = self.main([book, "--gui"])
        self.assertEqual(code, 0)
        self.assertEqual(self.launched, [[book]])
        self.assertEqual(self.listed(), ["book.epub"])

    def test_a_windowed_exe_opens_the_window_even_with_files(self) -> None:
        """Files dropped on the exe icon: there is no console to print into,
        so they are loaded into the window instead."""
        book = self.book()
        out, err = io.StringIO(), io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = None, err
        sys.frozen = True
        try:
            code = cli.main([book])
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            del sys.frozen
        self.assertEqual(code, 0)
        self.assertEqual(self.launched, [[book]])

    def test_files_go_to_the_command_line(self) -> None:
        self.book()
        code, out, _ = self.main([self.path("book.epub"), "--font"])
        self.assertEqual(code, 0)
        self.assertEqual(self.launched, [])
        self.assertIn("book_unlocked.epub", out)


class ExitCodes(Base):
    def test_success_is_zero(self) -> None:
        self.book()
        self.assertEqual(self.main([self.path("book.epub"), "--font"])[0], 0)

    def test_nothing_to_do_is_two(self) -> None:
        self.assertEqual(self.main([self.path("nope.epub")])[0], 2)
        self.assertEqual(self.main([self.dir])[0], 2)

    def test_a_failed_book_is_one(self) -> None:
        open(self.path("bad.epub"), "wb").close()
        self.assertEqual(self.main([self.path("bad.epub")])[0], 1)

    def test_an_interrupted_run_is_one_hundred_and_thirty(self) -> None:
        self.book()
        real = unlock.process_epub
        unlock.process_epub = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())
        self.addCleanup(setattr, unlock, "process_epub", real)
        code, out, _ = self.main([self.path("book.epub"), "--font"])
        self.assertEqual(code, 130)
        self.assertIn(i18n.t("status_cancelled"), out)

    def test_a_typo_among_good_paths_is_one_not_zero(self) -> None:
        """A script that passes three paths and gets exit 0 back never learns
        that one of them was a typo."""
        good = self.book()
        code, out, err = self.main([good, self.path("typo.epub"), "--font"])
        self.assertEqual(code, 1)
        self.assertIn("typo.epub", err)
        self.assertIn("book_unlocked.epub", out)  # the good one was still done

    def test_a_file_that_is_not_an_epub_is_reported_not_ignored(self) -> None:
        good = self.book()
        notes = self.path("notes.txt")
        with open(notes, "w", encoding="utf-8") as f:
            f.write("not a book")
        code, out, err = self.main([notes, good, "--font"])
        self.assertEqual(code, 1)
        self.assertIn("notes.txt", err)
        self.assertIn("book_unlocked.epub", out)

    def test_only_a_non_epub_file_is_two_and_still_reported(self) -> None:
        notes = self.path("notes.txt")
        with open(notes, "w", encoding="utf-8") as f:
            f.write("x")
        code, _, err = self.main([notes, "--font"])
        self.assertEqual(code, 2)
        self.assertIn("notes.txt", err)

    def test_a_folder_full_of_other_files_is_not_an_error(self) -> None:
        """Only a path named outright is held to being an EPUB; what a folder
        contains is just looked through."""
        self.book()
        with open(self.path("notes.txt"), "w", encoding="utf-8") as f:
            f.write("x")
        code, _, err = self.main([self.dir, "--font"])
        self.assertEqual(code, 0)
        self.assertNotIn("notes.txt", err)

    def test_an_unknown_option_is_two(self) -> None:
        old = sys.stderr
        sys.stderr = io.StringIO()
        try:
            with self.assertRaises(SystemExit) as caught:
                cli.main([self.path("book.epub"), "--nope"])
        finally:
            sys.stderr = old
        self.assertEqual(caught.exception.code, 2)


class Output(Base):
    def test_analyze_only_prints_and_writes_nothing(self) -> None:
        self.book()
        code, out, _ = self.main([self.path("book.epub"), "--analyze-only"])
        self.assertEqual(code, 0)
        self.assertIn("fixed fonts", out)
        self.assertIn("inline styles", out)
        self.assertEqual(self.listed(), ["book.epub"])

    def test_analyze_only_walks_a_folder(self) -> None:
        self.book("one.epub")
        self.book("two.epub")
        code, out, _ = self.main([self.dir, "--analyze-only"])
        self.assertEqual(code, 0)
        self.assertIn("[1/2]", out)
        self.assertIn("[2/2]", out)
        self.assertEqual(self.listed(), ["one.epub", "two.epub"])

    def test_the_run_reports_what_it_removed_and_where_it_went(self) -> None:
        self.book()
        counts = unlock.analyze_epub(self.path("book.epub")).counts
        code, out, _ = self.main([self.path("book.epub"), "--font", "--size", "--line-height"])
        self.assertEqual(code, 0)
        self.assertIn(f"{counts.fonts} fonts", out)
        self.assertIn("book_unlocked.epub", out)
        self.assertIn("Done: 1 files processed", out)

    def test_the_language_flag_changes_the_output(self) -> None:
        self.book()
        for lang, needle in (("ko", "완료: 1개 파일 처리"),
                             ("ja", "完了: 1 件のファイルを処理"),
                             ("zh-CN", "完成：已处理 1 个文件")):
            with self.subTest(lang=lang):
                shutil.rmtree(self.dir, True)
                os.makedirs(self.dir)
                self.book()
                code, out, _ = self.main([self.path("book.epub"), "--font", "--lang", lang])
                self.assertEqual(code, 0)
                self.assertIn(needle, out)

    def test_the_language_flag_is_not_remembered(self) -> None:
        """--lang is for one run; it must not rewrite the saved preference."""
        i18n.set_lang("ko")
        self.book()
        self.main([self.path("book.epub"), "--font", "--lang", "ja"])
        self.assertEqual(i18n.load_settings()["lang"], "ko")

    def test_help_comes_out_in_the_chosen_language(self) -> None:
        for lang, needle in (("ko", "사용법: "), ("ja", "使い方: "),
                             ("zh-CN", "用法： "), ("en", "usage: ")):
            with self.subTest(lang=lang):
                i18n.init(lang)
                text = cli.build_parser().format_help()
                self.assertIn(needle, text)
                self.assertIn("--remove-font-files", text)
                self.assertIn("--analyze-only", text)

    def test_the_language_is_read_from_argv_before_the_parser_exists(self) -> None:
        self.assertEqual(cli._preselect_lang(["a.epub", "--lang", "ja"]), "ja")
        self.assertEqual(cli._preselect_lang(["a.epub", "--lang=zh-CN"]), "zh-CN")
        self.assertIsNone(cli._preselect_lang(["a.epub", "--lang"]))
        self.assertIsNone(cli._preselect_lang(["a.epub"]))


class Options(Base):
    def parse(self, argv: list[str]) -> unlock.Options:
        return cli.options_from(cli.build_parser().parse_args(argv))

    def test_no_flags_means_the_three_defaults(self) -> None:
        opts = self.parse(["x.epub"])
        self.assertEqual((opts.font, opts.size, opts.line, opts.margin,
                          opts.remove_font_files), (True, True, True, False, False))

    def test_naming_a_flag_selects_exactly_that_set(self) -> None:
        opts = self.parse(["x.epub", "--margin"])
        self.assertEqual((opts.font, opts.size, opts.line, opts.margin), (False, False, False, True))
        opts = self.parse(["x.epub", "--font", "--remove-font-files"])
        self.assertEqual((opts.font, opts.size, opts.line, opts.remove_font_files),
                         (True, False, False, True))

    def test_analyze_only_does_not_count_as_choosing_options(self) -> None:
        opts = self.parse(["x.epub", "--analyze-only"])
        self.assertTrue(opts.font and opts.size and opts.line)

    def test_the_prd_command_line_works_as_written(self) -> None:
        self.book()
        code, out, _ = self.main([self.path("book.epub"), "--font", "--size", "--line-height",
                                  "--margin", "--remove-font-files"])
        self.assertEqual(code, 0)
        self.assertIn("book_unlocked.epub", out)


if __name__ == "__main__":
    sys.exit(unittest.main())
