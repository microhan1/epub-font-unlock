"""Update check: once a day, an off switch, silent failures, what is sent, when a
notice shows. The network is always faked -- these tests never reach the real
server (fixtures.py also shuts the default connection for the whole suite).
"""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse

import fixtures  # noqa: E402  (inserts the repo root on sys.path)

import i18n  # noqa: E402
import main as cli  # noqa: E402
import update_check as uc  # noqa: E402
import version  # noqa: E402

DAY = "2026-10-13"
OK = {"slug": "epubfont", "latest": "0.3.0", "tag": "v0.3.0", "client": "0.1.2",
      "updateAvailable": True,
      "url": "https://chaekgalpi.co.kr/tools/epubfont?utm_source=app&utm_medium=update"
             "&utm_campaign=tool_update&utm_content=epubfont"}
CURRENT = "0.1.2"


class FakeRoot:
    """after() runs the callback at once; the thread is run inline (see setUp)."""

    def __init__(self):
        self.delays: list[int] = []

    def after(self, ms, fn):
        self.delays.append(ms)
        fn()


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, status: int = 200):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Base(unittest.TestCase):
    def setUp(self) -> None:
        class InlineThread:
            def __init__(self, target, name=None, daemon=None):
                self.target = target

            def start(self):
                self.target()

        self.addCleanup(setattr, uc.threading, "Thread", threading.Thread)
        uc.threading.Thread = InlineThread

    def run_once(self, prefs, fetcher, day=DAY):
        """One app start. The caller records the day only when the server
        answered, as the window does."""
        results = []

        def on_result(info, answered):
            results.append(info)
            if answered:
                prefs.update_check_last = day

        root = FakeRoot()
        scheduled = uc.check_async(root, "epubfont", CURRENT, prefs, on_result, fetcher=fetcher, day=day)
        return scheduled, results, root

    @staticmethod
    def counting(answer):
        calls = []

        def fetcher(slug, version_):
            calls.append((slug, version_))
            return answer

        return fetcher, calls


class HowOften(Base):
    def test_same_day_twice_asks_once(self) -> None:
        prefs = uc.UpdatePrefs()
        fetcher, calls = self.counting(OK)
        self.run_once(prefs, fetcher)
        self.run_once(prefs, fetcher)
        self.assertEqual(len(calls), 1)
        self.run_once(prefs, fetcher, day="2026-10-14")
        self.assertEqual(len(calls), 2)

    def test_switched_off_never_asks(self) -> None:
        prefs = uc.UpdatePrefs(update_check=False)
        fetcher, calls = self.counting(OK)
        scheduled, results, _ = self.run_once(prefs, fetcher)
        self.assertFalse(scheduled)
        self.assertEqual((calls, results), ([], []))

    def test_offline_does_not_use_up_the_day(self) -> None:
        prefs = uc.UpdatePrefs()
        fetcher, calls = self.counting(None)  # no answer
        self.run_once(prefs, fetcher)
        self.run_once(prefs, fetcher)
        self.assertEqual(len(calls), 2)
        self.assertEqual(prefs.update_check_last, "")

    def test_an_answer_of_no_news_still_uses_up_the_day(self) -> None:
        prefs = uc.UpdatePrefs()
        fetcher, calls = self.counting(dict(OK, updateAvailable=False))
        self.run_once(prefs, fetcher)
        self.run_once(prefs, fetcher)
        self.assertEqual(len(calls), 1)
        self.assertEqual(prefs.update_check_last, DAY)

    def test_waits_one_and_a_half_seconds_first(self) -> None:
        _, _, root = self.run_once(uc.UpdatePrefs(), self.counting(OK)[0])
        self.assertEqual(root.delays[0], 1500)

    def test_a_window_that_is_already_gone_is_not_an_error(self) -> None:
        class Gone:
            def after(self, ms, fn):
                raise RuntimeError("main thread is not in main loop")

        self.assertFalse(uc.check_async(Gone(), "epubfont", CURRENT, uc.UpdatePrefs(),
                                        lambda *a: None, fetcher=self.counting(OK)[0], day=DAY))

