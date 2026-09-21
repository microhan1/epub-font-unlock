"""CSS and inline-style processing for epub-font-unlock.

Only two kinds of bytes are ever rewritten: declaration blocks in stylesheets,
and the value of a ``style`` attribute inside a tag. Text nodes are never in
range, so the book reads exactly as it did before.

Declarations are removed with tinycss2, one declaration at a time, never with a
regular expression over the CSS. Whitespace and comments are parsed along with
the rest and serialized back, so a file we do not change is returned as its own
original bytes and a file we do change keeps its shape. Before any rewrite the
parse has to pass _safe_to_rewrite: no error nodes anywhere, and serializing is
stable. Anything we did not fully understand is left exactly as it was.
"""
from __future__ import annotations

import dataclasses
import posixpath
import re
from urllib.parse import unquote

import tinycss2
from tinycss2 import serialize

# Re-exported so gui.py and main.py talk to one core module, as the series does.
from epub_io import (  # noqa: F401
    CSS_EXTS, FONT_EXTS, XHTML_EXTS, BrokenArchive, Cancelled, DrmProtected,
    Entry, EpubError, collect_epubs, decode, opf_path, output_path_for,
    read_epub, write_epub,
)

ABSOLUTE_UNITS = frozenset({"px", "pt", "pc", "cm", "mm", "in", "q"})

GENERIC_FAMILIES = frozenset({
    "serif", "sans-serif", "monospace", "cursive", "fantasy", "system-ui",
    "ui-serif", "ui-sans-serif", "ui-monospace", "ui-rounded", "math", "emoji",
    "fangsong", "-apple-system", "blinkmacsystemfont",
})
CSS_WIDE = frozenset({"inherit", "initial", "unset", "revert", "revert-layer", "default"})

SIZE_KEYWORDS = frozenset({
    "xx-small", "x-small", "small", "medium", "large", "x-large", "xx-large",
    "xxx-large", "larger", "smaller",
})
FONT_STYLE_KW = frozenset({"italic", "oblique"})
FONT_VARIANT_KW = frozenset({"small-caps"})
FONT_WEIGHT_KW = frozenset({"bold", "bolder", "lighter"})
FONT_STRETCH_KW = frozenset({
    "ultra-condensed", "extra-condensed", "condensed", "semi-condensed",
    "semi-expanded", "expanded", "extra-expanded", "ultra-expanded",
})
SYSTEM_FONT_KW = frozenset({
    "caption", "icon", "menu", "message-box", "small-caption", "status-bar",
})

MARGIN_PROPS = frozenset({
    "margin", "margin-top", "margin-right", "margin-bottom", "margin-left",
    "padding", "padding-top", "padding-right", "padding-bottom", "padding-left",
})
# The PRD scopes margin cleanup to p and div so the effect stays predictable:
# tables, figures and covers keep the spacing their maker chose.
PROSE_ELEMENTS = frozenset({"p", "div"})

NESTED_AT_RULES = frozenset({"media", "supports", "layer", "container", "-moz-document"})

TAG_RE = re.compile(r"<[a-zA-Z/!?][^<>]*>", re.S)
TAG_NAME_RE = re.compile(r"<\s*([a-zA-Z][\w:.-]*)")
# Attributes are walked in order from the end of the tag name rather than
# searched for, so a value that happens to contain ` style="..."` -- an alt or
# a title quoting some markup -- is stepped over instead of rewritten.
ATTR_RE = re.compile(
    r'(\s+)([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s"\'>]+)))?', re.S)
# script is in here because its body is code, not markup: a JavaScript string
# holding '<p style="...">' must not be mistaken for a tag and rewritten.
RAW_EL_RE = re.compile(r"<(style|script)\b[^<>]*>(.*?)</\s*\1\s*>", re.I | re.S)
COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
CDATA_RE = re.compile(r"\A(\s*<!\[CDATA\[)(.*)(\]\]>\s*)\Z", re.S)
CDO_WRAP_RE = re.compile(r"\A(\s*<!--)(.*)(-->\s*)\Z", re.S)
CDO_CDC_RE = re.compile(r"<!--|-->")


class CssParseError(Exception):
    """The stylesheet cannot be rewritten safely; leave it exactly as it is."""


