"""EPUB container I/O for epub-font-unlock.

An EPUB is a zip with two rules a reader will not forgive: ``mimetype`` must be
the first entry and must be stored uncompressed. Everything else keeps the
order and the compression method it arrived with, so a rewritten book differs
from the original only in the bytes we deliberately changed.

Nothing here writes over the input. Output goes to a temporary file next to the
target and is renamed into place only once the whole archive is written, so a
cancel or a crash can never leave a half-finished epub behind.
"""
from __future__ import annotations

import dataclasses
import os
import re
import zipfile
from xml.etree import ElementTree

MIMETYPE_NAME = "mimetype"
MIMETYPE_VALUE = b"application/epub+zip"
ENCRYPTION_PATH = "META-INF/encryption.xml"
CONTAINER_PATH = "META-INF/container.xml"

CSS_EXTS = (".css",)
XHTML_EXTS = (".xhtml", ".html", ".htm", ".xht")
FONT_EXTS = (".ttf", ".otf", ".woff", ".woff2", ".ttc", ".eot")

OUTPUT_SUFFIX = "_unlocked"


class EpubError(Exception):
    """Base class for anything that stops a single book from being processed."""


class BrokenArchive(EpubError):
    """Not a readable zip, or the central directory is damaged."""


class DrmProtected(EpubError):
    """META-INF/encryption.xml is present; the content may be encrypted."""


class Cancelled(EpubError):
    """The user stopped the run before the output was written."""


@dataclasses.dataclass
class Entry:
    """One zip member, held in memory with the metadata needed to write it back."""

    name: str
    data: bytes
    compress_type: int = zipfile.ZIP_DEFLATED
    date_time: tuple = (1980, 1, 1, 0, 0, 0)
    external_attr: int = 0
    create_system: int = 0
    internal_attr: int = 0
    comment: bytes = b""

    @property
    def is_dir(self) -> bool:
        return self.name.endswith("/")

    def has_ext(self, exts: tuple[str, ...]) -> bool:
        return self.name.lower().endswith(exts)


def _member_name(info: zipfile.ZipInfo) -> str:
    """The member's real name.

    A zip says whether its names are UTF-8 (general-purpose flag bit 11). When
    it does not, Python reads the raw bytes as cp437. Some tools write UTF-8
    names without setting the flag, so a Korean file name arrives as mojibake --
    and written back out it would be encoded a second time, leaving a name that
    no longer matches the href in the package document. The raw bytes are
    recovered here and read as UTF-8, which is what the EPUB spec requires.

    Anything else (cp949, Shift-JIS ...) cannot be told apart reliably and
    cannot be written back byte for byte through zipfile, so the book is
    refused rather than returned with garbled names.
    """
    name = info.filename
    if info.flag_bits & 0x800 or name.isascii():
        return name
    try:
        return name.encode("cp437").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError) as exc:
        raise BrokenArchive(f"member name is not UTF-8: {name!r}") from exc


def read_epub(path: str) -> list[Entry]:
    """Load every member into memory, so the output is built from a complete
    picture rather than streamed. That costs about the size of the book (a 78 MB
    book of page images peaks near 95 MB); the text files are the only part that
    is ever edited."""
    try:
        with zipfile.ZipFile(path, "r") as zf:
            names = zf.namelist()
            if ENCRYPTION_PATH in names:
                raise DrmProtected(path)
            entries = []
            for info in zf.infolist():
                data = b"" if info.is_dir() else zf.read(info)
                entries.append(
                    Entry(
                        name=_member_name(info),
                        data=data,
                        compress_type=info.compress_type,
                        date_time=info.date_time,
                        external_attr=info.external_attr,
                        create_system=info.create_system,
                        internal_attr=info.internal_attr,
                        comment=info.comment,
                    )
                )
    except DrmProtected:
        raise
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError) as exc:
        raise BrokenArchive(str(exc)) from exc
    if not entries:
        raise BrokenArchive(path)
    return entries