class ThreadHandOff(unittest.TestCase):
    """The worker thread must not hold, or call into, anything of Tk's.

    It used to hand its answer over with root.after(0, ...on_result...). Close the
    window while the request was out and the thread became the last owner of the
    App: its Tk variables were freed off the main thread and Tcl aborted the whole
    process (Tcl_AsyncDelete: async handler deleted by the wrong thread). The
    worker now only fills a dict, and the Tk thread polls for it.
    """

    class QueueRoot:
        """A root whose after() queues; the test runs the queue, as Tk's loop would."""

        def __init__(self):
            self.queue: list[tuple[int, object]] = []
            self.delays: list[int] = []

        def after(self, ms, fn):
            self.delays.append(ms)
            self.queue.append((ms, fn))

        def run_one(self):
            ms, fn = self.queue.pop(0)
            fn()

    def test_the_worker_closure_holds_no_root_and_no_callback(self) -> None:
        captured = {}

        class CaptureThread:
            def __init__(self, target, name=None, daemon=None):
                captured["target"] = target

            def start(self):
                pass

        self.addCleanup(setattr, uc.threading, "Thread", threading.Thread)
        uc.threading.Thread = CaptureThread

        root = self.QueueRoot()
        on_result = lambda info, answered: None  # noqa: E731
        uc.check_async(root, "epubfont", CURRENT, uc.UpdatePrefs(), on_result,
                       fetcher=lambda s, v: OK, day=DAY)
        root.run_one()  # the delayed start
        target = captured["target"]
        held = [c.cell_contents for c in (target.__closure__ or ())] + list(target.__defaults__ or ())
        self.assertTrue(held, "the worker holds nothing at all; the check below would prove nothing")
        self.assertFalse(any(x is root or x is on_result for x in held))
        # and nothing it holds can reach them either: only plain data and the fetcher
        for item in held:
            self.assertIsInstance(item, (dict, str, type(None)) + (type(lambda: 0),))

    def test_the_answer_reaches_the_callback_on_the_thread_that_polls_not_the_worker(self) -> None:
        seen = {}

        def fetcher(slug, version_):
            seen["worker"] = threading.get_ident()
            return OK

        def on_result(info, answered):
            seen["callback"] = threading.get_ident()
            seen["result"] = (info, answered)

        root = self.QueueRoot()
        uc.check_async(root, "epubfont", CURRENT, uc.UpdatePrefs(), on_result, fetcher=fetcher, day=DAY)
        root.run_one()                    # DELAY_MS: starts the real thread, then polls
        for thread in threading.enumerate():
            if thread.name == "update-check":
                thread.join(5)
        while root.queue:
            root.run_one()                # the poll that finds the answer
        self.assertEqual(seen["callback"], threading.get_ident())
        self.assertNotEqual(seen["callback"], seen["worker"])
        self.assertEqual(seen["result"], (uc.UpdateInfo("0.3.0", CURRENT, OK["url"]), True))

    def test_polling_goes_on_at_the_poll_interval_until_the_answer_is_in(self) -> None:
        release = threading.Event()
        calls = []

        def fetcher(slug, version_):
            release.wait(5)
            return OK

        root = self.QueueRoot()
        uc.check_async(root, "epubfont", CURRENT, uc.UpdatePrefs(), lambda *a: calls.append(a),
                       fetcher=fetcher, day=DAY)
        root.run_one()                    # starts the thread; the first poll finds nothing yet
        for _ in range(3):
            root.run_one()                # more polls, still waiting
        self.assertEqual(calls, [])
        self.assertEqual(root.delays[0], uc.DELAY_MS)
        self.assertTrue(all(ms == uc.POLL_MS for ms in root.delays[1:]), root.delays)
        release.set()
        for thread in threading.enumerate():
            if thread.name == "update-check":
                thread.join(5)
        while root.queue:
            root.run_one()
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(root.queue), 0)  # and it stops asking once answered

    def test_a_window_that_closed_while_the_request_was_out_gets_no_callback(self) -> None:
        """Closing a Tk window discards its pending after() callbacks, so the poll
        simply never runs again; nothing has to be unwound."""
        release = threading.Event()
        calls = []
        root = self.QueueRoot()
        uc.check_async(root, "epubfont", CURRENT, uc.UpdatePrefs(), lambda *a: calls.append(a),
                       fetcher=lambda s, v: (release.wait(5), OK)[1], day=DAY)
        root.run_one()
        root.queue.clear()                # the window is destroyed: its callbacks are gone
        release.set()
        for thread in threading.enumerate():
            if thread.name == "update-check":
                thread.join(5)
        self.assertEqual(calls, [])

    def test_a_poll_that_cannot_reschedule_is_not_an_error(self) -> None:
        class Dying(self.QueueRoot):
            def after(self, ms, fn):
                if self.delays:  # the first (the delay) works; later ones fail
                    raise RuntimeError("the window is gone")
                super().after(ms, fn)

        release = threading.Event()
        root = Dying()
        uc.check_async(root, "epubfont", CURRENT, uc.UpdatePrefs(), lambda *a: None,
                       fetcher=lambda s, v: (release.wait(5), OK)[1], day=DAY)
        root.run_one()                    # the first poll tries to reschedule and cannot
        release.set()
        for thread in threading.enumerate():
            if thread.name == "update-check":
                thread.join(5)