@dataclasses.dataclass
class Options:
    font: bool = True
    size: bool = True
    line: bool = True
    margin: bool = False
    remove_font_files: bool = False

    def validated(self) -> "Options":
        """settings.json sits beside the exe where anyone can edit it, so every
        flag is coerced rather than trusted."""
        return Options(
            font=bool(self.font), size=bool(self.size), line=bool(self.line),
            margin=bool(self.margin), remove_font_files=bool(self.remove_font_files),
        )

    def any_on(self) -> bool:
        return any((self.font, self.size, self.line, self.margin, self.remove_font_files))

    @classmethod
    def all_on(cls) -> "Options":
        """Used by the analysis pass, which counts every category regardless of
        what the user has ticked."""
        return cls(font=True, size=True, line=True, margin=True, remove_font_files=True)


@dataclasses.dataclass
class Counts:
    fonts: int = 0          # font-family declarations that would go
    sizes: int = 0          # absolute font-size declarations
    lines: int = 0          # absolute line-height declarations
    margins: int = 0        # absolute margin/padding declarations on p or div
    inline: int = 0         # style="..." attributes touched
    font_faces: int = 0     # @font-face rules
    font_files: int = 0     # embedded font files
    families: set = dataclasses.field(default_factory=set)

    @property
    def family_count(self) -> int:
        return len(self.families)

    def total_style(self) -> int:
        return self.fonts + self.sizes + self.lines + self.margins

    def any_change(self) -> int:
        return self.total_style() + self.font_faces + self.font_files


# ---------------------------------------------------------------- value tests

def _significant(values) -> list:
    return [v for v in values if v.type not in ("whitespace", "comment")]


def _has_absolute(values, ignore_zero: bool) -> bool:
    for v in values:
        if v.type == "dimension" and v.lower_unit in ABSOLUTE_UNITS:
            if ignore_zero and v.value == 0:
                continue
            return True
        if v.type == "function" and _has_absolute(v.arguments, ignore_zero):
            return True
    return False


def _is_size_value(token) -> bool:
    if token.type in ("dimension", "percentage"):
        return True
    if token.type == "number" and token.value == 0:
        return True
    if token.type == "ident" and token.lower_value in SIZE_KEYWORDS:
        return True
    return token.type == "function" and token.lower_name in ("calc", "clamp", "min", "max")


def _family_names(values) -> list[str]:
    """Family names from a font-family value. Unquoted names arrive as several
    ident tokens (Nanum Myeongjo), so each comma-separated part is joined back
    into one name before generic families are dropped."""
    names = []
    part: list[str] = []
    for v in _significant(values) + [None]:
        if v is None or (v.type == "literal" and v.value == ","):
            if part:
                name = " ".join(part).strip()
                low = name.lower()
                # Generic families, CSS-wide keywords and the system-font
                # keywords of the font shorthand are not fonts anybody chose.
                if name and low not in GENERIC_FAMILIES and low not in CSS_WIDE \
                        and low not in SYSTEM_FONT_KW:
                    names.append(name)
            part = []
            continue
        if v.type in ("string", "ident"):
            part.append(v.value)
    return names


def _subject_is_prose(prelude) -> bool:
    """True when every selector in the group targets a p or a div.

    Every, not any: a rule like p, table { margin: 12px } must leave the
    table's spacing alone.
    """
    groups: list[list] = [[]]
    for v in prelude:
        if v.type == "literal" and v.value == ",":
            groups.append([])
        else:
            groups[-1].append(v)
    groups = [g for g in groups if _significant(g)]
    if not groups:
        return False
    for group in groups:
        compound: list = []
        for v in reversed(group):
            if v.type == "whitespace" or (v.type == "literal" and v.value in (">", "+", "~")):
                if compound:
                    break
                continue
            compound.insert(0, v)
        head = compound[0] if compound else None
        if not (head is not None and head.type == "ident" and head.lower_value in PROSE_ELEMENTS):
            return False
    return True


# ------------------------------------------------------------- declarations

def _decl_nodes(text: str) -> list:
    return tinycss2.parse_declaration_list(text, skip_comments=False, skip_whitespace=False)


def _stylesheet_nodes(text: str) -> list:
    return tinycss2.parse_stylesheet(text, skip_comments=False, skip_whitespace=False)


