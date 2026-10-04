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

    def test_a_page_that_is_not_well_formed_xml_is_still_handled(self) -> None:
        """Real converted books turn up with a bare & or < in the text. We do
        not parse the markup, so they are still unlocked -- and the broken
        characters are handed back exactly as they came."""
        src = ('<?xml version="1.0" encoding="utf-8"?>\n'
               '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
               '<p style="font-size: 9pt">a & b, 5 < 6, AT&T</p>'
               '<p style="line-height: 14px">tail</p></body></html>')
        out, c = page(src)
        self.assertEqual(c.inline, 2)
        self.assertIn("a & b, 5 < 6, AT&T", out)
        self.assertNotIn("9pt", out)
        self.assertNotIn("14px", out)

    def test_several_style_blocks(self) -> None:
        out, _ = page("<style>p{font-size:9pt}</style><style>h1{font-size:8pt}</style>")
        self.assertNotIn("9pt", out)
        self.assertNotIn("8pt", out)


class Scaling(unittest.TestCase):
    """A chapter can hold tens of thousands of styled spans (Word and InDesign
    exports do), so the cost has to grow in step with the page, not with its
    square. The first version took 229 seconds for 64,000 spans in 6 MB."""

    SPAN = '<span style="font-family: Batang; font-size: 11pt">본문 글자</span> '

    def page(self, spans: int, comments: int = 0) -> str:
        """`comments` extra comments up front; each one used to cost a scan of
        every tag after it."""
        lead = "".join(f"<!-- page {i} -->" for i in range(comments))
        head = '<?xml version="1.0" encoding="utf-8"?>\n<html xmlns="http://www.w3.org/1999/xhtml">'
        return head + "<body>" + lead + self.SPAN * spans + "</body></html>"

    def timed(self, text: str) -> float:
        import time
        started = time.perf_counter()
        out, changed, errors = unlock.unlock_xhtml(text, Options(), Counts())
        took = time.perf_counter() - started
        self.assertTrue(changed)
        self.assertEqual(errors, [])
        self.assertNotIn("font-size", out)
        self.assertEqual(out.count("<span>"), text.count("<span "))
        return took

    def test_four_times_the_spans_is_about_four_times_the_work(self) -> None:
        small = min(self.timed(self.page(4000)) for _ in range(2))
        large = min(self.timed(self.page(16000)) for _ in range(2))
        # Linear is 4x; the quadratic version was 13x and climbing.
        self.assertLess(large / small, 9.0, f"{small:.3f}s -> {large:.3f}s")

    def test_many_comments_do_not_make_every_tag_slower(self) -> None:
        plain = min(self.timed(self.page(2000)) for _ in range(2))
        noisy = min(self.timed(self.page(2000, comments=8000)) for _ in range(2))
        self.assertLess(noisy / plain, 3.0, f"{plain:.3f}s -> {noisy:.3f}s")

    def test_the_replacements_land_in_the_right_places(self) -> None:
        """The single-pass rebuild must put every piece back where it was."""
        src = "".join(f'<p style="font-size: {10 + i % 5}pt">item {i}</p>' for i in range(300))
        out, _, _ = unlock.unlock_xhtml(src, Options(), Counts())
        self.assertEqual(out, "".join(f"<p>item {i}</p>" for i in range(300)))


class SkipRegions(unittest.TestCase):
    """The merged-span lookup that keeps comments and code out of the tag scan."""

    def test_merge_joins_nested_and_touching_spans(self) -> None:
        starts, ends = unlock._merge_spans([(10, 20), (12, 15), (20, 30), (40, 50), (0, 5)])
        self.assertEqual((starts, ends), ([0, 10, 40], [5, 30, 50]))

    def test_inside_is_half_open(self) -> None:
        starts, ends = unlock._merge_spans([(10, 20), (40, 50)])
        for pos, want in ((9, False), (10, True), (19, True), (20, False),
                          (39, False), (40, True), (49, True), (50, False), (0, False), (99, False)):
            self.assertEqual(unlock._inside(starts, ends, pos), want, pos)

    def test_inside_with_no_spans(self) -> None:
        self.assertFalse(unlock._inside(*unlock._merge_spans([]), 5))

    def test_a_comment_wrapping_a_style_block_hides_the_tags_in_it(self) -> None:
        src = ('<!-- <style>p{font-size:9pt}</style><p style="font-size:9pt">x</p> -->'
               '<p style="font-size:9pt">y</p>')
        out, c = page(src)
        self.assertIn('<p style="font-size:9pt">x</p>', out)   # inside the comment: untouched
        self.assertIn("<p>y</p>", out)                         # after it: unlocked
        self.assertEqual(c.inline, 1)


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