class WhatIsSent(Base):
    def test_request_carries_only_slug_version_and_os(self) -> None:
        seen = {}

        def opener(req, timeout):
            seen["url"], seen["timeout"] = req.full_url, timeout
            seen["headers"] = {k.lower(): v for k, v in req.header_items()}
            return FakeResponse(json.dumps(OK).encode())

        self.assertEqual(uc.fetch("epubfont", CURRENT, opener=opener), OK)
        url = urllib.parse.urlsplit(seen["url"])
        self.assertEqual((url.scheme, url.netloc, url.path),
                         ("https", "chaekgalpi.co.kr", "/api/tools/epubfont/latest"))
        self.assertEqual(urllib.parse.parse_qs(url.query), {"v": [CURRENT], "os": ["win"]})
        self.assertEqual(seen["timeout"], 4.0)
        self.assertEqual(seen["headers"]["user-agent"], f"chaekgalpi-tool/epubfont/{CURRENT}")
        # nothing else about the user or the machine
        self.assertLessEqual(set(seen["headers"]), {"user-agent", "accept"})

    def test_the_program_sends_its_own_real_version_and_name(self) -> None:
        self.assertEqual(version.UPDATE_SLUG, "epubfont")
        self.assertRegex(version.APP_VERSION, r"^\d+\.\d+\.\d+$")

    def test_the_wire_data_has_no_path_user_or_machine_name(self) -> None:
        seen = {}

        def opener(req, timeout):
            seen["text"] = req.full_url + repr(req.header_items())
            return FakeResponse(b"{}")

        uc.fetch("epubfont", CURRENT, opener=opener)
        for private in (os.path.expanduser("~"), os.environ.get("USERNAME", "\0"),
                        os.environ.get("COMPUTERNAME", "\0"), "settings.json", ".epub"):
            self.assertNotIn(private, seen["text"])


class Failures(Base):
    def test_every_kind_of_failure_is_silent(self) -> None:
        def make(behaviour):
            def opener(req, timeout):
                if behaviour == "urlerror":
                    raise urllib.error.URLError("offline")
                if behaviour == "timeout":
                    raise socket.timeout("slow")
                if behaviour == "http500":
                    raise urllib.error.HTTPError(req.full_url, 500, "boom", {}, None)
                if behaviour == "oserror":
                    raise OSError("reset")
                if behaviour == "badjson":
                    return FakeResponse(b"<html>not json")
                if behaviour == "list":
                    return FakeResponse(b"[1, 2]")
                if behaviour == "nonutf8":
                    return FakeResponse(b"\xff\xfe\x00")
                return FakeResponse(json.dumps(OK).encode(), status=500)  # status500
            return opener

        for behaviour in ("urlerror", "timeout", "http500", "badjson", "list", "status500",
                          "oserror", "nonutf8"):
            with self.subTest(behaviour=behaviour):
                self.assertIsNone(uc.fetch("epubfont", CURRENT, opener=make(behaviour)))

    def test_a_huge_answer_is_cut_off_rather_than_read_whole(self) -> None:
        class Endless(FakeResponse):
            asked = None

            def read(self, n=-1):
                Endless.asked = n
                return b"{" * min(n, 10) if n > 0 else b"{" * 10**7

        self.assertIsNone(uc.fetch("epubfont", CURRENT, opener=lambda r, timeout: Endless(b"")))
        self.assertEqual(Endless.asked, uc.MAX_BYTES)


