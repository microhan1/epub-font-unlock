"""What counts as DRM, and what does not.

encryption.xml is not only for DRM: the EPUB spec uses it to list embedded
fonts that have been obfuscated, and Sigil and InDesign write it whenever they
embed one. Those are the books whose fonts are pinned down -- the ones this tool
exists for -- and treating the file's mere presence as DRM refused all of them.

The rule now is narrow: the book is unlocked only if every entry in
encryption.xml names a font-obfuscation algorithm and nothing carries a key.
Anything else is refused, because the two mistakes are not equal. Turning away
a book that was only obfuscated sends its owner somewhere else; unlocking one
that is really encrypted would rewrite ciphertext.
"""
from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile

import fixtures  # noqa: E402  (inserts the repo root on sys.path)

import epub_io  # noqa: E402
import i18n  # noqa: E402
import main as cli  # noqa: E402
import unlock  # noqa: E402

CONTENT = {"OEBPS/style.css": fixtures.CSS, "OEBPS/ch1.xhtml": fixtures.XHTML}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="efu_drm_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        original = i18n.SETTINGS_PATH
        i18n.SETTINGS_PATH = os.path.join(self.dir, "settings.json")
        self.addCleanup(setattr, i18n, "SETTINGS_PATH", original)
        i18n.save_settings({"lang": "en"})
        i18n.init("en")
        self.addCleanup(i18n.init, "en")

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    def book(self, name: str = "book.epub", **kwargs) -> str:
        return fixtures.build(self.path(name), dict(CONTENT), **kwargs)

    def refused(self, book: str) -> bool:
        try:
            unlock.analyze_epub(book)
        except unlock.DrmProtected:
            return True
        return False

    def zip_has(self, path: str, name: str) -> bool:
        with zipfile.ZipFile(path) as zf:
            return name in zf.namelist()


class RealDrmIsStillRefused(Base):
    def test_content_under_aes(self) -> None:
        self.assertTrue(self.refused(self.book(encryption=True)))

    def test_process_refuses_it_too_and_writes_nothing(self) -> None:
        book = self.book(encryption=True)
        with self.assertRaises(unlock.DrmProtected):
            unlock.process_epub(book, unlock.Options())
        self.assertEqual(os.listdir(self.dir), ["book.epub", "settings.json"])

    def test_an_obfuscated_font_does_not_excuse_an_encrypted_chapter(self) -> None:
        mixed = fixtures.encryption_xml(("OEBPS/bundled.ttf", fixtures.IDPF),
                                        ("OEBPS/ch1.xhtml", fixtures.AES))
        self.assertTrue(self.refused(self.book(encryption=mixed)))

    def test_any_unknown_algorithm(self) -> None:
        for algorithm in ("http://example.com/some-drm", "http://www.w3.org/2001/04/xmlenc#aes256-cbc",
                          "", "http://www.idpf.org/2008/embedding/extra"):
            with self.subTest(algorithm=algorithm):
                xml = fixtures.encryption_xml(("OEBPS/bundled.ttf", algorithm))
                self.assertTrue(self.refused(self.book(encryption=xml)))

    def test_an_entry_with_no_algorithm_at_all(self) -> None:
        xml = (fixtures.ENC_HEAD + "<enc:EncryptedData><enc:CipherData>"
               '<enc:CipherReference URI="OEBPS/ch1.xhtml"/></enc:CipherData>'
               "</enc:EncryptedData></encryption>")
        self.assertTrue(self.refused(self.book(encryption=xml)))

    def test_a_wrapped_key(self) -> None:
        xml = (fixtures.ENC_HEAD
               + fixtures.encrypted_data("OEBPS/bundled.ttf", fixtures.IDPF)
               + "<enc:EncryptedKey><enc:EncryptionMethod Algorithm=\"x\"/></enc:EncryptedKey>"
               "</encryption>")
        self.assertTrue(self.refused(self.book(encryption=xml)))

    def test_an_encryption_file_that_does_not_parse(self) -> None:
        for broken in ("<encryption><oops", "not xml at all", b"\x00\x01\x02", " "):
            with self.subTest(broken=broken):
                self.assertTrue(self.refused(self.book(encryption=broken)))

    def test_a_rights_file_alone(self) -> None:
        """ADEPT and FairPlay leave one beside encryption.xml; if only that
        survives, the book is still not ours to open."""
        self.assertTrue(self.refused(self.book(rights=True)))

    def test_apple_sinf(self) -> None:
        book = self.book()
        with zipfile.ZipFile(book, "a") as zf:
            zf.writestr("META-INF/sinf.xml", b"<sinf/>")
        self.assertTrue(self.refused(book))

    def test_the_cli_says_drm_and_does_not_fail_the_run(self) -> None:
        book = self.book(encryption=True)
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = cli.run_cli(cli.build_parser().parse_args([book, "--font"]))
        finally:
            sys.stdout, sys.stderr = old
        self.assertEqual(code, 0)
        self.assertIn("DRM", err.getvalue())