def _has_error(nodes) -> bool:
    """Any parse error anywhere in the tree, however deeply nested."""
    for node in nodes:
        if node.type == "error":
            return True
        for attr in ("prelude", "content", "value", "arguments"):
            child = getattr(node, attr, None)
            if isinstance(child, list) and _has_error(child):
                return True
    return False


def _safe_to_rewrite(nodes, reparse) -> bool:
    """Refuse to rewrite anything we did not fully understand.

    A byte-exact round trip is too strict to use as the test: serialize()
    terminates the last declaration with a semicolon the source may have left
    off, and it normalizes string quotes ('X' becomes "X"). What has to hold is
    that the parse found no errors and that serializing is stable -- then the
    output is a faithful rendering of everything the parser saw, and removing
    declarations from it cannot invent anything.
    """
    if _has_error(nodes):
        return False
    once = serialize(nodes)
    try:
        return once == serialize(reparse(once))
    except Exception:
        return False


def _restore_crlf(source: str, out: str) -> str:
    """Put Windows line endings back.

    The CSS tokenizer folds CRLF to LF before it sees anything, so serializing
    would quietly rewrite every line ending in the file. Only a file that used
    CRLF throughout is converted back; a mixed file is left as the parser
    normalized it rather than guessed at.
    """
    if "\r\n" in source and source.count("\r\n") == source.count("\n"):
        return out.replace("\n", "\r\n")
    return out


def _escape_for_attr(value: str, quote: str) -> str:
    """serialize() writes strings with double quotes, which would end a
    double-quoted style attribute early. A numeric reference is understood
    everywhere and leaves the CSS meaning intact."""
    return value.replace(quote, "&#34;" if quote == '"' else "&#39;")


def _split_font_shorthand(values):
    """(prefix declarations, size token, line-height token, family tokens).

    None when the value is not a shorthand we fully understand -- in that case
    the declaration is left alone rather than guessed at.
    """
    vals = _significant(values)
    if not vals:
        return None
    if len(vals) == 1 and vals[0].type == "ident" and vals[0].lower_value in SYSTEM_FONT_KW:
        return [], None, None, [vals[0]]
    prefix: list[tuple[str, str]] = []
    i = 0
    while i < len(vals):
        v = vals[i]
        prop = None
        if v.type == "ident":
            low = v.lower_value
            if low in FONT_STYLE_KW:
                prop = "font-style"
            elif low in FONT_VARIANT_KW:
                prop = "font-variant"
            elif low in FONT_WEIGHT_KW:
                prop = "font-weight"
            elif low in FONT_STRETCH_KW:
                prop = "font-stretch"
            elif low == "normal":
                prop = ""  # the initial value; nothing is lost by dropping it
        elif v.type == "number" and v.int_value is not None and 1 <= v.int_value <= 1000:
            prop = "font-weight"
        if prop is None:
            break
        if prop:
            prefix.append((prop, serialize([v])))
        i += 1
    if i >= len(vals) or not _is_size_value(vals[i]):
        return None
    size_tok = vals[i]
    i += 1
    line_tok = None
    if i < len(vals) and vals[i].type == "literal" and vals[i].value == "/":
        i += 1
        if i >= len(vals):
            return None
        line_tok = vals[i]
        i += 1
    family = vals[i:]
    if not family:
        return None
    return prefix, size_tok, line_tok, family


def _rewrite_font_shorthand(decl, opts: "Options", counts: "Counts"):
    parsed = _split_font_shorthand(decl.value)
    if parsed is None:
        return None
    prefix, size_tok, line_tok, family = parsed
    drop_family = opts.font and bool(family)
    drop_size = size_tok is not None and opts.size and _has_absolute([size_tok], ignore_zero=False)
    drop_line = line_tok is not None and opts.line and _has_absolute([line_tok], ignore_zero=False)
    if not (drop_family or drop_size or drop_line):
        return None
    if drop_family:
        counts.fonts += 1
        counts.families.update(_family_names(family))
    if drop_size:
        counts.sizes += 1
    if drop_line:
        counts.lines += 1

    parts = list(prefix)
    if size_tok is not None and not drop_size:
        parts.append(("font-size", serialize([size_tok])))
    if line_tok is not None and not drop_line:
        parts.append(("line-height", serialize([line_tok])))
    if family and not drop_family:
        parts.append(("font-family", serialize(family).strip()))
    if not parts:
        return []
    bang = " !important" if decl.important else ""
    text = "; ".join(f"{prop}: {value}{bang}" for prop, value in parts)
    return _decl_nodes(text)


