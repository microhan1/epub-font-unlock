"""Randomly built stylesheets and pages, checked against the invariants.

A fixed seed, so a failure is reproducible and the suite stays deterministic.
The point is the combinations nobody thinks to write down: a comment between a
removed declaration and the next one, a font shorthand inside a nested @media,
a style attribute next to an alt that quotes markup.

What has to hold, whatever came in:
  the visible text is unchanged; script bodies are unchanged; the output
  re-parses with no errors; running again changes nothing; everything an
  option asked to remove is gone; nothing else is.
"""
from __future__ import annotations

import os
import random
import re
import sys
import unittest

import fixtures  # noqa: E402,F401  (inserts the repo root on sys.path)
import tinycss2  # noqa: E402

import unlock  # noqa: E402
from unlock import Counts, Options  # noqa: E402

# A deeper sweep on demand, without editing the file:
#   EFU_FUZZ_ROUNDS=20000 EFU_FUZZ_SEED=7 python -m unittest test_fuzz
ROUNDS = int(os.environ.get("EFU_FUZZ_ROUNDS", "400"))
SEED = int(os.environ.get("EFU_FUZZ_SEED", "20260920"))

ABS = ["11pt", "12px", "0.5cm", "3mm", "1in", "2pc", "10Q", "12PX", "11Pt"]
REL = ["1em", "120%", "1.2rem", "larger", "smaller", "medium", "x-large", "1.5ex"]
LINES = ["1.6", "150%", "1.4em", "normal", "18px", "20pt", "0"]
FAMS = ['"Nanum Myeongjo"', "Batang, serif", "'Nanum Gothic'", "serif", "inherit", "var(--f)"]
SEL = ["p", "div", "p.first", "div > p", ".note", "h1", "body", "p, div", "p, table",
       "*", "td", "p::first-line", "#id p", "blockquote p:not(.x)"]
OTHER = ["color: #333", "text-align: justify", "text-indent: 1em", "background: url('a.png')",
         "font-weight: bold", "--custom: Batang", "font-variant: small-caps"]
WS = ["", " ", "\n", "\n  ", "\t", "\n\n"]
COMMENTS = ["", "/* c */", "/* 주석 */", "/**/"]
TEXT = ["본문입니다.", "Plain text.", "a < b & c > d", 'style="font-size: 9pt" 를 언급함', ""]

ABS_UNITS = {"px", "pt", "pc", "cm", "mm", "in", "q"}
TAG_STRIP = re.compile(r"<[^>]*>", re.S)
RAW_STRIP = re.compile(r"<(style|script)\b[^<>]*>.*?</\s*\1\s*>", re.I | re.S)
SCRIPT_RE = re.compile(r"<script\b[^<>]*>(.*?)</script>", re.S)


def _decl(rng: random.Random) -> str:
    kind = rng.randrange(8)
    bang = " !important" if rng.random() < 0.15 else ""
    if kind == 0:
        return f"font-family: {rng.choice(FAMS)}{bang}"
    if kind == 1:
        return f"font-size: {rng.choice(ABS + REL)}{bang}"
    if kind == 2:
        return f"line-height: {rng.choice(LINES)}{bang}"
    if kind == 3:
        prop = rng.choice(["margin", "padding", "margin-top", "padding-left"])
        return f"{prop}: {rng.choice(['0', '0 0 8px 0', '1em', '12px', '0 3%'])}{bang}"
    if kind == 4:
        return (f"font: {rng.choice(['', 'bold ', 'italic bold ', '700 '])}"
                f"{rng.choice(ABS + REL)}{rng.choice(['', '/1.4', '/18px', '/150%'])} "
                f"{rng.choice(['\"Batang\"', 'serif', 'Nanum Gothic, serif'])}{bang}")
    return rng.choice(OTHER) + bang


def _rule(rng: random.Random, depth: int = 0) -> str:
    roll = rng.random()
    ws = rng.choice(WS)
    if roll < 0.08 and depth < 2:
        inner = "".join(_rule(rng, depth + 1) for _ in range(rng.randrange(1, 3)))
        at = rng.choice(["@media screen", "@media (min-width: 100px)", "@supports (display:flex)"])
        return f"{at} {{{ws}{inner}{ws}}}{ws}"
    if roll < 0.12:
        return '@font-face { font-family: "Bundled"; src: url("b.ttf"); }' + ws
    if roll < 0.15:
        return "@page { margin: 2cm; font-size: 10pt; }" + ws
    if roll < 0.17:
        return "@import url('other.css');" + ws
    body = "".join(rng.choice(WS) + rng.choice(COMMENTS) + rng.choice(WS) + _decl(rng) + ";"
                   for _ in range(rng.randrange(0, 5)))
    return f"{rng.choice(SEL)}{ws}{{{body}{ws}}}{ws}"


def stylesheet(rng: random.Random) -> str:
    head = '@charset "utf-8";\n' if rng.random() < 0.2 else ""
    return head + "".join(_rule(rng) for _ in range(rng.randrange(1, 8)))


