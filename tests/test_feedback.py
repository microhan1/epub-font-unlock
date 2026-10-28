"""[Send feedback]: the address it opens, the strings it shows, and what it leaves alone.

The window side (one click, one browser call, the failure message) is in
test_gui.py with the other window tests. Nothing here opens a browser or reaches
the network: the address is only built and taken apart again.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import socket
import sys
import unittest
import urllib.parse

import fixtures  # noqa: E402,F401  (inserts the repo root on sys.path)

import i18n  # noqa: E402
import main as cli  # noqa: E402
import update_check as uc  # noqa: E402
from version import APP_VERSION, UPDATE_SLUG  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KO = "https://chaekgalpi.co.kr/tools/epubfont?utm_source=app&utm_medium=feedback&v={v}#feedback"
EN = "https://chaekgalpi.co.kr/en/tools/epubfont?utm_source=app&utm_medium=feedback&v={v}#feedback"
KEYS = ("btn_feedback", "tip_feedback", "msg_feedback_failed", "log_feedback_url")


def query(url: str) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query, keep_blank_values=True)


def load(lang: str) -> dict:
    with open(os.path.join(REPO, "lang", f"{lang}.json"), encoding="utf-8") as f:
        return json.load(f)


class Address(unittest.TestCase):
    """a. Which page, for which language."""

    def test_the_four_languages_give_exactly_these_addresses(self) -> None:
        wanted = {"ko": KO, "en": EN, "ja": EN, "zh-CN": EN}
        for lang, template in wanted.items():
            with self.subTest(lang=lang):
                self.assertEqual(uc.feedback_url("epubfont", "0.2.1", lang), template.format(v="0.2.1"))

    def test_the_window_languages_are_all_covered(self) -> None:
        for lang in i18n.LANGS:
            url = uc.feedback_url(UPDATE_SLUG, APP_VERSION, lang)
            self.assertEqual(url, (KO if lang == "ko" else EN).format(v=APP_VERSION), lang)

    def test_an_unknown_language_gets_the_english_page(self) -> None:
        """Only Korean gets the Korean page; a code the window does not know is not
        a reason to send someone to a page in Korean."""
        for lang in ("fr", "zh", "zh-TW", "en-US", "klingon"):
            with self.subTest(lang=lang):
                self.assertEqual(uc.feedback_url("epubfont", "0.2.1", lang), EN.format(v="0.2.1"))

    def test_a_missing_language_follows_the_window_s_default(self) -> None:
        """No language at all is the window's default, English."""
        self.assertEqual(i18n.DEFAULT_LANG, "en")
        for lang in (None, "", "   ", 7, ["en"]):
            with self.subTest(lang=lang):
                self.assertEqual(uc.feedback_url("epubfont", "0.2.1", lang), EN.format(v="0.2.1"))

    def test_the_language_code_is_read_without_regard_to_case_or_spaces(self) -> None:
        for lang in ("EN", "En", "JA", "zh-cn", "ZH-CN", " en "):
            with self.subTest(lang=lang):
                self.assertEqual(uc.feedback_url("epubfont", "0.2.1", lang), EN.format(v="0.2.1"))
        for lang in ("KO", "Ko", " ko"):
            with self.subTest(lang=lang):
                self.assertEqual(uc.feedback_url("epubfont", "0.2.1", lang), KO.format(v="0.2.1"))

    def test_every_address_is_on_the_site_ends_at_the_box_and_carries_the_tags(self) -> None:
        for lang in (*i18n.LANGS, None, "xx"):
            for version in ("0.2.1", "", None, "a&b#c"):
                url = uc.feedback_url(UPDATE_SLUG, version, lang)
                with self.subTest(lang=lang, version=version):
                    self.assertTrue(url.startswith("https://chaekgalpi.co.kr/"), url)
                    self.assertTrue(url.startswith(uc.SITE), url)
                    self.assertTrue(url.endswith("#feedback"), url)
                    self.assertEqual(url.count("#"), 1, url)
                    parts = urllib.parse.urlsplit(url)
                    self.assertEqual((parts.scheme, parts.netloc), ("https", "chaekgalpi.co.kr"))
                    self.assertEqual(parts.fragment, "feedback")
                    self.assertIn(parts.path, ("/tools/epubfont", "/en/tools/epubfont"))
                    q = query(url)
                    self.assertEqual(q["utm_source"], ["app"])
                    self.assertEqual(q["utm_medium"], ["feedback"])
                    self.assertIn("utm_source=app", url)
                    self.assertIn("utm_medium=feedback", url)

    def test_there_is_no_japanese_or_chinese_page_in_the_address(self) -> None:
        for lang in ("ja", "zh-CN"):
            path = urllib.parse.urlsplit(uc.feedback_url("epubfont", "1", lang)).path
            self.assertEqual(path, "/en/tools/epubfont")

    def test_the_address_is_built_from_constants_not_from_settings(self) -> None:
        """Nothing a settings file or a server answer holds can move it."""
        before = uc.feedback_url("epubfont", "0.2.1", "ko")
        uc.page_for(uc.UpdateInfo("9.9.9", "0.2.1", "https://chaekgalpi.co.kr/x", "https://evil.example/"), "en")
        self.assertEqual(uc.feedback_url("epubfont", "0.2.1", "ko"), before)
        import inspect
        source = inspect.getsource(uc.feedback_url)
        for word in ("load_settings", "settings", "fetch", "urlopen", "open("):
            self.assertNotIn(word, source.split('"""')[-1], word)