class ObfuscatedFontsAreNotDrm(Base):
    def test_both_algorithms_are_accepted(self) -> None:
        for algorithm in (fixtures.IDPF, fixtures.ADOBE_FONTS):
            with self.subTest(algorithm=algorithm):
                book = fixtures.with_obfuscated_font(self.path("b.epub"), algorithm)
                self.assertFalse(self.refused(book))
                os.remove(book)

    def test_the_book_is_unlocked_and_its_text_is_untouched(self) -> None:
        book = fixtures.with_obfuscated_font(self.path("book.epub"))
        before = fixtures.book_text(book)
        result = unlock.process_epub(book, unlock.Options())
        self.assertFalse(result.already_unlocked)
        self.assertEqual(before, fixtures.book_text(result.output_path))
        with zipfile.ZipFile(result.output_path) as zf:
            css = zf.read("OEBPS/style.css").decode()
        self.assertNotIn("font-size", css)

    def test_the_font_and_encryption_xml_are_left_byte_for_byte(self) -> None:
        """Obfuscated bytes are the font's own business; touching one would
        corrupt it."""
        book = fixtures.with_obfuscated_font(self.path("book.epub"))
        out = unlock.process_epub(book, unlock.Options()).output_path
        with zipfile.ZipFile(book) as a, zipfile.ZipFile(out) as b:
            for name in ("OEBPS/bundled.ttf", "META-INF/encryption.xml"):
                self.assertEqual(a.read(name), b.read(name), name)

    def test_analysis_counts_the_obfuscated_font(self) -> None:
        book = fixtures.with_obfuscated_font(self.path("book.epub"))
        self.assertEqual(unlock.analyze_epub(book).counts.font_files, 1)

    def test_running_twice_finds_nothing_more_to_unlock(self) -> None:
        book = fixtures.with_obfuscated_font(self.path("book.epub"))
        once = unlock.process_epub(book, unlock.Options()).output_path
        self.assertTrue(unlock.process_epub(once, unlock.Options()).already_unlocked)

    def test_an_empty_encryption_file_describes_nothing_and_is_fine(self) -> None:
        self.assertFalse(self.refused(self.book(encryption="<encryption/>")))

    def test_element_prefixes_do_not_matter(self) -> None:
        """A default-namespace file and a prefixed one say the same thing."""
        xml = ('<?xml version="1.0"?>\n'
               '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"'
               ' xmlns:e="http://www.w3.org/2001/04/xmlenc#">'
               + fixtures.encrypted_data("OEBPS/bundled.ttf", fixtures.IDPF, prefix="e:")
               + "</encryption>")
        self.assertFalse(self.refused(self.book(encryption=xml)))

    def test_the_cli_processes_it(self) -> None:
        book = fixtures.with_obfuscated_font(self.path("book.epub"))
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = cli.run_cli(cli.build_parser().parse_args([book, "--font", "--size"]))
        finally:
            sys.stdout, sys.stderr = old
        self.assertEqual(code, 0)
        self.assertNotIn("DRM", err.getvalue())
        self.assertIn("book_unlocked.epub", out.getvalue())