def _rewrite_declaration(decl, opts: "Options", counts: "Counts", prose: bool):
    """None keeps the declaration, an empty list drops it, a list replaces it."""
    name = decl.lower_name
    if name == "font-family" and opts.font:
        counts.fonts += 1
        counts.families.update(_family_names(decl.value))
        return []
    if name == "font":
        return _rewrite_font_shorthand(decl, opts, counts)
    if name == "font-size" and opts.size and _has_absolute(decl.value, ignore_zero=False):
        counts.sizes += 1
        return []
    if name == "line-height" and opts.line and _has_absolute(decl.value, ignore_zero=False):
        counts.lines += 1
        return []
    if name in MARGIN_PROPS and opts.margin and prose and _has_absolute(decl.value, ignore_zero=True):
        counts.margins += 1
        return []
    return None


def _filter_declarations(nodes, opts: "Options", counts: "Counts", prose: bool):
    out: list = []
    changed = False
    i = 0
    while i < len(nodes):
        node = nodes[i]
        if node.type != "declaration":
            out.append(node)
            i += 1
            continue
        replacement = _rewrite_declaration(node, opts, counts, prose)
        i += 1
        if replacement is None:
            out.append(node)
            continue
        changed = True
        out.extend(replacement)
        # Swallow the whitespace that trailed the removed declaration so the
        # block closes up instead of leaving a blank line behind.
        if not replacement and i < len(nodes) and nodes[i].type == "whitespace":
            i += 1
    return out, changed


def _filter_rules(nodes, opts: "Options", counts: "Counts"):
    out: list = []
    changed = False
    i = 0
    while i < len(nodes):
        node = nodes[i]
        i += 1
        if node.type == "qualified-rule":
            prose = _subject_is_prose(node.prelude)
            content, ch = _filter_declarations(_decl_nodes(serialize(node.content)), opts, counts, prose)
            if ch:
                node.content = content
                changed = True
            out.append(node)
            continue
        if node.type == "at-rule":
            keyword = node.lower_at_keyword
            if keyword == "font-face":
                counts.font_faces += 1
                if opts.remove_font_files:
                    changed = True
                    if i < len(nodes) and nodes[i].type == "whitespace":
                        i += 1
                    continue
                out.append(node)
                continue
            if keyword in NESTED_AT_RULES and node.content is not None:
                inner = tinycss2.parse_rule_list(node.content, skip_comments=False, skip_whitespace=False)
                filtered, ch = _filter_rules(inner, opts, counts)
                if ch:
                    node.content = filtered
                    changed = True
            out.append(node)
            continue
        out.append(node)
    return out, changed


# ------------------------------------------------------------------ entries

def _unwrap(text: str) -> tuple[str, str, str]:
    """Peel a CDATA section or a legacy <!-- --> wrapper off a style block.

    tinycss2 parses those markers into CDO/CDC tokens and drops them when it
    serializes, which would silently delete them. Held aside here, they come
    back out untouched.
    """
    prefix = suffix = ""
    inner = text
    for pattern in (CDATA_RE, CDO_WRAP_RE):
        m = pattern.match(inner)
        if m:
            prefix += m.group(1)
            suffix = m.group(3) + suffix
            inner = m.group(2)
    return prefix, inner, suffix


def unlock_css(text: str, opts: "Options", counts: "Counts") -> tuple[str, bool]:
    """Rewrite a whole stylesheet. Raises CssParseError when it is not safe."""
    try:
        prefix, inner, suffix = _unwrap(text)
        if CDO_CDC_RE.search(inner):
            # A stray marker somewhere in the middle; serializing would eat it.
            raise CssParseError("stray <!-- or --> in the stylesheet")
        nodes = tinycss2.parse_stylesheet(inner, skip_comments=False, skip_whitespace=False)
        if not _safe_to_rewrite(nodes, _stylesheet_nodes):
            raise CssParseError("stylesheet did not parse cleanly")
        filtered, changed = _filter_rules(nodes, opts, counts)
        if not changed:
            return text, False
        return prefix + _restore_crlf(inner, serialize(filtered)) + suffix, True
    except CssParseError:
        raise
    except Exception as exc:  # a malformed file must never end a batch
        raise CssParseError(str(exc)) from exc