class Version(unittest.TestCase):
    """b. What the version does to the address."""

    def test_ordinary_versions(self) -> None:
        cases = {"0.2.1": "v=0.2.1", "v0.2.1": "v=v0.2.1", "10.20.30": "v=10.20.30"}
        for version, part in cases.items():
            with self.subTest(version=version):
                url = uc.feedback_url("epubfont", version, "ko")
                self.assertIn("&" + part + "#feedback", url)
                self.assertEqual(query(url)["v"], [version])

    def test_a_plus_is_quoted_so_it_does_not_come_back_as_a_space(self) -> None:
        url = uc.feedback_url("epubfont", "1.0.0-beta+3", "en")
        self.assertEqual(url, EN.format(v="1.0.0-beta%2B3"))
        self.assertEqual(query(url)["v"], ["1.0.0-beta+3"])

    def test_no_version_leaves_v_out(self) -> None:
        for version in ("", None, "   ", "\t\n"):
            with self.subTest(version=version):
                for lang in ("ko", "en"):
                    url = uc.feedback_url("epubfont", version, lang)
                    base = "https://chaekgalpi.co.kr/" + ("" if lang == "ko" else "en/")
                    self.assertEqual(url, f"{base}tools/epubfont?utm_source=app&utm_medium=feedback#feedback")
                    self.assertNotIn("v", query(url))
                    self.assertNotIn("&v=", url)

    def test_odd_versions_are_quoted_and_come_back_unchanged(self) -> None:
        odd = ["0.2 beta", "a&b", "a#b", "1&utm_source=evil", "x#frag", "한글버전",
               "0.2.1 & 한글 #1 + ?=/%", "%41%", "  padded  ", "a\nb", "😀", "v" * 200]
        for version in odd:
            with self.subTest(version=version):
                url = uc.feedback_url("epubfont", version, "ko")
                parts = urllib.parse.urlsplit(url)
                self.assertEqual(parts.fragment, "feedback", url)
                self.assertEqual(parts.path, "/tools/epubfont", url)
                q = query(url)
                self.assertEqual(q["v"], [version], url)
                self.assertEqual(q["utm_source"], ["app"], url)  # not overridden
                self.assertEqual(sorted(q), ["utm_medium", "utm_source", "v"], url)
                self.assertTrue(url.endswith("#feedback"))
                self.assertEqual(url.count("#"), 1, url)
                self.assertEqual(url.count("?"), 1, url)
                self.assertTrue(url.isascii(), url)
                self.assertNotRegex(url, r"\s")

    def test_the_value_is_quoted_with_urllib(self) -> None:
        version = "0.2.1 & 한글 #1"
        url = uc.feedback_url("epubfont", version, "ko")
        self.assertIn("&v=" + urllib.parse.quote(version, safe="") + "#feedback", url)

    def test_the_program_s_own_version_goes_in(self) -> None:
        self.assertRegex(APP_VERSION, r"^\d+\.\d+\.\d+$")
        url = uc.feedback_url(UPDATE_SLUG, APP_VERSION, "ko")
        self.assertEqual(url, KO.format(v=APP_VERSION))


