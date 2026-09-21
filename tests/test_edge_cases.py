"""The awkward cases: CSS that is not tidy, markup that only looks like markup,
encodings that are not UTF-8, and zips that are not shaped the usual way.

Several of these were written after the code got them wrong. The two that
mattered: a JavaScript string holding '<p style="...">' was being rewritten as
if it were a tag, and the legacy <!-- --> wrapper around a <style> block was
being deleted by the serializer.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading
import unittest
import zipfile

import fixtures  # noqa: E402  (inserts the repo root on sys.path)

import epub_io  # noqa: E402
import unlock  # noqa: E402
from unlock import Counts, Options  # noqa: E402


def css(source: str, opts: Options | None = None) -> tuple[str, Counts]:
    counts = Counts()
    out, _ = unlock.unlock_css(source, opts or Options(), counts)
    return out, counts


def page(source: str, opts: Options | None = None) -> tuple[str, Counts]:
    counts = Counts()
    out, _, errors = unlock.unlock_xhtml(source, opts or Options(), counts)
    assert not errors, errors
    return out, counts


class Stylesheets(unittest.TestCase):
    def test_minified(self) -> None:
        out, c = css("p{font-family:Batang;font-size:11pt;line-height:18px;margin:0 0 8px 0}",
                     Options(margin=True))
        self.assertEqual(out, "p{}")
        self.assertEqual((c.fonts, c.sizes, c.lines, c.margins), (1, 1, 1, 1))

    def test_uppercase_properties_units_and_important(self) -> None:
        out, c = css("p { Font-Family: Batang; FONT-SIZE: 12PX !important; LINE-HEIGHT: 18Pt; }")
        self.assertEqual((c.fonts, c.sizes, c.lines), (1, 1, 1))
        self.assertNotIn("12PX", out)

    def test_nested_at_rules(self) -> None:
        out, c = css("@media screen and (min-width:100px){@supports (display:flex){p{font-size:9pt}}}")
        self.assertEqual(c.sizes, 1)
        self.assertIn("@media screen and (min-width:100px)", out)
        self.assertIn("@supports (display:flex)", out)

    def test_import_and_namespace_survive(self) -> None:
        out, _ = css("@import url('a.css');\n@namespace svg url(http://www.w3.org/2000/svg);\np{font-size:9pt}")
        self.assertIn("@import", out)
        self.assertIn("@namespace", out)

    def test_page_rule_is_left_alone(self) -> None:
        out, c = css("@page { margin: 2cm; font-size: 10pt; }\np { font-size: 10pt; }",
                     Options(margin=True))
        self.assertIn("@page { margin: 2cm; font-size: 10pt; }", out)
        self.assertEqual(c.sizes, 1)  # only the p rule

    def test_calc_with_an_absolute_part_counts_as_absolute(self) -> None:
        _, c = css("p { font-size: calc(100% - 2px); line-height: calc(1em + 2px); }")
        self.assertEqual((c.sizes, c.lines), (1, 1))

    def test_var_without_absolute_units_is_kept(self) -> None:
        out, c = css("p { font-size: var(--s); line-height: var(--l); }")
        self.assertEqual((c.sizes, c.lines), (0, 0))
        self.assertIn("var(--s)", out)

    def test_custom_property_holding_a_font_is_left_but_the_use_goes(self) -> None:
        out, c = css(":root { --f: Batang; }\np { font-family: var(--f); }")
        self.assertIn("--f: Batang", out)
        self.assertEqual(c.fonts, 1)

    def test_comment_inside_a_value(self) -> None:
        _, c = css("p { font-family: /* why */ Batang, serif; }")
        self.assertEqual(sorted(c.families), ["Batang"])

    def test_bom_and_crlf_survive(self) -> None:
        out, _ = css("﻿p { font-size: 11pt; }")
        self.assertTrue(out.startswith("﻿"))
        out, _ = css("p { font-size: 11pt; }\r\nh1 { font-size: 2em; }\r\n")
        self.assertIn("\r\n", out)

    def test_url_containing_css_delimiters(self) -> None:
        out, _ = css("p { background: url('a;b}c.png'); font-size: 11pt; }")
        self.assertIn("a;b}c.png", out)

    def test_zero_font_size_and_line_height_are_kept(self) -> None:
        out, c = css("p { font-size: 0; line-height: 0; }")
        self.assertEqual((c.sizes, c.lines), (0, 0))
        self.assertIn("font-size: 0", out)

    def test_later_relative_value_survives_an_earlier_absolute_one(self) -> None:
        out, _ = css("p { font-size: 11pt; font-size: 1em; }")
        self.assertIn("1em", out)
        self.assertNotIn("11pt", out)

    def test_stray_comment_markers_make_the_file_skip(self) -> None:
        with self.assertRaises(unlock.CssParseError):
            css("p { font-size: 11pt; }\n<!-- legacy -->\nh1 { font-size: 2em; }")


class Selectors(unittest.TestCase):
    def test_margin_scope_by_subject_element(self) -> None:
        out, c = css("div p { margin: 12px } p div { margin: 12px } p, div { margin: 12px }",
                     Options(margin=True))
        self.assertEqual(c.margins, 3)
        self.assertNotIn("12px", out)

    def test_a_group_with_one_non_prose_selector_is_left_alone(self) -> None:
        for selector in ("p, table", "*, p", "html, body > div p:not(.x)", "td", ".note"):
            with self.subTest(selector=selector):
                out, c = css(selector + " { margin: 12px; }", Options(margin=True))
                self.assertEqual(c.margins, 0, out)

    def test_pseudo_elements_and_classes_on_p_still_count(self) -> None:
        for selector in ("p.first", "p::first-line", "p:not(.x)", "body > p", ".a + p", "div>p"):
            with self.subTest(selector=selector):
                _, c = css(selector + " { margin: 12px; }", Options(margin=True))
                self.assertEqual(c.margins, 1)


class FontShorthand(unittest.TestCase):
    def test_minified(self) -> None:
        out, c = css("p{font:bold 10pt/1.4 Batang}")
        self.assertIn("font-weight: bold", out)
        self.assertIn("line-height: 1.4", out)
        self.assertEqual(sorted(c.families), ["Batang"])

    def test_relative_size_with_absolute_line_height(self) -> None:
        out, c = css("p { font: 1.2em/14px serif; }", Options(font=False))
        self.assertIn("font-size: 1.2em", out)
        self.assertIn("serif", out)
        self.assertNotIn("14px", out)
        self.assertEqual((c.lines, c.fonts), (1, 0))

    def test_system_font_keyword_is_not_a_font_name(self) -> None:
        _, c = css("p { font: caption; }")
        self.assertEqual(c.families, set())
        self.assertEqual(c.fonts, 1)

    def test_a_value_we_do_not_understand_is_left_alone(self) -> None:
        out, c = css("p { font: something odd here; }")
        self.assertIn("font: something odd here", out)
        self.assertEqual(c.fonts, 0)

    def test_important_is_carried_to_every_longhand(self) -> None:
        out, _ = css("p { font: bold 10pt/1.4 Batang !important; }")
        self.assertIn("font-weight: bold !important", out)
        self.assertIn("line-height: 1.4 !important", out)


class Markup(unittest.TestCase):
    def test_script_body_is_never_rewritten(self) -> None:
        src = ('<p>a</p><script>var s = \'<p style="font-size:9pt">x</p>\';</script>'
               '<p style="font-size:9pt">b</p>')
        out, c = page(src)
        self.assertIn('var s = \'<p style="font-size:9pt">x</p>\';', out)
        self.assertIn("<p>b</p>", out)
        self.assertEqual(c.inline, 1)

    def test_commented_out_markup_is_left_alone(self) -> None:
        out, c = page('<!-- <p style="font-size:9pt"> --><p style="font-size:9pt">t</p>')
        self.assertIn('<!-- <p style="font-size:9pt"> -->', out)
        self.assertEqual(c.inline, 1)

    def test_legacy_comment_wrapper_around_a_style_block_survives(self) -> None:
        out, _ = page("<style><!-- p{font-size:9pt} --></style>")
        self.assertIn("<!--", out)
        self.assertIn("-->", out)
        self.assertNotIn("9pt", out)

    def test_cdata_wrapper_survives(self) -> None:
        out, _ = page("<style><![CDATA[ p{font-size:9pt} ]]></style>")
        self.assertIn("<![CDATA[", out)
        self.assertIn("]]>", out)
        self.assertNotIn("9pt", out)

    def test_an_attribute_value_that_quotes_markup_is_stepped_over(self) -> None:
        for tag in ('<img alt=\' style="font-size:9pt"\' src="a.png"/>',
                    '<p title="see style=\'font-size:9pt\'">t</p>'):
            with self.subTest(tag=tag):
                out, c = page(tag)
                self.assertEqual(out, tag)
                self.assertEqual(c.inline, 0)

    def test_uppercase_attribute_and_multiline_value(self) -> None:
        out, c = page('<p STYLE="FONT-SIZE: 9pt">t</p>')
        self.assertEqual(out, "<p>t</p>")
        out, c = page('<p style="font-size:\n 9pt;\n line-height: 2px">t</p>')
        self.assertEqual(out, "<p>t</p>")

    def test_empty_or_blank_style_attribute_is_untouched(self) -> None:
        src = '<p style="">t</p><p style="   ">t</p>'
        out, c = page(src)
        self.assertEqual(out, src)
        self.assertEqual(c.inline, 0)

    def test_unquoted_attribute_value_is_skipped(self) -> None:
        src = "<p style=font-size:9pt>t</p>"
        self.assertEqual(page(src)[0], src)

    def test_boolean_attribute_before_style(self) -> None:
        self.assertEqual(page('<input disabled style="font-size:9pt"/>')[0], "<input disabled/>")

    def test_namespaced_attributes(self) -> None:
        self.assertEqual(page('<p xml:lang="ko" style="font-size:9pt">t</p>')[0],
                         '<p xml:lang="ko">t</p>')

    def test_svg_inline_style(self) -> None:
        self.assertEqual(page('<svg><text style="font-size:9pt">t</text></svg>')[0],
                         "<svg><text>t</text></svg>")

    def test_a_tag_whose_attribute_holds_a_gt_is_left_alone_not_mangled(self) -> None:
        """A raw > inside an attribute value ends the tag as far as the scan is
        concerned, so that tag is missed. Missing it is fine; mangling is not."""
        src = '<div data-x="a>b" style="font-size:9pt">t</div>'
        self.assertEqual(page(src)[0], src)

    def test_several_style_blocks(self) -> None:
        out, _ = page("<style>p{font-size:9pt}</style><style>h1{font-size:8pt}</style>")
        self.assertNotIn("9pt", out)
        self.assertNotIn("8pt", out)


class Encodings(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="efu_enc_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def build(self, codec: str, declared: str, bom: bytes = b"") -> str:
        text = ('<?xml version="1.0" encoding="{enc}"?>\n'
                '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                '<p style="font-size: 11pt">한글 본문입니다.</p></body></html>\n').format(enc=declared)
        sheet = f'@charset "{declared}";\np {{ font-family: "바탕"; font-size: 11pt; }}\n'
        return fixtures.build(os.path.join(self.dir, f"{codec}.epub"), {
            "OEBPS/style.css": bom + sheet.encode(codec),
            "OEBPS/ch1.xhtml": bom + text.encode(codec),
        })

    def check(self, book: str) -> unlock.FileResult:
        before = fixtures.book_text(book)
        result = unlock.process_epub(book, unlock.Options())
        self.assertFalse(result.already_unlocked)
        self.assertEqual(before, fixtures.book_text(result.output_path))
        with zipfile.ZipFile(result.output_path) as zf:
            sheet = epub_io.decode(zf.read("OEBPS/style.css"))[0]
            self.assertNotIn("11pt", sheet)
            self.assertNotIn("바탕", sheet)
        return result

    def test_euc_kr(self) -> None:
        self.check(self.build("euc-kr", "euc-kr"))

    def test_utf_8_with_bom(self) -> None:
        self.check(self.build("utf-8", "utf-8", bom=b"\xef\xbb\xbf"))

    def test_utf_16_with_bom(self) -> None:
        for codec, bom in (("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")):
            with self.subTest(codec=codec):
                self.check(self.build(codec, "utf-16", bom=bom))

    def test_utf_16_without_a_bom_is_left_alone_rather_than_corrupted(self) -> None:
        book = self.build("utf-16-le", "utf-16")
        with open(book, "rb") as f:
            before = f.read()
        result = unlock.process_epub(book, unlock.Options())
        self.assertTrue(result.already_unlocked)
        with open(book, "rb") as f:
            self.assertEqual(before, f.read())


class ZipShapes(unittest.TestCase):
    CSS = b'p { font-family: Batang; font-size: 11pt; }\n'
    PAGE = ('<?xml version="1.0" encoding="utf-8"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>본문</p></body></html>'
            ).encode("utf-8")

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="efu_zip_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def make(self, name: str, entries: list[tuple[str, bytes, int]]) -> str:
        path = os.path.join(self.dir, name)
        with zipfile.ZipFile(path, "w") as zf:
            for entry, data, method in entries:
                info = zipfile.ZipInfo(entry, (2026, 1, 1, 0, 0, 0))
                info.compress_type = method
                zf.writestr(info, data)
        return path

    def content(self, opf: str = "OEBPS/content.opf") -> list[tuple[str, bytes, int]]:
        items = ('    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>\n'
                 '    <item id="css" href="style.css" media-type="text/css"/>')
        return [
            ("META-INF/container.xml", fixtures.CONTAINER.format(opf=opf).encode("utf-8"),
             zipfile.ZIP_DEFLATED),
            (opf, fixtures.OPF.format(items=items).encode("utf-8"), zipfile.ZIP_DEFLATED),
            ("OEBPS/style.css", self.CSS, zipfile.ZIP_DEFLATED),
            ("OEBPS/ch1.xhtml", self.PAGE, zipfile.ZIP_DEFLATED),
        ]

    def test_directory_entries_are_kept(self) -> None:
        book = self.make("d.epub", [("mimetype", b"application/epub+zip", zipfile.ZIP_STORED),
                                    ("META-INF/", b"", zipfile.ZIP_STORED),
                                    ("OEBPS/", b"", zipfile.ZIP_STORED)] + self.content())
        out = unlock.process_epub(book, unlock.Options()).output_path
        with zipfile.ZipFile(out) as zf:
            self.assertIn("OEBPS/", zf.namelist())
            self.assertIn("META-INF/", zf.namelist())

    def test_a_misplaced_mimetype_is_moved_to_the_front(self) -> None:
        book = self.make("m.epub", self.content() +
                         [("mimetype", b"application/epub+zip", zipfile.ZIP_DEFLATED)])
        out = unlock.process_epub(book, unlock.Options()).output_path
        with zipfile.ZipFile(out) as zf:
            first = zf.infolist()[0]
        self.assertEqual(first.filename, "mimetype")
        self.assertEqual(first.compress_type, zipfile.ZIP_STORED)

    def test_stored_content_stays_stored(self) -> None:
        book = self.make("s.epub", [("mimetype", b"application/epub+zip", zipfile.ZIP_STORED),
                                    ("META-INF/container.xml",
                                     fixtures.CONTAINER.format(opf="OEBPS/content.opf").encode("utf-8"),
                                     zipfile.ZIP_STORED),
                                    ("OEBPS/style.css", self.CSS, zipfile.ZIP_STORED),
                                    ("OEBPS/ch1.xhtml", self.PAGE, zipfile.ZIP_STORED)])
        out = unlock.process_epub(book, unlock.Options()).output_path
        with zipfile.ZipFile(out) as zf:
            for info in zf.infolist():
                self.assertEqual(info.compress_type, zipfile.ZIP_STORED, info.filename)

    def test_a_traversal_name_never_reaches_the_filesystem(self) -> None:
        """Members are read into memory and written into a new zip, never
        extracted, so ../.. in a name stays a name."""
        book = self.make("t.epub", [("mimetype", b"application/epub+zip", zipfile.ZIP_STORED)]
                         + self.content() + [("../../evil.css", self.CSS, zipfile.ZIP_DEFLATED)])
        out = unlock.process_epub(book, unlock.Options()).output_path
        self.assertFalse(os.path.exists(os.path.join(self.dir, "..", "..", "evil.css")))
        with zipfile.ZipFile(out) as zf:
            self.assertIn("../../evil.css", zf.namelist())

    def test_a_book_with_no_container_xml_still_works(self) -> None:
        book = self.make("n.epub", [("mimetype", b"application/epub+zip", zipfile.ZIP_STORED),
                                    ("OEBPS/style.css", self.CSS, zipfile.ZIP_DEFLATED),
                                    ("OEBPS/ch1.xhtml", self.PAGE, zipfile.ZIP_DEFLATED)])
        result = unlock.process_epub(book, unlock.Options())
        self.assertFalse(result.already_unlocked)

    def test_many_entries(self) -> None:
        entries = [("mimetype", b"application/epub+zip", zipfile.ZIP_STORED)] + self.content()
        entries += [(f"OEBPS/p{i}.xhtml",
                     self.PAGE.replace(b"<p>", b'<p style="font-size: 11pt">'), zipfile.ZIP_DEFLATED)
                    for i in range(500)]
        book = self.make("big.epub", entries)
        counts = unlock.analyze_epub(book).counts
        self.assertEqual(counts.inline, 500)
        out = unlock.process_epub(book, unlock.Options()).output_path
        with zipfile.ZipFile(out) as zf:
            self.assertEqual(len(zf.namelist()), len(entries))


class FontFiles(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="efu_font_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def build(self) -> str:
        items = ('    <item id="ch1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>\n'
                 '    <item id="css" href="css/style.css" media-type="text/css"/>\n'
                 '    <item id="f1" href="fonts/b%20my.ttf" media-type="font/ttf"></item>\n'
                 '    <item id="f2" href="fonts/other.otf" media-type="font/otf"/>\n'
                 '    <item id="cover" href="images/cover.jpg" media-type="image/jpeg"/>')
        path = os.path.join(self.dir, "f.epub")
        sheet = ('@font-face { font-family: "B"; src: url("../fonts/b my.ttf"); }\n'
                 'p { font-family: "B"; font-size: 11pt; }\n')
        page_src = ('<?xml version="1.0" encoding="utf-8"?>'
                    '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>본문</p></body></html>')
        with zipfile.ZipFile(path, "w") as zf:
            for name, data, method in [
                ("mimetype", b"application/epub+zip", zipfile.ZIP_STORED),
                ("META-INF/container.xml",
                 fixtures.CONTAINER.format(opf="EPUB/content.opf").encode("utf-8"), zipfile.ZIP_DEFLATED),
                ("EPUB/content.opf", fixtures.OPF.format(items=items).encode("utf-8"), zipfile.ZIP_DEFLATED),
                ("EPUB/css/style.css", sheet.encode("utf-8"), zipfile.ZIP_DEFLATED),
                ("EPUB/text/ch1.xhtml", page_src.encode("utf-8"), zipfile.ZIP_DEFLATED),
                ("EPUB/fonts/b my.ttf", b"font", zipfile.ZIP_DEFLATED),
                ("EPUB/fonts/other.otf", b"font", zipfile.ZIP_DEFLATED),
                ("EPUB/images/cover.jpg", b"jpeg", zipfile.ZIP_DEFLATED),
            ]:
                info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
                info.compress_type = method
                zf.writestr(info, data)
        return path

    def test_manifest_items_go_whatever_form_or_escaping_they_use(self) -> None:
        out = unlock.process_epub(self.build(), unlock.Options(remove_font_files=True)).output_path
        with zipfile.ZipFile(out) as zf:
            names = zf.namelist()
            opf = zf.read("EPUB/content.opf").decode("utf-8")
        self.assertNotIn("EPUB/fonts/b my.ttf", names)     # href was percent-encoded
        self.assertNotIn("EPUB/fonts/other.otf", names)    # <item></item> form
        self.assertNotIn("b%20my.ttf", opf)
        self.assertNotIn("other.otf", opf)
        self.assertIn("images/cover.jpg", opf)             # the cover is not a font
        self.assertIn("EPUB/images/cover.jpg", names)
        self.assertIn("<dc:title>", opf)                   # metadata untouched
        self.assertIn("<itemref idref=\"ch1\"/>", opf)     # spine untouched

    def test_fonts_stay_when_the_option_is_off(self) -> None:
        out = unlock.process_epub(self.build(), unlock.Options()).output_path
        with zipfile.ZipFile(out) as zf:
            self.assertIn("EPUB/fonts/b my.ttf", zf.namelist())
            self.assertIn("@font-face", zf.read("EPUB/css/style.css").decode("utf-8"))

    def test_counting_font_files_does_not_depend_on_the_option(self) -> None:
        self.assertEqual(unlock.analyze_epub(self.build()).counts.font_files, 2)


class Cancelling(unittest.TestCase):
    def test_a_cancel_part_way_through_writes_nothing(self) -> None:
        directory = tempfile.mkdtemp(prefix="efu_cancel_")
        self.addCleanup(shutil.rmtree, directory, True)
        book = fixtures.standard(os.path.join(directory, "book.epub"))
        cancel = threading.Event()
        seen = []

        def progress(index: int, total: int, name: str) -> None:
            seen.append(name)
            cancel.set()  # stop at the first content file, mid-book

        with self.assertRaises(unlock.Cancelled):
            unlock.process_epub(book, unlock.Options(), progress=progress, cancel=cancel)
        self.assertTrue(seen)
        self.assertEqual(os.listdir(directory), ["book.epub"])


if __name__ == "__main__":
    sys.exit(unittest.main())