def write_epub(path: str, entries: list[Entry]) -> None:
    """Write the archive, mimetype first and uncompressed.

    The members are written to ``<path>.tmp`` and renamed at the end; readers
    must never see a partially written book.
    """
    ordered = sorted(entries, key=lambda e: e.name != MIMETYPE_NAME)
    tmp = path + ".tmp"
    try:
        with zipfile.ZipFile(tmp, "w") as zf:
            for entry in ordered:
                if entry.name == MIMETYPE_NAME:
                    # A fresh ZipInfo so no extra field tags along; epubcheck
                    # requires the first entry to be stored with none.
                    info = zipfile.ZipInfo(MIMETYPE_NAME, entry.date_time)
                    info.compress_type = zipfile.ZIP_STORED
                    info.external_attr = entry.external_attr
                    info.create_system = entry.create_system
                    zf.writestr(info, entry.data)
                    continue
                info = zipfile.ZipInfo(entry.name, entry.date_time)
                info.compress_type = entry.compress_type
                info.external_attr = entry.external_attr
                info.create_system = entry.create_system
                info.internal_attr = entry.internal_attr
                info.comment = entry.comment
                zf.writestr(info, entry.data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def collect_epubs(paths) -> list[str]:
    """Expand folders recursively, keep the given order, drop duplicates.
    Upper-case .EPUB counts too."""
    found: list[str] = []
    seen: set[str] = set()

    def add(p: str) -> None:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            found.append(p)

    for raw in paths:
        p = os.path.abspath(raw)
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs.sort()
                for name in sorted(files):
                    if name.lower().endswith(".epub"):
                        add(os.path.join(root, name))
        elif os.path.isfile(p) and p.lower().endswith(".epub"):
            add(p)
    return found


def output_path_for(input_path: str) -> str:
    """<name>_unlocked.epub beside the original, numbered if taken. A folder
    sitting on the name counts as taken; the original is never overwritten."""
    base, ext = os.path.splitext(os.path.abspath(input_path))
    candidate = f"{base}{OUTPUT_SUFFIX}{ext or '.epub'}"
    n = 2
    while os.path.exists(candidate):
        candidate = f"{base}{OUTPUT_SUFFIX}({n}){ext or '.epub'}"
        n += 1
    return candidate


def decode(data: bytes) -> tuple[str, str]:
    """Text plus the codec to write it back with.

    latin-1 is the fallback because it round-trips every byte: a file we cannot
    read as UTF-8 still comes out byte-identical wherever we did not edit it.
    """
    # A UTF-16 BOM has to be caught before anything else: the encoding
    # declaration inside the file is itself UTF-16, so the search below cannot
    # see it, and latin-1 would otherwise swallow the file as nulls and text.
    # The endian-specific codecs leave the BOM as a ﻿ character, so
    # re-encoding puts the same two bytes back.
    if data[:2] == b"\xff\xfe" and data[2:4] != b"\x00\x00":
        return data.decode("utf-16-le"), "utf-16-le"
    if data[:2] == b"\xfe\xff":
        return data.decode("utf-16-be"), "utf-16-be"
    for codec in _declared_codecs(data):
        try:
            return data.decode(codec), codec
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1"), "latin-1"


def _declared_codecs(data: bytes) -> list[str]:
    codecs = []
    head = data[:200]
    m = re.search(rb'encoding\s*=\s*["\']([\w.-]+)["\']', head)
    if m:
        codecs.append(m.group(1).decode("ascii", "replace"))
    m = re.search(rb'@charset\s+"([\w.-]+)"', head)
    if m:
        codecs.append(m.group(1).decode("ascii", "replace"))
    # A declaration we could read as ASCII cannot belong to a UTF-16 or UTF-32
    # file -- those have no ASCII bytes to read it from, and their BOM was
    # handled before this point. So such a declaration is a lie, and a common
    # one: .NET's XmlWriter over a StringWriter writes encoding="utf-16" above
    # UTF-8 content. Believing it turns the page to garbage.
    codecs = [c for c in codecs if not c.lower().replace("_", "-").startswith(("utf-16", "utf-32", "ucs"))]
    codecs.append("utf-8")
    return codecs


def opf_path(entries: list[Entry]) -> str | None:
    """The package document path from META-INF/container.xml."""
    for entry in entries:
        if entry.name == CONTAINER_PATH:
            try:
                root = ElementTree.fromstring(entry.data)
            except ElementTree.ParseError:
                return None
            for el in root.iter():
                if el.tag.rsplit("}", 1)[-1] == "rootfile":
                    full = el.get("full-path")
                    if full:
                        return full.replace("\\", "/")
            return None
    return None