def unlock_style_attr(value: str, opts: "Options", counts: "Counts", prose: bool) -> tuple[str, bool]:
    """Rewrite one style attribute value with the same rules as a stylesheet."""
    try:
        nodes = _decl_nodes(value)
        if not _safe_to_rewrite(nodes, _decl_nodes):
            raise CssParseError("style attribute did not parse cleanly")
        filtered, changed = _filter_declarations(nodes, opts, counts, prose)
        if not changed:
            return value, False
        return _restore_crlf(value, serialize(filtered)), True
    except CssParseError:
        raise
    except Exception as exc:
        raise CssParseError(str(exc)) from exc


def _iter_attrs(tag: str, pos: int):
    """Walk a tag's attributes left to right from the end of its name. Stepping
    attribute by attribute is what keeps a quoted value from being searched."""
    while True:
        am = ATTR_RE.match(tag, pos)
        if am is None or am.end() == pos:
            return
        pos = am.end()
        yield am


def unlock_xhtml(text: str, opts: "Options", counts: "Counts") -> tuple[str, bool, list[str]]:
    """Rewrite style blocks and style attributes. Everything between tags --
    the words of the book -- is never part of a replacement span."""
    errors: list[str] = []
    repls: list[tuple[int, int, str]] = []
    raw_spans: list[tuple[int, int]] = []

    for m in RAW_EL_RE.finditer(text):
        start, end = m.span(2)
        raw_spans.append((start, end))
        if m.group(1).lower() != "style":
            continue  # a script body is code; read past it, never into it
        try:
            new_body, changed = unlock_css(m.group(2), opts, counts)
        except CssParseError as exc:
            errors.append(str(exc))
            continue
        if changed:
            repls.append((start, end, new_body))

    # Commented-out markup renders nowhere, so there is nothing to unlock in it
    # and no reason to touch it. A <!-- inside a script body is that script's
    # business, not a comment, so those are ignored here.
    for m in COMMENT_RE.finditer(text):
        if not any(s <= m.start() < e for s, e in raw_spans):
            raw_spans.append(m.span())

    for m in TAG_RE.finditer(text):
        if any(s <= m.start() < e for s, e in raw_spans):
            continue
        tag = m.group(0)
        name_m = TAG_NAME_RE.match(tag)
        if name_m is None:
            continue
        prose = name_m.group(1).lower() in PROSE_ELEMENTS
        for am in _iter_attrs(tag, name_m.end()):
            if am.group(2).lower() != "style":
                continue
            quoted = 3 if am.group(3) is not None else 4
            value = am.group(quoted)
            if value is None:
                continue  # an unquoted value; rewriting it would need re-quoting
            try:
                new_value, changed = unlock_style_attr(value, opts, counts, prose)
            except CssParseError as exc:
                errors.append(str(exc))
                continue
            if not changed:
                continue
            counts.inline += 1
            if new_value.strip():
                s, e = am.span(quoted)
                repls.append((m.start() + s, m.start() + e,
                              _escape_for_attr(new_value, '"' if quoted == 3 else "'")))
            else:  # nothing left; drop the attribute and the space before it
                s, e = am.span()
                repls.append((m.start() + s, m.start() + e, ""))

    if not repls:
        return text, False, errors
    out = text
    for start, end, new in sorted(repls, reverse=True):
        out = out[:start] + new + out[end:]
    return out, True, errors


# -------------------------------------------------------------- whole books

MANIFEST_ITEM_RE = re.compile(
    r"[ \t]*<item\b[^<>]*?(?:/>|>\s*</\s*item\s*>)[ \t]*\r?\n?", re.I | re.S)
HREF_RE = re.compile(r'href\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', re.I)


@dataclasses.dataclass
class Analysis:
    path: str
    counts: "Counts"
    css_files: int = 0
    xhtml_files: int = 0
    failed: list = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class FileResult:
    input_path: str
    output_path: str | None
    counts: "Counts"
    changed_files: int = 0
    failed: list = dataclasses.field(default_factory=list)

    @property
    def already_unlocked(self) -> bool:
        return self.output_path is None


