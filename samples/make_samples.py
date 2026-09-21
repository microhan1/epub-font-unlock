"""Build the sample books in this folder.

    python samples/make_samples.py

The text is written for this repository and carries no rights of its own, so
the samples can be committed and shared. Two books are produced: an EPUB 2 with
an NCX and a stylesheet that pins everything down, and an EPUB 3 with a nav
document, a @font-face, inline styles and a font shorthand.
"""
from __future__ import annotations

import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))

CHAPTERS = [
    ("1장 고정된 것들", [
        "책을 만드는 사람은 글자 하나하나의 모양까지 정해 두고 싶어 한다.",
        "그 마음은 종이 위에서는 옳다. 종이는 크기가 변하지 않기 때문이다.",
        "그러나 화면은 종이가 아니다. 읽는 사람의 눈도 저마다 다르다.",
    ]),
    ("2장 풀어 주는 일", [
        "글꼴을 풀어 준다는 것은 책을 망가뜨리는 일이 아니다.",
        "제목이 본문보다 크다는 약속은 그대로 두고, 그 크기가 몇 밀리미터인지만 지운다.",
        "그러면 리더기는 비로소 제 몫을 한다.",
    ]),
]

CSS2 = """@charset "utf-8";

body {
  font-family: "Nanum Myeongjo", serif;
  font-size: 11pt;
  line-height: 18px;
  margin: 0 12px;
}

p {
  font-family: "Nanum Myeongjo", serif;
  font-size: 11pt;
  line-height: 1.7;
  margin: 0 0 8px 0;
  text-indent: 1em;
}

h1 {
  font-family: "Nanum Gothic", sans-serif;
  font-size: 1.6em;     /* relative: the gap between heading and body must survive */
  line-height: 130%;
  margin: 1.2em 0 0.6em 0;
}

blockquote { font: italic bold 10pt/14px "Batang", serif; }
"""

CSS3 = """@charset "utf-8";

@font-face {
  font-family: "Bundled Serif";
  src: url("../fonts/bundled.ttf");
}

body { font-family: "Bundled Serif", serif; font-size: 12px; }
p    { line-height: 20px; margin: 0 0 10px 0; }
h1   { font-size: 180%; line-height: 1.3; }
table td { margin: 4px; }          /* not prose: margin cleanup must leave it */
p.note   { font-size: 0.9em; }     /* relative: must survive */
"""

XHTML2 = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml">
<head>
  <title>{title}</title>
  <link rel="stylesheet" type="text/css" href="style.css"/>
</head>
<body>
  <h1>{title}</h1>
{body}
</body>
</html>
"""

XHTML3 = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head>
  <title>{title}</title>
  <link rel="stylesheet" type="text/css" href="../css/style.css"/>
  <style type="text/css">
  p.lead {{ font-family: "Bundled Serif"; font-size: 14px; line-height: 22px; }}
  </style>
</head>
<body>
  <h1 style="font-family: 'Nanum Gothic'; font-size: 22px; color: #333">{title}</h1>
{body}
</body>
</html>
"""


def _paras(lines, style: str = "") -> str:
    attr = f' style="{style}"' if style else ""
    return "\n".join(f"  <p{attr}>{line}</p>" for line in lines)