def document(rng: random.Random) -> str:
    parts = ['<?xml version="1.0" encoding="utf-8"?>',
             '<html xmlns="http://www.w3.org/1999/xhtml"><head>']
    if rng.random() < 0.4:
        body = stylesheet(rng)
        wrap = rng.random()
        if wrap < 0.25:
            body = f"<![CDATA[ {body} ]]>"
        elif wrap < 0.45:
            body = f"<!-- {body} -->"
        parts.append(f'<style type="text/css">{body}</style>')
    parts.append("</head><body>")
    for _ in range(rng.randrange(1, 8)):
        roll = rng.random()
        if roll < 0.1:
            parts.append(f'<!-- <p style="font-size: 9pt">{rng.choice(TEXT)}</p> -->')
        elif roll < 0.18:
            parts.append('<script>var s = \'<p style="font-size:9pt">x</p>\'; if (a<b) {}</script>')
        else:
            tag = rng.choice(["p", "div", "h1", "span", "blockquote", "td"])
            attrs = ""
            if rng.random() < 0.4:
                attrs += f' class="{rng.choice(["a", "note", "b c"])}"'
            if rng.random() < 0.2:
                attrs += ' title=\'see style="font-size: 9pt"\''
            if rng.random() < 0.6:
                quote = rng.choice(['"', "'"])
                inner = "; ".join(_decl(rng) for _ in range(rng.randrange(1, 4))).replace(quote, "")
                attrs += f" style={quote}{inner}{quote}"
            parts.append(f"<{tag}{attrs}>{rng.choice(TEXT)}</{tag}>")
    parts.append("</body></html>")
    return "\n".join(parts)


def visible(text: str) -> str:
    return TAG_STRIP.sub("", RAW_STRIP.sub("", text))


def has_error(nodes) -> bool:
    for node in nodes:
        if node.type == "error":
            return True
        for attr in ("prelude", "content", "value", "arguments"):
            child = getattr(node, attr, None)
            if isinstance(child, list) and has_error(child):
                return True
    return False


def _absolute(values) -> bool:
    for v in values:
        if v.type == "dimension" and v.lower_unit in ABS_UNITS:
            return True
        if v.type == "function" and _absolute(v.arguments):
            return True
    return False


def leftovers(css_text: str) -> dict:
    """Read the output back with tinycss2 directly -- not through the tool --
    and list anything that should have gone. @font-face is skipped: the
    font-family in there names the face, it does not pin one on the text."""
    found: dict = {"family": [], "size": [], "line": []}

    def walk(nodes) -> None:
        for node in nodes:
            if node.type == "qualified-rule":
                for d in tinycss2.parse_declaration_list(tinycss2.serialize(node.content)):
                    if d.type != "declaration":
                        continue
                    if d.lower_name in ("font-family", "font"):
                        found["family"].append(tinycss2.serialize([d]))
                    elif d.lower_name == "font-size" and _absolute(d.value):
                        found["size"].append(tinycss2.serialize([d]))
                    elif d.lower_name == "line-height" and _absolute(d.value):
                        found["line"].append(tinycss2.serialize([d]))
            elif (node.type == "at-rule" and node.content is not None
                  and node.lower_at_keyword in unlock.NESTED_AT_RULES):
                walk(tinycss2.parse_rule_list(node.content, skip_comments=False,
                                              skip_whitespace=False))

    walk(tinycss2.parse_stylesheet(css_text, skip_comments=False, skip_whitespace=False))
    return found


class Fuzz(unittest.TestCase):
    def options(self, rng: random.Random) -> Options:
        return Options(font=rng.random() < 0.7, size=rng.random() < 0.7,
                       line=rng.random() < 0.7, margin=rng.random() < 0.4,
                       remove_font_files=rng.random() < 0.3)

    def test_stylesheets(self) -> None:
        rng = random.Random(SEED)
        for i in range(ROUNDS):
            opts = self.options(rng)
            source = stylesheet(rng)
            with self.subTest(round=i):
                try:
                    out, _ = unlock.unlock_css(source, opts, Counts())
                except unlock.CssParseError:
                    continue  # refusing to touch it is an allowed outcome
                self.assertFalse(has_error(tinycss2.parse_stylesheet(out)), out)
                again, changed = unlock.unlock_css(out, opts, Counts())
                self.assertFalse(changed, f"not idempotent\n{source}\n->\n{out}")
                self.assertEqual(again, out)

                left = leftovers(out)
                if opts.font:
                    self.assertEqual(left["family"], [], out)
                if opts.size:
                    self.assertEqual(left["size"], [], out)
                if opts.line:
                    self.assertEqual(left["line"], [], out)
                if not opts.font and not opts.remove_font_files:
                    self.assertLessEqual(source.count("font-family"), out.count("font-family"), out)
                if not opts.remove_font_files:
                    self.assertEqual(source.count("@font-face"), out.count("@font-face"), out)

    def test_pages(self) -> None:
        rng = random.Random(SEED + 1)
        for i in range(ROUNDS):
            opts = self.options(rng)
            source = document(rng)
            with self.subTest(round=i):
                out, _, errors = unlock.unlock_xhtml(source, opts, Counts())
                self.assertEqual(errors, [], source)
                self.assertEqual(visible(source), visible(out), source)
                self.assertEqual(SCRIPT_RE.findall(source), SCRIPT_RE.findall(out), source)
                again, _, _ = unlock.unlock_xhtml(out, opts, Counts())
                self.assertEqual(again, out, f"not idempotent\n{source}\n->\n{out}")


if __name__ == "__main__":
    sys.exit(unittest.main())
