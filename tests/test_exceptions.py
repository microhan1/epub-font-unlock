"""The exception table, run rather than read.

Every row of the PRD's 예외 처리 table has a case here, plus the series rules
about settings.json, batch isolation and cancelling mid-run.
"""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest
import zipfile

import fixtures  # noqa: E402  (inserts the repo root on sys.path)

import epub_io  # noqa: E402
import gui  # noqa: E402
import i18n  # noqa: E402
import main as cli  # noqa: E402
import unlock  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="efu_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        i18n.init("en")

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    @staticmethod
    def digest(path: str) -> str:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = cli.run_cli(cli.build_parser().parse_args(argv))
        finally:
            sys.stdout, sys.stderr = old
        return code, out.getvalue(), err.getvalue()


class BadInput(Base):
    def test_drm_file_is_reported_and_skipped(self) -> None:
        book = fixtures.build(self.path("drm.epub"),
                              {"OEBPS/style.css": fixtures.CSS, "OEBPS/ch1.xhtml": fixtures.XHTML},
                              encryption=True)
        with self.assertRaises(unlock.DrmProtected):
            unlock.analyze_epub(book)
        code, out, err = self.run_cli([book, "--font"])
        self.assertIn("DRM", err)
        self.assertEqual(code, 0)  # skipped, not a failure
        self.assertEqual(sorted(os.listdir(self.dir)), ["drm.epub"])

    def test_empty_file(self) -> None:
        open(self.path("empty.epub"), "wb").close()
        with self.assertRaises(unlock.BrokenArchive):
            unlock.analyze_epub(self.path("empty.epub"))

    def test_text_file_with_epub_extension(self) -> None:
        with open(self.path("fake.epub"), "w", encoding="utf-8") as f:
            f.write("this is not a zip")
        with self.assertRaises(unlock.BrokenArchive):
            unlock.analyze_epub(self.path("fake.epub"))

    def test_truncated_zip(self) -> None:
        book = fixtures.standard(self.path("book.epub"))
        with open(book, "rb") as f:
            data = f.read()
        with open(self.path("cut.epub"), "wb") as f:
            f.write(data[: len(data) // 2])
        with self.assertRaises(unlock.BrokenArchive):
            unlock.analyze_epub(self.path("cut.epub"))

    def test_broken_input_gives_exit_code_1(self) -> None:
        open(self.path("empty.epub"), "wb").close()
        code, _, err = self.run_cli([self.path("empty.epub"), "--font"])
        self.assertEqual(code, 1)
        self.assertIn("Cannot open", err)

    def test_missing_path_is_reported_not_silently_skipped(self) -> None:
        code, _, err = self.run_cli([self.path("nope.epub"), "--font"])
        self.assertEqual(code, 2)
        self.assertIn("nope.epub", err)


class Isolation(Base):
    def test_one_bad_css_does_not_stop_the_book(self) -> None:
        book = fixtures.with_broken_css(self.path("book.epub"))
        result = unlock.process_epub(book, unlock.Options())
        self.assertTrue(any("bad.css" in name for name, _ in result.failed))
        with zipfile.ZipFile(result.output_path) as zf:
            self.assertNotIn("font-family", zf.read("OEBPS/style.css").decode("utf-8"))
            self.assertEqual(zf.read("OEBPS/bad.css").decode("utf-8"), fixtures.BROKEN_CSS)

    def test_one_bad_book_does_not_stop_the_batch(self) -> None:
        good = fixtures.standard(self.path("good.epub"))
        open(self.path("bad.epub"), "wb").close()
        code, out, _ = self.run_cli([self.path("bad.epub"), good, "--font"])
        self.assertEqual(code, 1)
        self.assertIn("good_unlocked.epub", out)

    def test_batch_reports_already_unlocked_per_file(self) -> None:
        fixtures.already_unlocked(self.path("plain.epub"))
        fixtures.standard(self.path("fixed.epub"))
        code, out, _ = self.run_cli([self.dir, "--font", "--size", "--line-height"])
        self.assertEqual(code, 0)
        self.assertIn("Already unlocked", out)
        self.assertIn("Done: 1 files processed", out)


class Files(Base):
    def test_uppercase_extension_and_nested_folders(self) -> None:
        os.makedirs(self.path("sub/deeper"))
        os.makedirs(self.path("empty"))
        fixtures.standard(self.path("sub/deeper/A.EPUB"))
        found = unlock.collect_epubs([self.dir])
        self.assertEqual([os.path.basename(p) for p in found], ["A.EPUB"])

    def test_duplicates_are_dropped(self) -> None:
        book = fixtures.standard(self.path("book.epub"))
        self.assertEqual(len(unlock.collect_epubs([book, book, self.dir])), 1)

    def test_awkward_file_names(self) -> None:
        for name in ("한글 제목.epub", "日本語の本.epub", "a#b&c%d.epub"):
            book = fixtures.standard(self.path(name))
            with self.subTest(name=name):
                result = unlock.process_epub(book, unlock.Options())
                self.assertTrue(os.path.exists(result.output_path))

    def test_read_only_original_keeps_its_bytes_and_flag(self) -> None:
        book = fixtures.standard(self.path("book.epub"))
        digest = self.digest(book)
        os.chmod(book, stat.S_IREAD)
        self.addCleanup(os.chmod, book, stat.S_IWRITE | stat.S_IREAD)
        unlock.process_epub(book, unlock.Options())
        self.assertEqual(digest, self.digest(book))
        self.assertFalse(os.access(book, os.W_OK))

    def test_book_without_a_mimetype_entry_still_round_trips(self) -> None:
        book = fixtures.build(self.path("nomime.epub"),
                              {"OEBPS/style.css": fixtures.CSS, "OEBPS/ch1.xhtml": fixtures.XHTML},
                              mimetype=False)
        result = unlock.process_epub(book, unlock.Options())
        with zipfile.ZipFile(result.output_path) as zf:
            self.assertNotIn("mimetype", zf.namelist())
            self.assertIn("OEBPS/style.css", zf.namelist())


class Cancelling(Base):
    def test_cancel_writes_nothing(self) -> None:
        book = fixtures.standard(self.path("book.epub"))
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(unlock.Cancelled):
            unlock.process_epub(book, unlock.Options(), cancel=cancel)
        self.assertEqual(os.listdir(self.dir), ["book.epub"])

    def test_no_temporary_file_is_left_behind(self) -> None:
        book = fixtures.standard(self.path("book.epub"))
        entries = epub_io.read_epub(book)
        target = self.path("out.epub")
        original = epub_io.zipfile.ZipFile

        class Exploding(original):
            def writestr(self, *a, **k):
                raise OSError("disk full")

        epub_io.zipfile.ZipFile = Exploding
        try:
            with self.assertRaises(OSError):
                epub_io.write_epub(target, entries)
        finally:
            epub_io.zipfile.ZipFile = original
        self.assertFalse(os.path.exists(target))
        self.assertFalse(os.path.exists(target + ".tmp"))


class Settings(Base):
    def test_garbage_settings_fall_back_to_defaults(self) -> None:
        for raw in ({"options": "not a dict"}, {"options": {"font": "yes", "size": 3}},
                    {"options": {"margin": None}}, {}, {"options": []}):
            with self.subTest(raw=raw):
                opts = gui._saved_options(raw)
                self.assertIsInstance(opts.font, bool)
                self.assertEqual(opts.font, True)
                self.assertEqual(opts.margin, False)

    @staticmethod
    def _restore_settings(path: str, backup) -> None:
        if backup is None:
            if os.path.exists(path):
                os.remove(path)
            return
        with open(path, "wb") as f:
            f.write(backup)

    def test_broken_settings_file_loads_as_empty(self) -> None:
        path = i18n.SETTINGS_PATH
        backup = None
        if os.path.exists(path):
            with open(path, "rb") as f:
                backup = f.read()
        self.addCleanup(self._restore_settings, path, backup)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(i18n.load_settings(), {})


class CommandLine(Base):
    def test_no_flags_defaults_to_font_size_and_line(self) -> None:
        args = cli.build_parser().parse_args(["x.epub"])
        opts = cli.options_from(args)
        self.assertEqual((opts.font, opts.size, opts.line), (True, True, True))
        self.assertEqual((opts.margin, opts.remove_font_files), (False, False))

    def test_naming_one_flag_selects_only_that_one(self) -> None:
        args = cli.build_parser().parse_args(["x.epub", "--margin"])
        opts = cli.options_from(args)
        self.assertEqual((opts.font, opts.size, opts.line, opts.margin), (False, False, False, True))

    def test_analyze_only_writes_nothing(self) -> None:
        book = fixtures.standard(self.path("book.epub"))
        code, out, _ = self.run_cli([book, "--analyze-only"])
        self.assertEqual(code, 0)
        self.assertIn("fixed fonts", out)
        self.assertEqual(os.listdir(self.dir), ["book.epub"])

    def test_unknown_option_exits_with_two(self) -> None:
        parser = cli.build_parser()
        old, sys.stderr = sys.stderr, io.StringIO()
        try:
            with self.assertRaises(SystemExit) as cm:
                parser.parse_args(["x.epub", "--nope"])
        finally:
            sys.stderr = old
        self.assertEqual(cm.exception.code, 2)

    def test_help_is_translated(self) -> None:
        for lang, needle in (("ko", "사용법: "), ("ja", "使い方: "), ("zh-CN", "用法： ")):
            with self.subTest(lang=lang):
                i18n.init(lang)
                self.assertIn(needle, cli.build_parser().format_help())
        i18n.init("en")


if __name__ == "__main__":
    sys.exit(unittest.main())
