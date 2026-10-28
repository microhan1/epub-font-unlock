"""The series-wide follow-up to [Send feedback] (2026-10-27): one rule for which
page a language gets, the tooltip kept on the monitor, and the README links.

The window side (Tab, Enter, Space, the focus colour, the tooltip at the screen's
edges) is in test_gui.py with the other window tests. Nothing here opens a browser
or reaches the network.
"""
from __future__ import annotations

import inspect
import os
import re
import socket
import sys
import unittest
import urllib.parse

import fixtures  # noqa: E402,F401  (inserts the repo root on sys.path)

import gui  # noqa: E402
import i18n  # noqa: E402
import update_check as uc  # noqa: E402
from version import APP_VERSION, UPDATE_SLUG  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KO_PAGE = "https://chaekgalpi.co.kr/tools/epubfont"
EN_PAGE = "https://chaekgalpi.co.kr/en/tools/epubfont"

KOREAN = ("ko", "KO", "Ko", " ko ", "ko-KR", "ko_KR", "KO-kr", "ko-Kore-KR", "\tko\n")
NOT_KOREAN = ("en", "ja", "zh", "zh-CN", "zh_CN", "zh-TW", "fr", "de", "EN", "en-US", "xx",
              "JA", "zh-cn", "en_GB", "kok", "korean", "kor", "k", "-ko", "_ko", "x-ko")
MISSING = (None, "", " ", "   ", "\t\n", 7, 0, ["ko"], {"ko": 1}, b"ko")

UPDATE_KO = "https://chaekgalpi.co.kr/tools/epubfont?utm_source=app&utm_medium=update"
UPDATE_EN = "https://chaekgalpi.co.kr/en/tools/epubfont?utm_source=app&utm_medium=update"


def path_of(url: str) -> str:
    return urllib.parse.urlsplit(url).path


def info(url_en: str = UPDATE_EN) -> uc.UpdateInfo:
    return uc.UpdateInfo("9.9.9", APP_VERSION, UPDATE_KO, url_en)


class LanguageRule(unittest.TestCase):
    """c. The first part of the code decides: ko is the Korean page, anything else
    the English one, and no code at all is the window's default language."""

    def test_korean_codes_get_the_korean_page(self) -> None:
        for lang in KOREAN:
            with self.subTest(lang=lang):
                self.assertTrue(uc.is_korean(lang))
                self.assertEqual(path_of(uc.feedback_url(UPDATE_SLUG, APP_VERSION, lang)), "/tools/epubfont")
                self.assertEqual(uc.page_for(info(), lang), UPDATE_KO)

    def test_every_other_code_gets_the_english_page(self) -> None:
        for lang in NOT_KOREAN:
            with self.subTest(lang=lang):
                self.assertFalse(uc.is_korean(lang))
                self.assertEqual(path_of(uc.feedback_url(UPDATE_SLUG, APP_VERSION, lang)),
                                 "/en/tools/epubfont")
                self.assertEqual(uc.page_for(info(), lang), UPDATE_EN)

    def test_the_window_s_own_languages(self) -> None:
        wanted = {"ko": True, "en": False, "ja": False, "zh-CN": False}
        self.assertEqual(set(wanted), set(i18n.LANGS))
        for lang, korean in wanted.items():
            with self.subTest(lang=lang):
                self.assertIs(uc.is_korean(lang), korean)

    def test_no_language_is_the_window_s_default(self) -> None:
        self.assertEqual(i18n.DEFAULT_LANG, "en")
        self.assertEqual(uc.DEFAULT_LANG, i18n.DEFAULT_LANG)
        for lang in MISSING:
            with self.subTest(lang=lang):
                self.assertIs(uc.is_korean(lang), uc.is_korean(i18n.DEFAULT_LANG))
                self.assertEqual(path_of(uc.feedback_url(UPDATE_SLUG, APP_VERSION, lang)),
                                 "/en/tools/epubfont")
                self.assertEqual(uc.page_for(info(), lang), UPDATE_EN)

    def test_no_language_really_follows_the_default_not_a_fixed_answer(self) -> None:
        """Were the default Korean, no language would be the Korean page."""
        saved = uc.DEFAULT_LANG
        self.addCleanup(setattr, uc, "DEFAULT_LANG", saved)
        uc.DEFAULT_LANG = "ko"
        for lang in MISSING:
            with self.subTest(lang=lang):
                self.assertTrue(uc.is_korean(lang))
                self.assertEqual(path_of(uc.feedback_url(UPDATE_SLUG, APP_VERSION, lang)), "/tools/epubfont")
                self.assertEqual(uc.page_for(info(), lang), UPDATE_KO)
        for lang in ("en", "xx", "ko"):  # a code that is there is not touched by the default
            self.assertEqual(uc.is_korean(lang), lang == "ko")

    def test_one_judgement_for_both_links(self) -> None:
        """[Send feedback] and [Update] ask the same function, so they cannot drift apart."""
        for fn in (uc.feedback_url, uc.page_for):
            body = inspect.getsource(fn).split('"""')[-1]
            with self.subTest(fn=fn.__name__):
                self.assertIn("is_korean(lang)", body)
                self.assertNotRegex(body, r"lang\s*[!=]=|\"zh|\"ja|\"en\"")
        self.assertFalse(hasattr(uc, "FEEDBACK_EN_LANGS"))
        for lang in (*KOREAN, *NOT_KOREAN, *MISSING):
            with self.subTest(lang=lang):
                feedback_en = path_of(uc.feedback_url(UPDATE_SLUG, APP_VERSION, lang)).startswith("/en/")
                update_en = uc.page_for(info(), lang) == UPDATE_EN
                self.assertEqual(feedback_en, update_en)

    def test_without_an_english_page_the_update_link_stays_korean(self) -> None:
        for lang in (*NOT_KOREAN, *MISSING):
            for url_en in ("", "https://evil.example/en"):
                with self.subTest(lang=lang, url_en=url_en):
                    self.assertEqual(uc.page_for(info(url_en), lang), UPDATE_KO)

    def test_the_shape_of_every_address(self) -> None:
        for lang in (*KOREAN, *NOT_KOREAN, *MISSING):
            url = uc.feedback_url(UPDATE_SLUG, APP_VERSION, lang)
            with self.subTest(lang=lang):
                page = KO_PAGE if uc.is_korean(lang) else EN_PAGE
                self.assertEqual(url, f"{page}?utm_source=app&utm_medium=feedback&v={APP_VERSION}#feedback")
                self.assertTrue(url.startswith(uc.SITE))
                self.assertEqual(url.count("#"), 1)
                self.assertEqual(url.count("?"), 1)
                self.assertTrue(url.isascii())
                self.assertNotRegex(url, r"\s")