class WhenANoticeShows(Base):
    def test_no_notice_unless_the_server_says_exactly_true(self) -> None:
        for flag in (False, None, "true", 1, 0, [], {}):
            with self.subTest(flag=flag):
                self.assertIsNone(uc.interpret(dict(OK, updateAvailable=flag), CURRENT))

    def test_the_server_missing_the_flag_shows_nothing(self) -> None:
        data = dict(OK)
        del data["updateAvailable"]
        self.assertIsNone(uc.interpret(data, CURRENT))

    def test_no_data_at_all_shows_nothing(self) -> None:
        for data in (None, [], "x", 3):
            with self.subTest(data=data):
                self.assertIsNone(uc.interpret(data, CURRENT))

    def test_a_notice_once_when_newer(self) -> None:
        prefs = uc.UpdatePrefs()
        _, results, _ = self.run_once(prefs, self.counting(OK)[0])
        self.assertEqual(results, [uc.UpdateInfo("0.3.0", CURRENT, OK["url"])])

    def test_no_second_notice_the_same_day(self) -> None:
        prefs = uc.UpdatePrefs()
        fetcher, calls = self.counting(OK)
        self.run_once(prefs, fetcher)
        _, again, _ = self.run_once(prefs, fetcher)
        self.assertEqual((again, len(calls)), ([], 1))

    def test_later_hides_that_version_but_not_a_newer_one(self) -> None:
        prefs = uc.UpdatePrefs(update_check_skipped="0.3.0")
        self.assertIsNone(uc.interpret(OK, CURRENT, prefs.update_check_skipped))
        newer = dict(OK, latest="0.3.1")
        self.assertEqual(uc.interpret(newer, CURRENT, prefs.update_check_skipped).latest, "0.3.1")

    def test_later_is_honoured_through_a_whole_start(self) -> None:
        prefs = uc.UpdatePrefs(update_check_skipped="0.3.0")
        _, results, _ = self.run_once(prefs, self.counting(OK)[0])
        self.assertEqual(results, [None])  # the server answered, but there is nothing to show
        self.assertEqual(prefs.update_check_last, DAY)

    def test_only_pages_of_the_tools_site_are_opened(self) -> None:
        for url in ("https://github.com/microhan1/epub-font-unlock/releases",
                    "http://chaekgalpi.co.kr/tools/epubfont",
                    "https://chaekgalpi.co.kr.evil.example/", "https://evil.example/chaekgalpi.co.kr/",
                    "https://chaekgalpi.co.kr@evil.example/", "javascript:alert(1)",
                    "file:///C:/Windows/System32/calc.exe", "", None, 5):
            with self.subTest(url=url):
                self.assertIsNone(uc.interpret(dict(OK, url=url), CURRENT))

    def test_odd_versions_show_nothing(self) -> None:
        for latest in ("", "   ", None, 3, "x" * 40, ["0.3.0"]):
            with self.subTest(latest=latest):
                self.assertIsNone(uc.interpret(dict(OK, latest=latest), CURRENT))

    def test_the_version_is_trimmed(self) -> None:
        self.assertEqual(uc.interpret(dict(OK, latest=" 0.3.0 "), CURRENT).latest, "0.3.0")


