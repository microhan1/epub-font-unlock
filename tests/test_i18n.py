"""The language loader and the language files themselves.

check_i18n.py compares the four files as text. These tests go the other way:
they load them the way the program does and check that every key the code asks
for comes back translated, with the placeholders the caller actually passes.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import unittest

import fixtures  # noqa: E402,F401  (inserts the repo root on sys.path)

import i18n  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def load(lang: str) -> dict:
    with open(os.path.join(REPO, "lang", f"{lang}.json"), encoding="utf-8") as f:
        return json.load(f)


class Files(unittest.TestCase):
    def test_every_language_has_a_name_and_a_file(self) -> None:
        for lang in i18n.LANGS:
            self.assertIn(lang, i18n.LANG_NAMES)
            self.assertTrue(load(lang))

    def test_the_prd_keys_say_what_the_prd_says(self) -> None:
        """The PRD fixes these strings; a well-meaning rewording is a change to
        the spec, not to the code."""
        wanted = {
            "app_title": ("EPUB 글꼴 풀기", "EPUB Font Unlock"),
            "drop_hint": ("EPUB 파일이나 폴더를 여기에 끌어다 놓으세요",
                          "Drop EPUB files or a folder here"),
            "analysis_summary": ("고정 글꼴 {fonts}종, 절대 크기 {sizes}곳, 절대 줄간격 {lines}곳",
                                 "{fonts} fixed fonts, {sizes} absolute sizes, "
                                 "{lines} absolute line heights"),
            "opt_font": ("글꼴 고정 해제", "Unlock font"),
            "opt_size": ("글자 크기 고정 해제", "Unlock font size"),
            "opt_line": ("줄간격 고정 해제", "Unlock line height"),
            "opt_margin": ("여백 정리", "Reset margins"),
            "opt_remove_files": ("내장 폰트 파일 삭제", "Remove embedded font files"),
            "btn_run": ("실행", "Run"),
            "msg_done": ("완료: {count}개 파일 처리", "Done: {count} files processed"),
            "msg_already": ("이미 풀려 있습니다", "Already unlocked"),
            "err_drm": ("DRM이 걸린 파일은 처리할 수 없습니다",
                        "DRM-protected files cannot be processed"),
        }
        ko, en = load("ko"), load("en")
        for key, (want_ko, want_en) in wanted.items():
            self.assertEqual(ko[key], want_ko, key)
            self.assertEqual(en[key], want_en, key)

    def test_the_app_title_is_translated_everywhere(self) -> None:
        titles = {lang: load(lang)["app_title"] for lang in i18n.LANGS}
        self.assertEqual(titles["zh-CN"], "EPUB字体解锁")
        self.assertEqual(titles["ja"], "EPUBフォント解除")
        self.assertEqual(len(set(titles.values())), len(i18n.LANGS), titles)

    def test_placeholders_are_the_same_in_every_language(self) -> None:
        base = load("en")
        for key, text in base.items():
            wanted = set(PLACEHOLDER_RE.findall(text))
            for lang in i18n.LANGS:
                self.assertEqual(set(PLACEHOLDER_RE.findall(load(lang)[key])), wanted,
                                 f"{key} in {lang}")

    def test_every_string_formats_with_its_own_placeholders(self) -> None:
        for lang in i18n.LANGS:
            i18n.set_lang(lang, persist=False)
            for key, text in load(lang).items():
                args = {name: 1 for name in PLACEHOLDER_RE.findall(text)}
                result = i18n.t(key, **args)
                self.assertNotIn("{", result, f"{lang}:{key} -> {result}")
        i18n.init("en")


class Loader(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="efu_i18n_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        original = i18n.SETTINGS_PATH
        i18n.SETTINGS_PATH = os.path.join(self.dir, "settings.json")
        self.addCleanup(setattr, i18n, "SETTINGS_PATH", original)
        self.addCleanup(i18n.init, "en")

    def write_settings(self, data) -> None:
        with open(i18n.SETTINGS_PATH, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def test_an_explicit_language_wins_over_the_saved_one(self) -> None:
        self.write_settings({"lang": "ja"})
        self.assertEqual(i18n.init("zh-CN"), "zh-CN")

    def test_the_saved_language_is_used_when_none_is_given(self) -> None:
        self.write_settings({"lang": "ja"})
        self.assertEqual(i18n.init(None), "ja")

    def test_an_unknown_language_falls_back_to_the_os_or_english(self) -> None:
        self.write_settings({"lang": "klingon"})
        self.assertIn(i18n.init(None), i18n.LANGS)
        self.assertIn(i18n.init("klingon"), i18n.LANGS)

    def test_a_non_string_language_does_not_crash(self) -> None:
        self.write_settings({"lang": 7})
        self.assertIn(i18n.init(None), i18n.LANGS)

    def test_the_os_language_is_one_we_ship(self) -> None:
        self.assertIn(i18n.detect_os_lang(), i18n.LANGS)

    def test_broken_settings_load_as_empty(self) -> None:
        for content in ("{ not json", "[]", '"a string"', ""):
            with self.subTest(content=content):
                self.write_settings(content)
                self.assertEqual(i18n.load_settings(), {})

    def test_settings_round_trip(self) -> None:
        i18n.save_settings({"lang": "ko", "options": {"font": True}})
        self.assertEqual(i18n.load_settings()["options"], {"font": True})

    def test_saving_to_an_unwritable_path_is_survivable(self) -> None:
        i18n.SETTINGS_PATH = os.path.join(self.dir, "no-such-dir", "settings.json")
        i18n.save_settings({"lang": "ko"})  # must not raise
        self.assertEqual(i18n.load_settings(), {})

    def test_choosing_a_language_saves_it(self) -> None:
        i18n.init("en")
        i18n.set_lang("ja")
        self.assertEqual(i18n.load_settings()["lang"], "ja")
        self.assertEqual(i18n.current_lang(), "ja")

    def test_an_unknown_key_comes_back_as_itself(self) -> None:
        i18n.init("ko")
        self.assertEqual(i18n.t("no_such_key"), "no_such_key")

    def test_a_missing_placeholder_leaves_the_text_alone(self) -> None:
        i18n.init("en")
        self.assertIn("{count}", i18n.t("msg_done"))  # no kwargs: not formatted
        self.assertEqual(i18n.t("msg_done", wrong=1), load("en")["msg_done"])

    def test_english_fills_in_for_a_key_a_translation_is_missing(self) -> None:
        i18n.init("ko")
        self.assertEqual(i18n.t("btn_run"), "실행")
        i18n._inst._strings.pop("btn_run")
        self.assertEqual(i18n.t("btn_run"), "Run")

    def test_the_settings_file_sits_next_to_the_program(self) -> None:
        self.assertEqual(os.path.dirname(os.path.abspath(i18n.__file__)), i18n.app_dir())
        self.assertTrue(os.path.isdir(os.path.join(i18n.resource_dir(), "lang")))


class UsedByTheCode(unittest.TestCase):
    """Every key the program asks for must exist in all four files."""

    def test_no_key_used_in_the_code_is_missing(self) -> None:
        used: set[str] = set()
        for name in ("gui.py", "main.py", "unlock.py", "epub_io.py", "i18n.py"):
            with open(os.path.join(REPO, name), encoding="utf-8") as f:
                used |= set(re.findall(r'\bt\(\s*["\']([a-z][a-z0-9_]*)["\']', f.read()))
        self.assertTrue(used)
        for lang in i18n.LANGS:
            missing = sorted(used - set(load(lang)))
            self.assertEqual(missing, [], f"{lang} is missing {missing}")


if __name__ == "__main__":
    sys.exit(unittest.main())
