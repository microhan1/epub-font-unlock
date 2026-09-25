"""The container layer on its own: decoding, naming, collecting, rewriting.

The rest of the suite exercises these through whole books. Here they are poked
at directly, because the failure they guard against is silent: a file that
comes back with different bytes than it went in with.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
import zipfile

import fixtures  # noqa: E402,F401  (inserts the repo root on sys.path)

import epub_io  # noqa: E402


class Decoding(unittest.TestCase):
    TEXT = "제목 <p>본문</p> ok"

    def round_trip(self, raw: bytes) -> tuple[str, str]:
        text, codec = epub_io.decode(raw)
        self.assertEqual(text.encode(codec), raw, f"{codec} did not round trip")
        return text, codec

    def test_plain_utf_8(self) -> None:
        text, codec = self.round_trip(self.TEXT.encode("utf-8"))
        self.assertEqual(codec, "utf-8")
        self.assertEqual(text, self.TEXT)

    def test_utf_8_with_a_bom(self) -> None:
        text, _ = self.round_trip(b"\xef\xbb\xbf" + self.TEXT.encode("utf-8"))
        self.assertTrue(text.startswith("﻿"))

    def test_an_encoding_declaration_is_believed(self) -> None:
        raw = ('<?xml version="1.0" encoding="euc-kr"?>' + self.TEXT).encode("euc-kr")
        text, codec = self.round_trip(raw)
        self.assertEqual(codec, "euc-kr")
        self.assertIn("본문", text)

    def test_a_charset_rule_is_believed(self) -> None:
        raw = ('@charset "euc-kr";\np { font-family: "바탕"; }').encode("euc-kr")
        _, codec = self.round_trip(raw)
        self.assertEqual(codec, "euc-kr")

    def test_utf_16_is_found_by_its_bom(self) -> None:
        for codec, bom in (("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")):
            with self.subTest(codec=codec):
                text, found = self.round_trip(bom + self.TEXT.encode(codec))
                self.assertEqual(found, codec)
                self.assertIn("본문", text)

    def test_utf_16_without_a_bom_is_not_guessed_at(self) -> None:
        """Nothing identifies it, so it is read as bytes and handed back
        unchanged rather than mangled."""
        raw = self.TEXT.encode("utf-16-le")
        text, codec = self.round_trip(raw)
        self.assertEqual(codec, "latin-1")

    def test_a_bogus_declared_encoding_falls_through(self) -> None:
        raw = b'<?xml version="1.0" encoding="not-a-codec"?>' + self.TEXT.encode("utf-8")
        _, codec = self.round_trip(raw)
        self.assertEqual(codec, "utf-8")

    def test_arbitrary_bytes_still_round_trip(self) -> None:
        self.round_trip(bytes(range(256)))


class Naming(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="efu_io_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def test_the_first_name_is_plain(self) -> None:
        self.assertEqual(os.path.basename(epub_io.output_path_for(self.path("book.epub"))),
                         "book_unlocked.epub")

    def test_numbers_climb_past_whatever_is_in_the_way(self) -> None:
        open(self.path("book_unlocked.epub"), "wb").close()
        os.makedirs(self.path("book_unlocked(2).epub"))  # a folder counts as taken
        open(self.path("book_unlocked(3).epub"), "wb").close()
        self.assertEqual(os.path.basename(epub_io.output_path_for(self.path("book.epub"))),
                         "book_unlocked(4).epub")

    def test_the_output_sits_beside_the_original(self) -> None:
        out = epub_io.output_path_for(self.path("book.epub"))
        self.assertEqual(os.path.dirname(out), os.path.abspath(self.dir))

    def test_a_name_without_an_extension_still_gets_one(self) -> None:
        self.assertTrue(epub_io.output_path_for(self.path("book")).endswith("_unlocked.epub"))

    def test_unicode_and_punctuation_in_names(self) -> None:
        for name in ("한글 책.epub", "本.epub", "a#b&c%d.epub", "dots.in.name.epub"):
            with self.subTest(name=name):
                out = epub_io.output_path_for(self.path(name))
                self.assertIn("_unlocked", os.path.basename(out))


class Collecting(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="efu_io_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def make(self, *names: str) -> None:
        for name in names:
            path = os.path.join(self.dir, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            open(path, "wb").close()

    def test_folders_are_walked_and_sorted(self) -> None:
        self.make("b.epub", "a.epub", os.path.join("sub", "c.epub"))
        found = [os.path.basename(p) for p in epub_io.collect_epubs([self.dir])]
        self.assertEqual(found, ["a.epub", "b.epub", "c.epub"])

    def test_the_order_of_named_files_is_kept(self) -> None:
        self.make("b.epub", "a.epub")
        given = [os.path.join(self.dir, n) for n in ("b.epub", "a.epub")]
        self.assertEqual(epub_io.collect_epubs(given), given)

    def test_case_does_not_matter_for_the_extension(self) -> None:
        self.make("A.EPUB", "b.Epub")
        self.assertEqual(len(epub_io.collect_epubs([self.dir])), 2)

    def test_other_files_are_ignored(self) -> None:
        self.make("book.epub", "notes.txt", "cover.jpg", "epub")
        self.assertEqual(len(epub_io.collect_epubs([self.dir])), 1)

    def test_the_same_book_twice_is_one_book(self) -> None:
        self.make("book.epub")
        book = os.path.join(self.dir, "book.epub")
        self.assertEqual(len(epub_io.collect_epubs([book, book, self.dir, book.upper()])), 1)

    def test_a_path_that_is_not_there_is_simply_absent(self) -> None:
        self.assertEqual(epub_io.collect_epubs([os.path.join(self.dir, "nope.epub")]), [])

    def test_an_empty_folder_gives_nothing(self) -> None:
        os.makedirs(os.path.join(self.dir, "empty"))
        self.assertEqual(epub_io.collect_epubs([self.dir]), [])


class Rewriting(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="efu_io_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_member_metadata_survives_a_rewrite(self) -> None:
        source = os.path.join(self.dir, "in.epub")
        with zipfile.ZipFile(source, "w") as zf:
            for name, method, when in (("mimetype", zipfile.ZIP_STORED, (2020, 5, 4, 3, 2, 0)),
                                       ("OEBPS/a.css", zipfile.ZIP_DEFLATED, (2021, 6, 5, 4, 3, 0)),
                                       ("OEBPS/b.xhtml", zipfile.ZIP_STORED, (2022, 7, 6, 5, 4, 0))):
                info = zipfile.ZipInfo(name, when)
                info.compress_type = method
                info.external_attr = 0o644 << 16
                info.comment = b"note"
                zf.writestr(info, b"x" * 40)

        entries = epub_io.read_epub(source)
        target = os.path.join(self.dir, "out.epub")
        epub_io.write_epub(target, entries)

        with zipfile.ZipFile(source) as a, zipfile.ZipFile(target) as b:
            for before, after in zip(a.infolist(), b.infolist()):
                self.assertEqual(before.filename, after.filename)
                self.assertEqual(before.date_time, after.date_time, before.filename)
                self.assertEqual(before.compress_type, after.compress_type, before.filename)
                self.assertEqual(before.external_attr, after.external_attr, before.filename)
                self.assertEqual(a.read(before), b.read(after), before.filename)

    def test_the_mimetype_entry_is_rebuilt_bare(self) -> None:
        source = os.path.join(self.dir, "in.epub")
        with zipfile.ZipFile(source, "w") as zf:
            info = zipfile.ZipInfo("mimetype", (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.extra = b"\x01\x00\x04\x00abcd"  # something to lose
            zf.writestr(info, epub_io.MIMETYPE_VALUE)
            zf.writestr("OEBPS/a.css", b"p{}")
        target = os.path.join(self.dir, "out.epub")
        epub_io.write_epub(target, epub_io.read_epub(source))
        with zipfile.ZipFile(target) as zf:
            first = zf.infolist()[0]
        self.assertEqual(first.filename, "mimetype")
        self.assertEqual(first.compress_type, zipfile.ZIP_STORED)
        self.assertEqual(first.extra, b"")

    def test_reading_an_empty_zip_is_an_error_not_an_empty_book(self) -> None:
        empty = os.path.join(self.dir, "empty.epub")
        with zipfile.ZipFile(empty, "w"):
            pass
        with self.assertRaises(epub_io.BrokenArchive):
            epub_io.read_epub(empty)

    def test_the_package_document_is_found_through_the_container(self) -> None:
        book = fixtures.standard(os.path.join(self.dir, "book.epub"))
        self.assertEqual(epub_io.opf_path(epub_io.read_epub(book)), "OEBPS/content.opf")

    def test_a_missing_or_broken_container_gives_no_package_path(self) -> None:
        entries = [epub_io.Entry(name="mimetype", data=epub_io.MIMETYPE_VALUE)]
        self.assertIsNone(epub_io.opf_path(entries))
        entries.append(epub_io.Entry(name=epub_io.CONTAINER_PATH, data=b"<not xml"))
        self.assertIsNone(epub_io.opf_path(entries))


if __name__ == "__main__":
    sys.exit(unittest.main())