class WhichPage(Base):
    """[Update] opens the Korean tools page in a Korean window and the English one
    in every other language, when the server names one. The server adds `urlEn` to
    its answer; nothing extra is sent to get it."""

    URL = OK["url"]
    URL_EN = ("https://chaekgalpi.co.kr/en/tools/epubfont?utm_source=app&utm_medium=update"
              "&utm_campaign=tool_update&utm_content=epubfont")

    def info(self, **extra) -> uc.UpdateInfo:
        info = uc.interpret(dict(OK, **extra), CURRENT)
        self.assertIsNotNone(info)
        return info

    def test_korean_always_gets_the_korean_page(self) -> None:
        self.assertEqual(uc.page_for(self.info(urlEn=self.URL_EN), "ko"), self.URL)
        self.assertEqual(uc.page_for(self.info(), "ko"), self.URL)

    def test_english_gets_the_english_page_when_there_is_one(self) -> None:
        self.assertEqual(uc.page_for(self.info(urlEn=self.URL_EN), "en"), self.URL_EN)

    def test_japanese_and_chinese_get_the_english_page_too(self) -> None:
        for lang in ("ja", "zh-CN"):
            with self.subTest(lang=lang):
                self.assertEqual(uc.page_for(self.info(urlEn=self.URL_EN), lang), self.URL_EN)

    def test_without_a_usable_english_page_every_language_gets_the_korean_one(self) -> None:
        missing = dict(OK)
        missing.pop("urlEn", None)
        bad = [None, "", "   ", 5, [], {}, True,
               "http://chaekgalpi.co.kr/en/tools/epubfont",            # not https
               "https://chaekgalpi.co.kr.evil.example/en/tools/epubfont",
               "https://evil.example/chaekgalpi.co.kr/",
               "https://chaekgalpi.co.kr@evil.example/en",
               "javascript:alert(1)", "file:///C:/Windows/System32/calc.exe",
               "chaekgalpi.co.kr/en/tools/epubfont"]                    # no scheme
        cases = [("missing key", missing)] + [(f"urlEn={v!r}", dict(OK, urlEn=v)) for v in bad]
        for label, data in cases:
            for lang in ("ko", "en", "ja", "zh-CN"):
                with self.subTest(case=label, lang=lang):
                    info = uc.interpret(data, CURRENT)
                    self.assertIsNotNone(info, "a bad urlEn must not hide the notice")
                    self.assertEqual(uc.page_for(info, lang), self.URL)

    def test_a_bad_english_page_does_not_hide_the_notice_or_change_the_rest(self) -> None:
        info = uc.interpret(dict(OK, urlEn="https://evil.example/"), CURRENT)
        self.assertEqual((info.latest, info.current, info.url, info.url_en),
                         ("0.3.0", CURRENT, self.URL, ""))

    def test_the_english_page_does_not_rescue_a_korean_page_off_the_site(self) -> None:
        """url is still required and still checked; urlEn is an addition to it."""
        self.assertIsNone(uc.interpret(dict(OK, url="https://evil.example/", urlEn=self.URL_EN), CURRENT))

    def test_whatever_page_is_chosen_is_on_the_tools_site(self) -> None:
        for lang in ("ko", "en", "ja", "zh-CN", "fr", ""):
            for extra in ({}, {"urlEn": self.URL_EN}, {"urlEn": "https://evil.example/"}):
                with self.subTest(lang=lang, extra=extra):
                    self.assertTrue(uc.page_for(self.info(**extra), lang).startswith(uc.SITE))

    def test_asking_for_the_english_page_adds_nothing_to_the_request(self) -> None:
        seen = {}

        def opener(req, timeout):
            seen["url"], seen["headers"] = req.full_url, {k.lower(): v for k, v in req.header_items()}
            return FakeResponse(json.dumps(dict(OK, urlEn=self.URL_EN)).encode())

        data = uc.fetch("epubfont", CURRENT, opener=opener)
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(seen["url"]).query),
                         {"v": [CURRENT], "os": ["win"]})
        self.assertLessEqual(set(seen["headers"]), {"user-agent", "accept"})
        self.assertEqual(data["urlEn"], self.URL_EN)  # it comes back; it is never asked for

    def test_through_a_whole_start_the_english_page_is_carried_to_the_notice(self) -> None:
        _, results, _ = self.run_once(uc.UpdatePrefs(), self.counting(dict(OK, urlEn=self.URL_EN))[0])
        self.assertEqual(results, [uc.UpdateInfo("0.3.0", CURRENT, self.URL, self.URL_EN)])

    def test_an_old_server_answer_with_no_urlEn_still_works_end_to_end(self) -> None:
        _, results, _ = self.run_once(uc.UpdatePrefs(), self.counting(OK)[0])
        self.assertEqual(results, [uc.UpdateInfo("0.3.0", CURRENT, self.URL)])
        self.assertEqual(results[0].url_en, "")


