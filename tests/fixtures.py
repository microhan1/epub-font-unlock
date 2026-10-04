"""Builders for the books the tests run against.

Fixtures are made here rather than committed, so the suite stays runnable on a
clean checkout and nothing copyrighted ever enters the repository.
"""
from __future__ import annotations

import os
import re
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CONTAINER = (
    '<?xml version="1.0" encoding="utf-8"?>\n'
    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
    '  <rootfiles>\n'
    '    <rootfile full-path="{opf}" media-type="application/oebps-package+xml"/>\n'
    '  </rootfiles>\n'
    '</container>\n'
)

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>시험용 책</dc:title>
    <dc:language>ko</dc:language>
    <dc:identifier id="bookid">urn:uuid:fixture</dc:identifier>
  </metadata>
  <manifest>
{items}
  </manifest>
  <spine>
    <itemref idref="ch1"/>
  </spine>
</package>
"""

CSS = """p {
  font-family: "Nanum Myeongjo", serif;
  font-size: 11pt;
  line-height: 18px;
  margin: 0 0 8px 0;
}
h1 { font-size: 1.6em; line-height: 130%; }
table td { margin: 4px; padding: 2pt; }
"""

XHTML = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <title>본문</title>
  <link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body>
  <h1>제목은 크게</h1>
  <p style="font-size: 11pt; line-height: 18px">첫 문단이다.</p>
  <p style="font-family: 'Batang'; color: #444">둘째 문단이다.</p>
  <p>여기 본문에 style="font-size: 99px" 라는 글자가 그대로 들어 있다.</p>
</body>
</html>
"""