class SelfClosing(unittest.TestCase):
    """<script src="a.js"/> is a whole element. Read as an opening tag, its
    'body' ran on to the next </script> and ate real content."""

    def test_a_self_closing_script_does_not_swallow_the_page(self) -> None:
        src = ('<html><head><script src="a.js"/></head><body>'
               '<p style="font-size:9pt">before the real script</p>'
               "<script>var x = 1;</script>"
               '<p style="font-size:9pt">after</p></body></html>')
        out, c = page(src)
        self.assertEqual(c.inline, 2)
        self.assertNotIn("9pt", out)
        self.assertIn("<script>var x = 1;</script>", out)
        self.assertIn('<script src="a.js"/>', out)

    def test_a_self_closing_style_leaves_the_markup_alone(self) -> None:
        """It used to hand the markup to the CSS serializer, which rewrote
        class='a' as class="a" on the way through."""
        src = ('<html><head><style type="text/css"/></head><body>'
               "<p class='a' style=\"font-size:9pt\">kept text</p>"
               "<style>p{font-size:9pt}</style></body></html>")
        out, c = page(src)
        self.assertIn("class='a'", out)
        self.assertNotIn("9pt", out)
        self.assertIn("<style type=\"text/css\"/>", out)

    def test_an_ordinary_script_still_hides_its_body(self) -> None:
        src = '<script>var s = \'<p style="font-size:9pt">x</p>\';</script><p style="font-size:9pt">y</p>'
        out, c = page(src)
        self.assertIn('<p style="font-size:9pt">x</p>', out)
        self.assertIn("<p>y</p>", out)


class Nesting(unittest.TestCase):
    """CSS nesting is read by the parser as a broken declaration that swallows
    whatever follows it, so the declarations around it stayed in place while the
    run reported success."""

    def test_nested_rules_make_the_file_skip(self) -> None:
        with self.assertRaises(unlock.CssParseError):
            css("p { color: red; & span { font-size: 9pt; } font-size: 11pt; }")

    def test_nesting_inside_a_media_query_skips_too(self) -> None:
        with self.assertRaises(unlock.CssParseError):
            css("@media screen { p { & b { color: red; } font-size: 11pt; } }")

    def test_a_declaration_the_parser_rejects_does_not_stop_the_rest(self) -> None:
        """Old hacks and typos are ignored by browsers; the rest of the file is
        still worth unlocking. Only a nested block means the parse cannot be
        trusted."""
        out, c = css("p { *zoom: 1; font-size: 9pt; color: red; }")
        self.assertNotIn("9pt", out)
        self.assertIn("*zoom: 1", out)
        self.assertEqual(c.sizes, 1)

    def test_a_custom_property_holding_a_block_is_not_nesting(self) -> None:
        out, c = css("p { --x: { a: b }; font-size: 9pt; }")
        self.assertNotIn("9pt", out)


class CentredMargins(unittest.TestCase):
    def test_auto_keeps_a_block_centred(self) -> None:
        for value in ("0 auto 12px", "12px auto", "0 auto", "auto"):
            with self.subTest(value=value):
                out, c = css(f"div.wrap {{ margin: {value}; }}", Options(margin=True))
                self.assertIn("auto", out)
                self.assertEqual(c.margins, 0)

    def test_ordinary_absolute_margins_still_go(self) -> None:
        out, c = css("p { margin: 0 8px 0 0; padding: 4pt; }", Options(margin=True))
        self.assertEqual(out.strip(), "p { }")
        self.assertEqual(c.margins, 2)