class Settings(unittest.TestCase):
    def test_defaults(self) -> None:
        prefs = uc.load_prefs({})
        self.assertEqual((prefs.update_check, prefs.update_check_last, prefs.update_check_skipped),
                         (True, "", ""))

    def test_wrong_types_fall_back_to_defaults(self) -> None:
        prefs = uc.load_prefs({"update_check": "no", "update_check_last": 20261013,
                               "update_check_skipped": ["0.3.0"]})
        self.assertEqual((prefs.update_check, prefs.update_check_last, prefs.update_check_skipped),
                         (True, "", ""))

    def test_an_integer_is_not_a_switch(self) -> None:
        self.assertTrue(uc.load_prefs({"update_check": 0}).update_check)

    def test_settings_that_are_not_a_dict(self) -> None:
        for raw in (None, [], "x", 5):
            with self.subTest(raw=raw):
                self.assertTrue(uc.load_prefs(raw).update_check)

    def test_long_values_are_cut(self) -> None:
        prefs = uc.load_prefs({"update_check_last": "2026-10-13-and-more", "update_check_skipped": "9" * 99})
        self.assertEqual(len(prefs.update_check_last), 10)
        self.assertEqual(len(prefs.update_check_skipped), uc.MAX_VERSION_LEN)

    def test_storing_leaves_every_other_key_alone(self) -> None:
        settings = {"lang": "ja", "options": {"font": False}, "something_else": [1, 2]}
        uc.store_prefs(uc.UpdatePrefs(False, DAY, "0.3.0"), settings)
        self.assertEqual(settings["lang"], "ja")
        self.assertEqual(settings["options"], {"font": False})
        self.assertEqual(settings["something_else"], [1, 2])
        self.assertEqual((settings["update_check"], settings["update_check_last"],
                          settings["update_check_skipped"]), (False, DAY, "0.3.0"))

    def test_load_after_store_is_the_same(self) -> None:
        prefs = uc.UpdatePrefs(False, DAY, "0.3.0")
        self.assertEqual(uc.load_prefs(uc.store_prefs(prefs, {})), prefs)


class CommandLineNeverAsks(unittest.TestCase):
    """The new-version check belongs to the window. A script that runs the tool
    over a folder must never be made to wait on, or report to, a server."""

    def test_run_cli_does_not_touch_the_network_or_the_update_module(self) -> None:
        directory = tempfile.mkdtemp(prefix="efu_upd_")
        self.addCleanup(shutil.rmtree, directory, True)
        book = fixtures.standard(os.path.join(directory, "book.epub"))
        calls = []
        self.addCleanup(setattr, uc, "fetch", uc.fetch)
        uc.fetch = lambda *a, **k: calls.append(a)
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = cli.run_cli(cli.build_parser().parse_args([book, "--font"]))
        finally:
            sys.stdout, sys.stderr = old
        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertEqual(fixtures.NETWORK_ATTEMPTS, [])

    def test_main_does_not_import_the_update_module(self) -> None:
        """The command line imports main and unlock, not the window."""
        import ast
        for name in ("main.py", "unlock.py", "epub_io.py", "i18n.py"):
            path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), name)
            with open(path, encoding="utf-8") as f:
                tree = ast.parse(f.read())
            imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            imported |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
            self.assertNotIn("update_check", imported, name)
            # urllib.parse (splitting file names) is fine; the network half is not
            self.assertNotIn("urllib.request", imported, name)
            self.assertNotIn("http.client", imported, name)
            self.assertNotIn("socket", imported, name)


class NoDownloads(unittest.TestCase):
    def test_the_module_can_only_open_a_page_never_fetch_a_file(self) -> None:
        """Nothing here downloads or replaces a program: the module reads one small
        JSON answer, and the window only opens the site's page in a browser."""
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "update_check.py")
        with open(path, encoding="utf-8") as f:
            source = f.read()
        for forbidden in ("urlretrieve", "shutil", "subprocess", "os.replace", "os.rename", ".exe"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    sys.exit(unittest.main())