PLAIN_XHTML = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
<head><title>본문</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body><p>아무것도 고정돼 있지 않다.</p></body>
</html>
"""

PLAIN_CSS = "p { font-size: 1em; line-height: 1.6; }\n"

BROKEN_CSS = "p { font-family: Batang; @@@ } } {{ font-size: 12px\n"

FONT_CSS = """@font-face {
  font-family: "Bundled";
  src: url("bundled.ttf");
}
p { font-family: "Bundled", serif; font-size: 12px; }
"""


ENC_HEAD = ('<?xml version="1.0" encoding="UTF-8"?>\n'
            '<encryption xmlns="urn:oasis:names:tc:opendocument:xmlns:container"'
            ' xmlns:enc="http://www.w3.org/2001/04/xmlenc#">\n')

IDPF = "http://www.idpf.org/2008/embedding"
ADOBE_FONTS = "http://ns.adobe.com/pdf/enc#RC"
AES = "http://www.w3.org/2001/04/xmlenc#aes128-cbc"


def encrypted_data(uri: str, algorithm: str, prefix: str = "enc:") -> str:
    return (f'  <{prefix}EncryptedData>\n'
            f'    <{prefix}EncryptionMethod Algorithm="{algorithm}"/>\n'
            f'    <{prefix}CipherData><{prefix}CipherReference URI="{uri}"/></{prefix}CipherData>\n'
            f'  </{prefix}EncryptedData>\n')


def encryption_xml(*entries: tuple[str, str]) -> str:
    """An encryption.xml listing (uri, algorithm) pairs."""
    return ENC_HEAD + "".join(encrypted_data(u, a) for u, a in entries) + "</encryption>\n"


# A book that really is encrypted: the chapter itself is under AES.
ADOBE_DRM = encryption_xml(("OEBPS/ch1.xhtml", AES))


def obfuscated(font: bytes, identifier: str = "urn:uuid:fixture") -> bytes:
    """IDPF font obfuscation as Sigil writes it: the first 1040 bytes XOR-ed
    with the SHA-1 of the identifier, the 20-byte key repeating."""
    import hashlib
    key = hashlib.sha1("".join(identifier.split()).encode("utf-8")).digest()
    return bytes(b ^ key[i % 20] for i, b in enumerate(font[:1040])) + font[1040:]


def build(path: str, files: dict, *, encryption: "bool | str | bytes" = False,
          rights: bool = False, mimetype: bool = True,
          opf_items: str | None = None) -> str:
    """Write an EPUB whose content files are given as {zip name: text or bytes}."""
    items = opf_items if opf_items is not None else (
        '    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>\n'
        '    <item id="css" href="style.css" media-type="text/css"/>'
    )
    entries: list[tuple[str, bytes, int]] = []
    if mimetype:
        entries.append(("mimetype", b"application/epub+zip", zipfile.ZIP_STORED))
    entries.append(("META-INF/container.xml", CONTAINER.format(opf="OEBPS/content.opf").encode("utf-8"),
                    zipfile.ZIP_DEFLATED))
    if encryption:
        # True is a book that really is encrypted; text or bytes are used as given.
        blob = ADOBE_DRM if encryption is True else (
            encryption.encode("utf-8") if isinstance(encryption, str) else encryption)
        entries.append(("META-INF/encryption.xml", blob, zipfile.ZIP_DEFLATED))
    if rights:
        entries.append(("META-INF/rights.xml", b"<rights/>", zipfile.ZIP_DEFLATED))
    entries.append(("OEBPS/content.opf", OPF.format(items=items).encode("utf-8"), zipfile.ZIP_DEFLATED))
    for name, data in files.items():
        blob = data.encode("utf-8") if isinstance(data, str) else data
        entries.append((name, blob, zipfile.ZIP_DEFLATED))
    with zipfile.ZipFile(path, "w") as zf:
        for name, blob, method in entries:
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.compress_type = method
            zf.writestr(info, blob)
    return path


def standard(path: str) -> str:
    """A book with a fixed font, absolute size, absolute line height, an
    absolute margin on p, a table that must keep its margin, inline styles and
    a text node that merely mentions style="...".
    """
    return build(path, {"OEBPS/style.css": CSS, "OEBPS/ch1.xhtml": XHTML})


def already_unlocked(path: str) -> str:
    return build(path, {"OEBPS/style.css": PLAIN_CSS, "OEBPS/ch1.xhtml": PLAIN_XHTML})


def with_broken_css(path: str) -> str:
    return build(path, {"OEBPS/style.css": CSS, "OEBPS/bad.css": BROKEN_CSS,
                        "OEBPS/ch1.xhtml": XHTML})


def with_obfuscated_font(path: str, algorithm: str = IDPF) -> str:
    """A book the way Sigil or InDesign leaves it after embedding a font: the
    font is obfuscated and encryption.xml lists it. Nothing is encrypted."""
    items = ('    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>\n'
             '    <item id="css" href="style.css" media-type="text/css"/>\n'
             '    <item id="font" href="bundled.ttf" media-type="font/ttf"/>')
    font = obfuscated(b"\x00\x01\x00\x00" + b"font bytes " * 200)
    return build(path, {"OEBPS/style.css": FONT_CSS, "OEBPS/ch1.xhtml": XHTML,
                        "OEBPS/bundled.ttf": font},
                 encryption=encryption_xml(("OEBPS/bundled.ttf", algorithm)), opf_items=items)


def with_font(path: str) -> str:
    items = ('    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>\n'
             '    <item id="css" href="style.css" media-type="text/css"/>\n'
             '    <item id="font" href="bundled.ttf" media-type="font/ttf"/>')
    return build(path, {"OEBPS/style.css": FONT_CSS, "OEBPS/ch1.xhtml": XHTML,
                        "OEBPS/bundled.ttf": b"\x00\x01\x00\x00fontbytes" * 16},
                 opf_items=items)


TEXT_RE = re.compile(r"<[^>]*>", re.S)
SCRIPT_RE = re.compile(r"<(style|script)\b[^<>]*>.*?</\s*\1\s*>", re.I | re.S)


def visible_text(xhtml: str) -> str:
    """Everything outside tags, with style and script contents removed. This is
    what must be identical before and after."""
    return TEXT_RE.sub("", SCRIPT_RE.sub("", xhtml))


def book_text(path: str) -> dict:
    """{zip name: visible text} for every XHTML file in the book.

    Decoded with the tool's own decoder so a EUC-KR or UTF-16 book is compared
    as text rather than as bytes -- otherwise a codec change would slip past.
    """
    import epub_io

    out = {}
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if name.lower().endswith((".xhtml", ".html", ".htm", ".xht")):
                out[name] = visible_text(epub_io.decode(zf.read(name))[0])
    return out