class LyingDeclarations(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="efu_lie_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_utf_16_declared_over_utf_8_content(self) -> None:
        """.NET's XmlWriter over a StringWriter writes exactly this."""
        raw = ('<?xml version="1.0" encoding="utf-16"?>\n'
               '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
               '<p style="font-size: 9pt">한글 본문</p></body></html>').encode("utf-8")
        text, codec = epub_io.decode(raw)
        self.assertEqual(codec, "utf-8")
        self.assertIn("한글 본문", text)

    def test_the_book_is_unlocked_and_stays_utf_8_without_a_bom(self) -> None:
        raw = ('<?xml version="1.0" encoding="utf-16"?>\n'
               '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
               '<p style="font-size: 9pt">한글 본문</p></body></html>').encode("utf-8")
        book = fixtures.build(os.path.join(self.dir, "lie.epub"),
                              {"OEBPS/style.css": fixtures.CSS, "OEBPS/ch1.xhtml": raw})
        out = unlock.process_epub(book, unlock.Options()).output_path
        with zipfile.ZipFile(out) as zf:
            data = zf.read("OEBPS/ch1.xhtml")
        self.assertFalse(data.startswith((b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf")))
        self.assertIn("한글 본문".encode("utf-8"), data)
        self.assertNotIn(b"9pt", data)

    def test_a_declared_utf_32_is_ignored_too(self) -> None:
        raw = b'<?xml version="1.0" encoding="UTF-32"?><p>text</p>'
        self.assertEqual(epub_io.decode(raw)[1], "utf-8")


class ZipNames(unittest.TestCase):
    """A zip flags its names as UTF-8 or leaves them to be read as cp437. Tools
    that write UTF-8 without the flag produced names like 한글.css that came
    back as φò£Ω╕Ç.css, no longer matching the package document."""

    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="efu_names_")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def unflagged(self, real: bytes, placeholder: bytes) -> str:
        """A book whose one stylesheet is named by raw bytes with no UTF-8 flag.
        zipfile will not write that, so the name is patched in afterwards, in
        the local header and in the central directory."""
        assert len(real) == len(placeholder)
        path = os.path.join(self.dir, "in.epub")
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("mimetype", b"application/epub+zip")
            zf.writestr("META-INF/container.xml",
                        fixtures.CONTAINER.format(opf="OEBPS/content.opf").encode())
            zf.writestr("OEBPS/content.opf", b"<package/>")
            zf.writestr(f"OEBPS/{placeholder.decode()}.css", b"p { font-size: 9pt; }")
        with open(path, "rb") as f:
            data = f.read()
        self.assertEqual(data.count(placeholder), 2)
        with open(path, "wb") as f:
            f.write(data.replace(placeholder, real))
        return path

    def test_utf_8_names_without_the_flag_survive(self) -> None:
        book = self.unflagged("한글".encode("utf-8"), b"abcdef")
        out = unlock.process_epub(book, unlock.Options()).output_path
        with zipfile.ZipFile(out) as zf:
            self.assertIn("OEBPS/한글.css", zf.namelist())
            self.assertNotIn("p { font-size", zf.read("OEBPS/한글.css").decode())

    def test_the_output_flags_its_names_as_utf_8(self) -> None:
        book = self.unflagged("한글".encode("utf-8"), b"abcdef")
        out = unlock.process_epub(book, unlock.Options()).output_path
        with zipfile.ZipFile(out) as zf:
            info = zf.getinfo("OEBPS/한글.css")
        self.assertTrue(info.flag_bits & 0x800)

    def test_names_that_are_not_utf_8_are_refused_not_garbled(self) -> None:
        book = self.unflagged("한글".encode("cp949"), b"abcd")
        with self.assertRaises(unlock.BrokenArchive):
            unlock.analyze_epub(book)
        with self.assertRaises(unlock.BrokenArchive):
            unlock.process_epub(book, unlock.Options())
        self.assertEqual(sorted(os.listdir(self.dir)), ["in.epub"])

    def test_ordinary_ascii_and_flagged_names_are_untouched(self) -> None:
        book = fixtures.standard(os.path.join(self.dir, "plain.epub"))
        out = unlock.process_epub(book, unlock.Options()).output_path
        with zipfile.ZipFile(book) as a, zipfile.ZipFile(out) as b:
            self.assertEqual(a.namelist(), b.namelist())
        flagged = os.path.join(self.dir, "flagged.epub")
        with zipfile.ZipFile(flagged, "w") as zf:
            zf.writestr("mimetype", b"application/epub+zip")
            zf.writestr("OEBPS/한글.css", b"p { font-size: 9pt; }")
        out = unlock.process_epub(flagged, unlock.Options()).output_path
        with zipfile.ZipFile(out) as zf:
            self.assertIn("OEBPS/한글.css", zf.namelist())


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


class ManifestMatching(unittest.TestCase):
    """A manifest item left pointing at a deleted font is a hard epubcheck
    error, so every spelling of the item and of its href has to be recognised."""

    CSS_ITEM = '<item id="c" href="c.css" media-type="text/css"/>'

    def strip(self, item: str, deleted: str) -> str:
        text = f"<manifest>\n  {item}\n  {self.CSS_ITEM}\n</manifest>"
        return unlock._strip_font_manifest_items(text, "OEBPS", {f"OEBPS/fonts/{deleted}"})

    def test_each_spelling_of_the_item_and_of_the_href(self) -> None:
        cases = [
            ("plain", '<item id="f" href="fonts/a.ttf" media-type="font/ttf"/>', "a.ttf"),
            ("long form", '<item id="f" href="fonts/a.ttf" media-type="font/ttf"></item>', "a.ttf"),
            ("prefixed element", '<opf:item id="f" href="fonts/a.ttf" media-type="font/ttf"/>', "a.ttf"),
            ("prefixed long form",
             '<opf:item id="f" href="fonts/a.ttf" media-type="font/ttf"></opf:item>', "a.ttf"),
            ("ampersand", '<item id="f" href="fonts/a&amp;b.ttf" media-type="font/ttf"/>', "a&b.ttf"),
            ("percent escape", '<item id="f" href="fonts/a%20b.ttf" media-type="font/ttf"/>', "a b.ttf"),
            ("both", '<item id="f" href="fonts/a%20b&amp;c.ttf" media-type="font/ttf"/>', "a b&c.ttf"),
            ("single quotes", "<item id='f' href='fonts/a.ttf' media-type='font/ttf'/>", "a.ttf"),
            ("parent directory", '<item id="f" href="../OEBPS/fonts/a.ttf" media-type="font/ttf"/>', "a.ttf"),
        ]
        for label, item, deleted in cases:
            with self.subTest(label=label):
                result = self.strip(item, deleted)
                self.assertNotIn("fonts/", result)
                self.assertIn(self.CSS_ITEM, result)  # the neighbour is untouched

    def test_an_item_for_something_else_is_kept(self) -> None:
        item = '<item id="i" href="fonts/a.ttf.jpg" media-type="image/jpeg"/>'
        self.assertIn(item, self.strip(item, "a.ttf"))
        item = '<item id="f" href="fonts/other.ttf" media-type="font/ttf"/>'
        self.assertIn(item, self.strip(item, "a.ttf"))

    def test_a_font_with_an_ampersand_in_its_name_goes_cleanly(self) -> None:
        directory = tempfile.mkdtemp(prefix="efu_amp_")
        self.addCleanup(shutil.rmtree, directory, True)
        items = ('    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>\n'
                 '    <item id="css" href="style.css" media-type="text/css"/>\n'
                 '    <item id="font" href="a&amp;b.ttf" media-type="font/ttf"/>')
        book = fixtures.build(os.path.join(directory, "amp.epub"),
                              {"OEBPS/style.css": fixtures.FONT_CSS, "OEBPS/ch1.xhtml": fixtures.XHTML,
                               "OEBPS/a&b.ttf": b"font" * 40}, opf_items=items)
        out = unlock.process_epub(book, unlock.Options(remove_font_files=True)).output_path
        with zipfile.ZipFile(out) as zf:
            self.assertNotIn("OEBPS/a&b.ttf", zf.namelist())
            self.assertNotIn("a&amp;b.ttf", zf.read("OEBPS/content.opf").decode("utf-8"))


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