class TipPosition(unittest.TestCase):
    """b. Where the tooltip goes, worked out without a window: (left, top, right,
    bottom) areas stand for monitors, including ones at negative coordinates."""

    AREA = (0, 0, 1920, 1040)

    def place(self, link_x: int, tip_w: int = 400, area=AREA, link_y: int = 100,
              link_w: int = 90, link_h: int = 20, tip_h: int = 26) -> tuple[int, int]:
        return gui._tip_position(link_x, link_y, link_w, link_h, tip_w, tip_h, area)

    def assertInside(self, x: int, y: int, tip_w: int, tip_h: int, area) -> None:
        left, top, right, bottom = area
        self.assertGreaterEqual(x, left)
        self.assertLessEqual(x + tip_w, right)
        self.assertGreaterEqual(y, top)
        self.assertLessEqual(y + tip_h, bottom)

    def test_in_the_open_it_ends_where_the_link_ends_just_below_it(self) -> None:
        x, y = self.place(1000)
        self.assertEqual((x + 400, y), (1000 + 90, 100 + 20 + 4))

    def test_a_tip_shorter_than_the_link_starts_where_the_link_starts(self) -> None:
        self.assertEqual(self.place(1000, tip_w=50)[0], 1000)

    def test_at_the_right_edge_of_the_screen(self) -> None:
        for link_x in (1920 - 90, 1920 - 60, 1920 - 1, 1920 + 50, 3000):  # flush, then hanging off
            with self.subTest(link_x=link_x):
                x, y = self.place(link_x)
                self.assertInside(x, y, 400, 26, self.AREA)
                self.assertEqual(x, min(link_x + 90, 1920) - 400)

    def test_at_the_left_edge_of_the_screen(self) -> None:
        for link_x in (0, 50, 200, -40, -500):
            with self.subTest(link_x=link_x):
                x, y = self.place(link_x)
                self.assertInside(x, y, 400, 26, self.AREA)

    def test_a_monitor_left_of_the_main_one_is_not_cut_at_zero(self) -> None:
        """The old rule pushed the tip to x >= 0, onto the other monitor."""
        area = (-1920, 0, 0, 1040)
        for link_x in (-1920, -1900, -800, -90):
            with self.subTest(link_x=link_x):
                x, y = self.place(link_x, area=area)
                self.assertInside(x, y, 400, 26, area)
                self.assertLess(x, 0)

    def test_a_monitor_above_or_right_of_the_main_one(self) -> None:
        for area in ((0, -1080, 1920, -40), (1920, 0, 3840, 1040), (2880, 200, 5760, 1980)):
            left, top, right, _ = area
            for link_x in (left, left + 10, right - 90, right - 5):
                with self.subTest(area=area, link_x=link_x):
                    x, y = self.place(link_x, area=area, link_y=top + 30)
                    self.assertInside(x, y, 400, 26, area)

    def test_no_room_below_puts_it_above_the_link(self) -> None:
        x, y = self.place(1000, link_y=1040 - 22)
        self.assertEqual(y, 1040 - 22 - 4 - 26)
        self.assertInside(x, y, 400, 26, self.AREA)

    def test_every_language_and_scale_fits_at_both_edges(self) -> None:
        """Tip widths a little wider than the four sentences measure at 100% and 200%."""
        for tip_w in (330, 400, 470, 660, 800, 940):
            for link_x in (0, 1920 - 90, 1920 - 10, 1920 + 40):
                with self.subTest(tip_w=tip_w, link_x=link_x):
                    x, y = self.place(link_x, tip_w=tip_w)
                    self.assertInside(x, y, tip_w, 26, self.AREA)

    def test_a_tip_that_fits_the_window_stays_inside_it(self) -> None:
        """The link sits mid-header: a long Korean tip ending at the link would
        start left of the window."""
        window = (1000, 1866)
        x, _ = gui._tip_position(1400, 100, 90, 20, 600, 26, self.AREA, window)
        self.assertEqual(x, 1000)
        x, _ = gui._tip_position(1700, 100, 90, 20, 600, 26, self.AREA, window)
        self.assertEqual(x, 1790 - 600)  # room enough: still ends where the link ends

    def test_a_tip_wider_than_the_window_ignores_the_window(self) -> None:
        x, _ = gui._tip_position(1400, 100, 90, 20, 900, 26, self.AREA, (1000, 1866))
        self.assertEqual(x, 1490 - 900)

    def test_the_monitor_wins_over_the_window(self) -> None:
        for window in ((1500, 2366), (-600, 266)):  # hanging off the right, off the left
            with self.subTest(window=window):
                x, y = gui._tip_position(window[0] + 400, 100, 90, 20, 600, 26, self.AREA, window)
                self.assertInside(x, y, 600, 26, self.AREA)

    def test_a_tip_wider_than_the_monitor_keeps_its_start_on_screen(self) -> None:
        x, _ = self.place(100, tip_w=3000)
        self.assertEqual(x, 0)