def _write(path: str, entries: list[tuple[str, bytes, int]]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data, method in entries:
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.compress_type = method
            zf.writestr(info, data)
    print("wrote", path)


def build_epub2(path: str) -> None:
    ch = []
    for i, (title, lines) in enumerate(CHAPTERS, 1):
        body = _paras(lines, "font-size: 11pt; line-height: 18px" if i == 1 else "")
        ch.append((f"OEBPS/ch{i}.xhtml", XHTML2.format(title=title, body=body).encode("utf-8")))
    manifest = "\n".join(
        f'    <item id="ch{i}" href="ch{i}.xhtml" media-type="application/xhtml+xml"/>'
        for i in range(1, len(CHAPTERS) + 1))
    spine = "\n".join(f'    <itemref idref="ch{i}"/>' for i in range(1, len(CHAPTERS) + 1))
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>글꼴이 고정된 책</dc:title>
    <dc:language>ko</dc:language>
    <dc:identifier id="bookid">urn:uuid:1f3b6c42-9a5e-4c81-8b70-2d6e0a4f7c15</dc:identifier>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
    <item id="css" href="style.css" media-type="text/css"/>
{manifest}
  </manifest>
  <spine toc="ncx">
{spine}
  </spine>
</package>
"""
    nav = "\n".join(
        f'    <navPoint id="n{i}" playOrder="{i}"><navLabel><text>{title}</text></navLabel>'
        f'<content src="ch{i}.xhtml"/></navPoint>'
        for i, (title, _) in enumerate(CHAPTERS, 1))
    ncx = f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="urn:uuid:1f3b6c42-9a5e-4c81-8b70-2d6e0a4f7c15"/></head>
  <docTitle><text>글꼴이 고정된 책</text></docTitle>
  <navMap>
{nav}
  </navMap>
</ncx>
"""
    entries = [("mimetype", b"application/epub+zip", zipfile.ZIP_STORED),
               ("META-INF/container.xml", CONTAINER.format(opf="OEBPS/content.opf").encode("utf-8"),
                zipfile.ZIP_DEFLATED),
               ("OEBPS/content.opf", opf.encode("utf-8"), zipfile.ZIP_DEFLATED),
               ("OEBPS/toc.ncx", ncx.encode("utf-8"), zipfile.ZIP_DEFLATED),
               ("OEBPS/style.css", CSS2.encode("utf-8"), zipfile.ZIP_DEFLATED)]
    entries += [(name, data, zipfile.ZIP_DEFLATED) for name, data in ch]
    _write(path, entries)


def build_epub3(path: str) -> None:
    ch = []
    for i, (title, lines) in enumerate(CHAPTERS, 1):
        body = _paras(lines, "font-family: 'Bundled Serif'; font-size: 13px" if i == 2 else "")
        ch.append((f"EPUB/text/ch{i}.xhtml", XHTML3.format(title=title, body=body).encode("utf-8")))
    manifest = "\n".join(
        f'    <item id="ch{i}" href="text/ch{i}.xhtml" media-type="application/xhtml+xml"/>'
        for i in range(1, len(CHAPTERS) + 1))
    spine = "\n".join(f'    <itemref idref="ch{i}"/>' for i in range(1, len(CHAPTERS) + 1))
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>내장 폰트가 있는 책</dc:title>
    <dc:language>ko</dc:language>
    <dc:identifier id="bookid">urn:uuid:7c92d418-5b3a-4e6f-9d21-8a0c4f2b6e37</dc:identifier>
    <meta property="dcterms:modified">2026-01-01T00:00:00Z</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="css" href="css/style.css" media-type="text/css"/>
    <item id="font" href="fonts/bundled.ttf" media-type="font/ttf"/>
{manifest}
  </manifest>
  <spine>
{spine}
  </spine>
</package>
"""
    nav_items = "\n".join(
        f'      <li><a href="text/ch{i}.xhtml">{title}</a></li>'
        for i, (title, _) in enumerate(CHAPTERS, 1))
    nav = f"""<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title>목차</title></head>
<body>
  <nav epub:type="toc"><ol>
{nav_items}
  </ol></nav>
</body>
</html>
"""
    entries = [("mimetype", b"application/epub+zip", zipfile.ZIP_STORED),
               ("META-INF/container.xml", CONTAINER.format(opf="EPUB/content.opf").encode("utf-8"),
                zipfile.ZIP_DEFLATED),
               ("EPUB/content.opf", opf.encode("utf-8"), zipfile.ZIP_DEFLATED),
               ("EPUB/nav.xhtml", nav.encode("utf-8"), zipfile.ZIP_DEFLATED),
               ("EPUB/css/style.css", CSS3.encode("utf-8"), zipfile.ZIP_DEFLATED),
               # Not a real font: just enough bytes to stand in for one.
               ("EPUB/fonts/bundled.ttf", b"\x00\x01\x00\x00" + b"sample font" * 64,
                zipfile.ZIP_DEFLATED)]
    entries += [(name, data, zipfile.ZIP_DEFLATED) for name, data in ch]
    _write(path, entries)


CONTAINER = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="{opf}" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""


def main() -> int:
    build_epub2(os.path.join(HERE, "sample_epub2.epub"))
    build_epub3(os.path.join(HERE, "sample_epub3.epub"))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