def _is_content(entry) -> bool:
    return not entry.is_dir and entry.has_ext(CSS_EXTS + XHTML_EXTS)


def _strip_font_manifest_items(text: str, opf_dir: str, deleted: set) -> str:
    """Drop the manifest entries for fonts we removed.

    The PRD says to leave the OPF alone, and this is the one exception it has
    to make: a manifest item pointing at a file that is no longer in the
    archive is a hard epubcheck error, so the book would stop being valid.
    Only those item elements go; metadata, spine and guide are not touched.
    """
    def repl(m):
        href_m = HREF_RE.search(m.group(0))
        if not href_m:
            return m.group(0)
        href = unquote(href_m.group(1) if href_m.group(1) is not None else href_m.group(2))
        joined = posixpath.join(opf_dir, href) if opf_dir else href
        return "" if posixpath.normpath(joined) in deleted else m.group(0)

    return MANIFEST_ITEM_RE.sub(repl, text)


def _run(path: str, opts: "Options", counts: "Counts", progress=None, cancel=None):
    """Shared engine for analysis and processing.

    Returns (entries, changed_files, failed). The analysis pass calls it with
    every option on and throws the entries away.
    """
    entries = read_epub(path)
    failed: list = []
    deleted_fonts: set = set()

    kept: list = []
    for entry in entries:
        if not entry.is_dir and entry.has_ext(FONT_EXTS):
            counts.font_files += 1
            if opts.remove_font_files:
                deleted_fonts.add(entry.name)
                continue
        kept.append(entry)

    targets = [e for e in kept if _is_content(e)]
    changed_files = 0
    for index, entry in enumerate(targets, 1):
        if cancel is not None and cancel.is_set():
            raise Cancelled(path)
        text, codec = decode(entry.data)
        try:
            if entry.has_ext(CSS_EXTS):
                new_text, changed = unlock_css(text, opts, counts)
                errors: list = []
            else:
                new_text, changed, errors = unlock_xhtml(text, opts, counts)
        except CssParseError as exc:
            failed.append((entry.name, str(exc)))
            if progress is not None:
                progress(index, len(targets), entry.name)
            continue
        for err in errors:
            failed.append((entry.name, err))
        if changed:
            entry.data = new_text.encode(codec, "strict")
            changed_files += 1
        if progress is not None:
            progress(index, len(targets), entry.name)

    if deleted_fonts:
        opf = opf_path(kept)
        for entry in kept:
            if opf is not None and entry.name == opf:
                text, codec = decode(entry.data)
                new_text = _strip_font_manifest_items(text, posixpath.dirname(opf), deleted_fonts)
                if new_text != text:
                    entry.data = new_text.encode(codec, "strict")
                changed_files += 1
                break
        else:
            changed_files += 1  # fonts left the archive even if no OPF was found
    return kept, changed_files, failed


def analyze_epub(path: str) -> "Analysis":
    """What is pinned down in this book, before anything changes. Writes
    nothing: the user sees the numbers first and then decides."""
    counts = Counts()
    entries = read_epub(path)
    css = sum(1 for e in entries if _is_content(e) and e.has_ext(CSS_EXTS))
    xhtml = sum(1 for e in entries if _is_content(e) and e.has_ext(XHTML_EXTS))
    _, _, failed = _run(path, Options.all_on(), counts)
    return Analysis(path=path, counts=counts, css_files=css, xhtml_files=xhtml, failed=failed)


def process_epub(path: str, opts: "Options", output_path: str | None = None,
                 progress=None, cancel=None) -> "FileResult":
    """Write <name>_unlocked.epub beside the original, or nothing at all when
    the book has no fixed styling left to remove."""
    counts = Counts()
    opts = opts.validated()
    entries, changed_files, failed = _run(path, opts, counts, progress=progress, cancel=cancel)
    if cancel is not None and cancel.is_set():
        raise Cancelled(path)
    if not changed_files:
        return FileResult(input_path=path, output_path=None, counts=counts, failed=failed)
    target = output_path or output_path_for(path)
    write_epub(target, entries)
    return FileResult(input_path=path, output_path=target, counts=counts,
                      changed_files=changed_files, failed=failed)