class Strings(unittest.TestCase):
    """e. The new words, in all four languages."""

    def tearDown(self) -> None:
        i18n.init("en")

    def test_every_new_key_is_in_every_language_and_not_empty(self) -> None:
        for lang in i18n.LANGS:
            data = load(lang)
            for key in KEYS:
                with self.subTest(lang=lang, key=key):
                    self.assertIn(key, data)
                    self.assertIsInstance(data[key], str)
                    self.assertTrue(data[key].strip())

    def test_the_wording_is_the_one_asked_for(self) -> None:
        wanted = {
            "ko": ("의견 보내기", "안 되는 것·불편한 것·바라는 기능을 알려 주세요(브라우저가 열립니다)"),
            "en": ("Send feedback", "Tell us what's broken or missing (opens your browser)"),
            "ja": ("ご意見を送る", "不具合・ご要望をお知らせください（ブラウザが開きます）"),
            "zh-CN": ("发送反馈", "告诉我们问题或建议（将打开浏览器）"),
        }
        for lang, (button, tip) in wanted.items():
            data = load(lang)
            self.assertEqual((data["btn_feedback"], data["tip_feedback"]), (button, tip), lang)

    def test_the_failure_message_shows_the_address_in_every_language(self) -> None:
        url = uc.feedback_url(UPDATE_SLUG, "0.2.1", "ko")
        texts = set()
        for lang in i18n.LANGS:
            i18n.init(lang)
            for key in ("msg_feedback_failed", "log_feedback_url"):
                text = i18n.t(key, url=url)
                with self.subTest(lang=lang, key=key):
                    self.assertIn(url, text)
                    self.assertNotIn("{", text)
            texts.add(i18n.t("msg_feedback_failed", url=url))
        self.assertEqual(len(texts), len(i18n.LANGS), "a language was left untranslated")

    def test_the_four_languages_word_the_button_differently(self) -> None:
        self.assertEqual(len({load(lang)["btn_feedback"] for lang in i18n.LANGS}), 4)


class LeavesTheRestAlone(unittest.TestCase):
    """f, g. The command line is as it was, and nothing here talks to anybody."""

    def tearDown(self) -> None:
        i18n.init("en")

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_help_still_works_and_says_nothing_about_feedback(self) -> None:
        for lang in i18n.LANGS:
            code, out, _ = self.run_cli(["--lang", lang, "--help"])
            with self.subTest(lang=lang):
                self.assertEqual(code, 0)
                self.assertIn("--font", out)
                self.assertNotIn("feedback", out.lower())
                self.assertNotIn(load(lang)["btn_feedback"], out)

    def test_version_is_still_not_an_option(self) -> None:
        """There never was a --version; this release does not add one."""
        code, _, err = self.run_cli(["--version"])
        self.assertEqual(code, 2)
        self.assertIn("--version", err)

    def test_building_addresses_touches_no_socket(self) -> None:
        calls: list = []
        saved = socket.socket.connect, socket.create_connection, socket.getaddrinfo

        def record(*args, **kwargs):
            calls.append(args)
            raise OSError("no network in tests")

        socket.socket.connect = record
        socket.create_connection = record
        socket.getaddrinfo = record
        try:
            for lang in (*i18n.LANGS, None):
                for version in ("0.2.1", None, "a b"):
                    uc.feedback_url(UPDATE_SLUG, version, lang)
        finally:
            socket.socket.connect, socket.create_connection, socket.getaddrinfo = saved
        self.assertEqual(calls, [])

    def test_the_update_check_is_not_changed_by_the_feedback_address(self) -> None:
        """f. The update API and the [Update] page are what they were."""
        self.assertEqual(uc.API, "https://chaekgalpi.co.kr/api/tools/{slug}/latest")
        info = uc.UpdateInfo("0.3.0", "0.2.0", "https://chaekgalpi.co.kr/tools/epubfont?utm_medium=update",
                             "https://chaekgalpi.co.kr/en/tools/epubfont?utm_medium=update")
        self.assertEqual(uc.page_for(info, "ko"), info.url)
        self.assertEqual(uc.page_for(info, "ja"), info.url_en)
        self.assertNotIn("feedback", uc.page_for(info, "en"))


class NoFormInTheProgram(unittest.TestCase):
    """The program opens the page and nothing more: no text box, no send."""

    def test_the_window_code_never_posts_or_requests(self) -> None:
        with open(os.path.join(REPO, "gui.py"), encoding="utf-8") as f:
            source = f.read()
        for word in ("urlopen", "urllib.request", "requests", "http.client", "socket"):
            self.assertNotIn(word, source, word)
        self.assertEqual(len(re.findall(r"feedback_url\(", source)), 1)


if __name__ == "__main__":
    sys.exit(unittest.main())