class Readmes(unittest.TestCase):
    """d. The feedback link in the four READMEs: Korean readers get the Korean
    page, the others the English one, and no address runs into the text after it."""

    FILES = {"README.md": KO_PAGE, "README.en.md": EN_PAGE, "README.ja.md": EN_PAGE,
             "README.zh-CN.md": EN_PAGE}

    def read(self, name: str) -> str:
        with open(os.path.join(REPO, name), encoding="utf-8") as f:
            return f.read()

    def test_each_readme_has_one_feedback_link_to_its_page(self) -> None:
        for name, page in self.FILES.items():
            text = self.read(name)
            links = re.findall(r"https://[^\s()\[\]<>]*#feedback", text)
            with self.subTest(name=name):
                self.assertEqual(links, [page + "#feedback"])

    def test_each_readme_links_the_tools_page_in_its_language(self) -> None:
        for name, page in self.FILES.items():
            links = re.findall(r"https://chaekgalpi\.co\.kr/[^\s()\[\]<>?#]*tools/epubfont(?=\?utm_source=github)",
                               self.read(name))
            with self.subTest(name=name):
                self.assertEqual(links, [page])

    def test_the_other_languages_link_words_not_a_bare_address(self) -> None:
        for name in ("README.en.md", "README.ja.md", "README.zh-CN.md"):
            text = self.read(name)
            with self.subTest(name=name):
                self.assertRegex(text, r"\[[^\]\n]+\]\(" + re.escape(EN_PAGE + "#feedback") + r"\)")

    def test_no_bare_address_touches_the_text_after_it(self) -> None:
        """GitHub turns a bare address into a link up to the next space, taking a
        bracket, a particle or a full-width comma with it."""
        for name in self.FILES:
            text = self.read(name)
            for match in re.finditer(r"https?://\S+", text):
                if text[max(0, match.start() - 2):match.start()] == "](":
                    continue  # [words](address): the bracket ends it
                with self.subTest(name=name, url=match.group()):
                    self.assertRegex(match.group(), r"^https?://[A-Za-z0-9./?=&_#%:-]+$")

    def test_the_markdown_links_hold_only_the_address(self) -> None:
        for name in self.FILES:
            for url in re.findall(r"\]\((https?://[^)\s]*)\)", self.read(name)):
                with self.subTest(name=name, url=url):
                    self.assertRegex(url, r"^https?://[A-Za-z0-9./?=&_#%:-]+$")


class NoNetwork(unittest.TestCase):
    """f. Deciding and building addresses opens no socket."""

    def test_nothing_here_reaches_the_network(self) -> None:
        calls: list = []
        saved = socket.socket.connect, socket.create_connection, socket.getaddrinfo

        def record(*args, **kwargs):
            calls.append(args)
            raise OSError("no network in tests")

        socket.socket.connect = socket.create_connection = socket.getaddrinfo = record
        try:
            for lang in (*KOREAN, *NOT_KOREAN, *MISSING):
                uc.is_korean(lang)
                uc.feedback_url(UPDATE_SLUG, APP_VERSION, lang)
                uc.page_for(info(), lang)
            for link_x in range(-2000, 4000, 250):
                gui._tip_position(link_x, 100, 90, 20, 400, 26, (0, 0, 1920, 1040))
        finally:
            socket.socket.connect, socket.create_connection, socket.getaddrinfo = saved
        self.assertEqual(calls, [])
        self.assertEqual(fixtures.NETWORK_ATTEMPTS, [])


if __name__ == "__main__":
    sys.exit(unittest.main())
