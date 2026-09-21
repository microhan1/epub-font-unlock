"""What the tool promises: the words do not change, the relative sizes stay,
the absolute ones go, and the original file is never touched.

    python -m unittest discover -s tests
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

import fixtures  # noqa: E402  (inserts the repo root on sys.path)

import epub_io  # noqa: E402
import unlock  # noqa: E402


def sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="efu_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def css_of(self, epub: str, name: str = "OEBPS/style.css") -> str:
        with zipfile.ZipFile(epub) as zf:
            return zf.read(name).decode("utf-8")


class TextIdentity(Base):
    def test_body_text_is_identical(self) -> None:
        book = fixtures.standard(self.path("book.epub"))
        before = fixtures.book_text(book)
        result = unlock.process_epub(book, unlock.Options(margin=True, remove_font_files=True))
        self.assertEqual(before, fixtures.book_text(result.output_path))

    def test_samples_text_is_identical(self) -> None:
        """The two committed sample books, which carry inline styles, a style
        element, a font shorthand and an embedded font."""
        samples = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")
        for name in ("sample_epub2.epub", "sample_epub3.epub"):
            src = os.path.join(samples, name)
            if not os.path.exists(src):
                self.skipTest("run samples/make_samples.py first")
            copy = shutil.copy(src, self.path(name))
            with self.subTest(name=name):
                before = fixtures.book_text(copy)
                result = unlock.process_epub(copy, unlock.Options(margin=True, remove_font_files=True))
                self.assertEqual(before, fixtures.book_text(result.output_path))

    def test_text_node_mentioning_style_is_left_alone(self) -> None:
        book = fixtures.standard(self.path("book.epub"))
        result = unlock.process_epub(book, unlock.Options())
        with zipfile.ZipFile(result.output_path) as zf:
            page = zf.read("OEBPS/ch1.xhtml").decode("utf-8")
        self.assertIn('style="font-size: 99px" 라는 글자가', page)


class Styles(Base):
    def test_relative_sizes_survive(self) -> None:
        book = fixtures.standard(self.path("book.epub"))
        css = self.css_of(unlock.process_epub(book, unlock.Options()).output_path)
        self.assertIn("1.6em", css)
        self.assertIn("130%", css)

    def test_absolute_values_are_gone(self) -> None:
        book = fixtures.standard(self.path("book.epub"))
        css = self.css_of(unlock.process_epub(book, unlock.Options()).output_path)
        self.assertNotIn("font-family", css)
        self.assertNotIn("11pt", css)
        self.assertNotIn("18px", css)

    def test_margin_cleanup_is_scoped_to_p_and_div(self) -> None:
        book = fixtures.standard(self.path("book.epub"))
        css = self.css_of(unlock.process_epub(book, unlock.Options(margin=True)).output_path)
        self.assertNotIn("8px", css)          # p { margin: 0 0 8px 0 }
        self.assertIn("table td { margin: 4px; padding: 2pt; }", css)

    def test_margin_is_off_by_default(self) -> None:
        book = fixtures.standard(self.path("book.epub"))
        css = self.css_of(unlock.process_epub(book, unlock.Options()).output_path)
        self.assertIn("margin: 0 0 8px 0", css)

    def test_zero_margin_is_kept(self) -> None:
        counts = unlock.Counts()
        out, changed = unlock.unlock_css("p { margin: 0; }", unlock.Options(margin=True), counts)
        self.assertFalse(changed)
        self.assertEqual(out, "p { margin: 0; }")

    def test_font_shorthand_keeps_weight_and_relative_line_height(self) -> None:
        counts = unlock.Counts()
        out, _ = unlock.unlock_css("p { font: italic bold 10pt/1.4 Batang, serif; }",
                                   unlock.Options(), counts)
        self.assertIn("font-style: italic", out)
        self.assertIn("font-weight: bold", out)
        self.assertIn("line-height: 1.4", out)
        self.assertNotIn("10pt", out)
        self.assertNotIn("Batang", out)

    def test_inline_style_emptied_drops_the_attribute(self) -> None:
        book = fixtures.standard(self.path("book.epub"))
        result = unlock.process_epub(book, unlock.Options())
        with zipfile.ZipFile(result.output_path) as zf:
            page = zf.read("OEBPS/ch1.xhtml").decode("utf-8")
        self.assertIn("<p>첫 문단이다.</p>", page)
        self.assertIn('<p style="color: #444;">둘째 문단이다.</p>', page)

    def test_font_face_stays_unless_asked(self) -> None:
        book = fixtures.with_font(self.path("book.epub"))
        result = unlock.process_epub(book, unlock.Options())
        with zipfile.ZipFile(result.output_path) as zf:
            self.assertIn("OEBPS/bundled.ttf", zf.namelist())
            self.assertIn("@font-face", zf.read("OEBPS/style.css").decode("utf-8"))

    def test_removing_fonts_also_clears_the_manifest_item(self) -> None:
        book = fixtures.with_font(self.path("book.epub"))
        result = unlock.process_epub(book, unlock.Options(remove_font_files=True))
        with zipfile.ZipFile(result.output_path) as zf:
            names = zf.namelist()
            opf = zf.read("OEBPS/content.opf").decode("utf-8")
            css = zf.read("OEBPS/style.css").decode("utf-8")
        self.assertNotIn("OEBPS/bundled.ttf", names)
        self.assertNotIn("bundled.ttf", opf)
        self.assertNotIn("@font-face", css)
        self.assertIn('id="ch1"', opf)  # the rest of the manifest is untouched


class Container(Base):
    def test_mimetype_is_first_stored_and_bare(self) -> None:
        book = fixtures.standard(self.path("book.epub"))
        out = unlock.process_epub(book, unlock.Options()).output_path
        with zipfile.ZipFile(out) as zf:
            first = zf.infolist()[0]
        self.assertEqual(first.filename, "mimetype")
        self.assertEqual(first.compress_type, zipfile.ZIP_STORED)
        self.assertEqual(first.extra, b"")
        self.assertEqual(zipfile.ZipFile(out).read("mimetype"), b"application/epub+zip")

    def test_entry_order_and_compression_are_preserved(self) -> None:
        book = fixtures.standard(self.path("book.epub"))
        out = unlock.process_epub(book, unlock.Options()).output_path
        with zipfile.ZipFile(book) as a, zipfile.ZipFile(out) as b:
            self.assertEqual([i.filename for i in a.infolist()], [i.filename for i in b.infolist()])
            self.assertEqual([i.compress_type for i in a.infolist()],
                             [i.compress_type for i in b.infolist()])

    def test_untouched_entries_keep_their_bytes(self) -> None:
        book = fixtures.standard(self.path("book.epub"))
        out = unlock.process_epub(book, unlock.Options()).output_path
        with zipfile.ZipFile(book) as a, zipfile.ZipFile(out) as b:
            for name in ("META-INF/container.xml", "OEBPS/content.opf"):
                self.assertEqual(a.read(name), b.read(name), name)

    def test_original_is_never_modified(self) -> None:
        book = fixtures.standard(self.path("book.epub"))
        before = sha256(book)
        unlock.process_epub(book, unlock.Options(margin=True, remove_font_files=True))
        self.assertEqual(before, sha256(book))

    def test_output_is_numbered_when_the_name_is_taken(self) -> None:
        book = fixtures.standard(self.path("book.epub"))
        first = unlock.process_epub(book, unlock.Options()).output_path
        self.assertTrue(first.endswith("book_unlocked.epub"))
        os.makedirs(self.path("book_unlocked(2).epub"))  # a folder counts as taken
        second = unlock.process_epub(book, unlock.Options()).output_path
        self.assertTrue(second.endswith("book_unlocked(3).epub"), second)


class WellFormed(Base):
    """epubcheck is not run here, but every XML document in the output has to
    still parse -- that is the failure mode a style-attribute rewrite could
    plausibly cause."""

    XML_EXTS = (".xhtml", ".html", ".htm", ".xht", ".opf", ".ncx", ".xml")

    def assert_parses(self, epub: str) -> None:
        from xml.etree import ElementTree
        with zipfile.ZipFile(epub) as zf:
            for name in zf.namelist():
                if name.lower().endswith(self.XML_EXTS):
                    try:
                        ElementTree.fromstring(zf.read(name))
                    except ElementTree.ParseError as exc:
                        self.fail(f"{name} no longer parses: {exc}")

    def test_fixture_output_parses(self) -> None:
        book = fixtures.standard(self.path("book.epub"))
        self.assert_parses(unlock.process_epub(
            book, unlock.Options(margin=True, remove_font_files=True)).output_path)

    def test_font_removal_output_parses(self) -> None:
        book = fixtures.with_font(self.path("book.epub"))
        self.assert_parses(unlock.process_epub(
            book, unlock.Options(remove_font_files=True)).output_path)

    def test_samples_output_parses(self) -> None:
        samples = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")
        for name in ("sample_epub2.epub", "sample_epub3.epub"):
            src = os.path.join(samples, name)
            if not os.path.exists(src):
                self.skipTest("run samples/make_samples.py first")
            copy = shutil.copy(src, self.path(name))
            with self.subTest(name=name):
                self.assert_parses(unlock.process_epub(
                    copy, unlock.Options(margin=True, remove_font_files=True)).output_path)

    def test_single_quoted_inline_font_does_not_break_the_attribute(self) -> None:
        """serialize() writes strings with double quotes; inside a
        double-quoted attribute that would end it early."""
        page = ('<?xml version="1.0" encoding="utf-8"?>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                '<p style="font-size: 12px; content: \'x\'">본문</p></body></html>\n')
        book = fixtures.build(self.path("q.epub"),
                              {"OEBPS/style.css": fixtures.CSS, "OEBPS/ch1.xhtml": page})
        out = unlock.process_epub(book, unlock.Options()).output_path
        self.assert_parses(out)
        with zipfile.ZipFile(out) as zf:
            self.assertIn("&#34;x&#34;", zf.read("OEBPS/ch1.xhtml").decode("utf-8"))


class Analysis(Base):
    def test_counts_match_the_fixture(self) -> None:
        book = fixtures.standard(self.path("book.epub"))
        counts = unlock.analyze_epub(book).counts
        self.assertEqual(counts.family_count, 2)   # Nanum Myeongjo, Batang
        self.assertEqual(counts.sizes, 2)          # 11pt in the css and 11pt inline; 1.6em is relative
        self.assertEqual(counts.lines, 2)          # 18px in css and inline
        self.assertEqual(counts.margins, 1)        # p only; table td excluded
        self.assertEqual(counts.inline, 2)

    def test_analysis_writes_nothing(self) -> None:
        book = fixtures.standard(self.path("book.epub"))
        unlock.analyze_epub(book)
        self.assertEqual(os.listdir(self.dir), ["book.epub"])

    def test_already_unlocked_produces_no_file(self) -> None:
        book = fixtures.already_unlocked(self.path("book.epub"))
        result = unlock.process_epub(book, unlock.Options())
        self.assertTrue(result.already_unlocked)
        self.assertEqual(os.listdir(self.dir), ["book.epub"])

    def test_running_twice_says_already_unlocked(self) -> None:
        book = fixtures.standard(self.path("book.epub"))
        first = unlock.process_epub(book, unlock.Options()).output_path
        again = unlock.process_epub(first, unlock.Options())
        self.assertTrue(again.already_unlocked)


if __name__ == "__main__":
    sys.exit(unittest.main())