class RemovingObfuscatedFonts(Base):
    """Delete the font and its encryption.xml entry has to go with it, or the
    book fails epubcheck (RSC-007: a resource that is not in the EPUB)."""

    def test_the_entry_and_the_file_go_when_nothing_is_left(self) -> None:
        book = fixtures.with_obfuscated_font(self.path("book.epub"))
        out = unlock.process_epub(book, unlock.Options(remove_font_files=True)).output_path
        self.assertFalse(self.zip_has(out, "OEBPS/bundled.ttf"))
        self.assertFalse(self.zip_has(out, "META-INF/encryption.xml"))

    def test_the_manifest_item_goes_too(self) -> None:
        book = fixtures.with_obfuscated_font(self.path("book.epub"))
        out = unlock.process_epub(book, unlock.Options(remove_font_files=True)).output_path
        with zipfile.ZipFile(out) as zf:
            self.assertNotIn("bundled.ttf", zf.read("OEBPS/content.opf").decode())

    def test_other_entries_survive_and_the_file_stays(self) -> None:
        """An obfuscated resource that is not a deleted font keeps its entry."""
        xml = fixtures.encryption_xml(("OEBPS/bundled.ttf", fixtures.IDPF),
                                      ("OEBPS/cover.jpg", fixtures.IDPF))
        items = ('    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>\n'
                 '    <item id="css" href="style.css" media-type="text/css"/>\n'
                 '    <item id="font" href="bundled.ttf" media-type="font/ttf"/>')
        book = fixtures.build(self.path("book.epub"),
                              {**CONTENT, "OEBPS/bundled.ttf": b"font" * 400,
                               "OEBPS/cover.jpg": b"jpeg" * 400},
                              encryption=xml, opf_items=items)
        out = unlock.process_epub(book, unlock.Options(remove_font_files=True)).output_path
        with zipfile.ZipFile(out) as zf:
            kept = zf.read("META-INF/encryption.xml").decode()
        self.assertNotIn("bundled.ttf", kept)
        self.assertIn("OEBPS/cover.jpg", kept)
        self.assertIn("</encryption>", kept)

    def test_strip_matches_every_spelling_of_the_uri(self) -> None:
        for label, uri, deleted in [
            ("plain", "OEBPS/f.ttf", "OEBPS/f.ttf"),
            ("leading slash", "/OEBPS/f.ttf", "OEBPS/f.ttf"),
            ("percent", "OEBPS/a%20b.ttf", "OEBPS/a b.ttf"),
            ("ampersand", "OEBPS/a&amp;b.ttf", "OEBPS/a&b.ttf"),
            ("dot segments", "OEBPS/./f.ttf", "OEBPS/f.ttf"),
        ]:
            with self.subTest(label=label):
                xml = fixtures.encryption_xml((uri, fixtures.IDPF))
                result = unlock._strip_encrypted_fonts(xml, {deleted})
                self.assertNotIn("EncryptedData", result)

    def test_strip_keeps_the_neighbours_byte_for_byte(self) -> None:
        xml = fixtures.encryption_xml(("OEBPS/gone.ttf", fixtures.IDPF),
                                      ("OEBPS/stay.ttf", fixtures.IDPF))
        result = unlock._strip_encrypted_fonts(xml, {"OEBPS/gone.ttf"})
        self.assertEqual(result, fixtures.encryption_xml(("OEBPS/stay.ttf", fixtures.IDPF)))

    def test_strip_with_prefixed_and_unprefixed_elements(self) -> None:
        for prefix in ("enc:", "", "x:"):
            with self.subTest(prefix=prefix):
                xml = (fixtures.ENC_HEAD
                       + fixtures.encrypted_data("OEBPS/f.ttf", fixtures.IDPF, prefix=prefix)
                       + "</encryption>")
                self.assertNotIn("EncryptedData", unlock._strip_encrypted_fonts(xml, {"OEBPS/f.ttf"}))

    def test_a_book_without_encryption_xml_is_not_disturbed(self) -> None:
        book = fixtures.with_font(self.path("book.epub"))
        out = unlock.process_epub(book, unlock.Options(remove_font_files=True)).output_path
        self.assertFalse(self.zip_has(out, "META-INF/encryption.xml"))
        self.assertFalse(self.zip_has(out, "OEBPS/bundled.ttf"))


class Detection(unittest.TestCase):
    """only_font_obfuscation on its own, with no zip around it."""

    def test_verdicts(self) -> None:
        enc = fixtures.encryption_xml
        cases = [
            ("idpf font", enc(("a.ttf", fixtures.IDPF)).encode(), True),
            ("adobe font", enc(("a.ttf", fixtures.ADOBE_FONTS)).encode(), True),
            ("two fonts", enc(("a.ttf", fixtures.IDPF), ("b.otf", fixtures.ADOBE_FONTS)).encode(), True),
            ("nothing listed", b"<encryption/>", True),
            ("aes", enc(("a.xhtml", fixtures.AES)).encode(), False),
            ("font plus aes", enc(("a.ttf", fixtures.IDPF), ("c.xhtml", fixtures.AES)).encode(), False),
            ("garbage", b"<<<", False),
            ("empty bytes", b"", False),
        ]
        for label, xml, want in cases:
            with self.subTest(label=label):
                self.assertEqual(epub_io.only_font_obfuscation(xml), want)


if __name__ == "__main__":
    sys.exit(unittest.main())
