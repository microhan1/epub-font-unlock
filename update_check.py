"""Once a day after start: ask the chaekgalpi tools site whether a newer version exists.

Only the tool's slug, its version and "win" are sent - no file, path, device or user
information. The request runs on a background thread 1.5 s after the window is shown and
never blocks the UI; any failure (offline, timeout, HTTP error, bad JSON) is silent. The
server decides whether the version is older (`updateAvailable`); null means "cannot
compare" and shows nothing. Standard library only. The window only; the command line
never asks, and nothing here downloads or replaces anything.

Also the address [Send feedback] opens (feedback_url); that one is only handed to
the browser, never requested from here.
"""
from __future__ import annotations

import dataclasses
import datetime
import json
import threading
import urllib.parse
import urllib.request
from typing import Callable

from i18n import DEFAULT_LANG

API = "https://chaekgalpi.co.kr/api/tools/{slug}/latest"
SITE = "https://chaekgalpi.co.kr/"  # the notice only ever opens a page on this site
TIMEOUT = 4.0
DELAY_MS = 1500
POLL_MS = 100  # how often the Tk thread looks for the answer
MAX_BYTES = 64 * 1024
MAX_VERSION_LEN = 32


@dataclasses.dataclass
class UpdateInfo:
    latest: str
    current: str
    url: str
    # The English tools page, when the server names one (`urlEn`) and it is a page
    # on this site; "" otherwise. A missing or odd one never hides the notice.
    url_en: str = ""


def is_korean(lang: str | None) -> bool:
    """Whether a window language gets the Korean tools page. Both [Update] and
    [Send feedback] ask this, so they never disagree. The first part of the code
    decides ("ko", "KO", "ko-KR", "ko_KR"); every other code, listed or not, gets
    the English page, as there is no Japanese or Chinese one. No language at all
    (None, "", spaces, not text) counts as the window's default, i18n.DEFAULT_LANG."""
    code = lang.strip() if isinstance(lang, str) else ""
    return (code or DEFAULT_LANG).lower().replace("_", "-").split("-")[0] == "ko"


def page_for(info: UpdateInfo, lang: str | None) -> str:
    """Which tools page [Update] opens: the Korean one for a Korean window, the
    English one for every other language when the server gave one, else the
    Korean one. Whatever is chosen has already been checked to be on the site."""
    if not is_korean(lang) and info.url_en.startswith(SITE):
        return info.url_en
    return info.url


def feedback_url(slug: str, version: str | None, lang: str | None) -> str:
    """The feedback box on the tools page, which [Send feedback] opens in the browser.

    Put together from constants only, never from settings or a server answer. The
    page needs `#feedback` to scroll down to the box, and `utm_source=app` to file
    the message under the Windows program. The version is quoted whole (a "+" left
    bare would come back as a space); an empty one leaves `v` out. Nothing is sent
    from here: the browser asks for the page, and the user decides what to write."""
    path = "tools" if is_korean(lang) else "en/tools"
    params = [("utm_source", "app"), ("utm_medium", "feedback")]
    if isinstance(version, str) and version.strip():
        params.append(("v", version))
    query = "&".join(f"{key}={urllib.parse.quote(value, safe='')}" for key, value in params)
    return f"{SITE}{path}/{urllib.parse.quote(slug, safe='')}?{query}#feedback"


@dataclasses.dataclass
class UpdatePrefs:
    """What is remembered in settings.json about this feature."""

    update_check: bool = True       # the checkbox; on by default
    update_check_last: str = ""     # YYYY-MM-DD of the last check the server answered
    update_check_skipped: str = ""  # "Later" was pressed for this version


def load_prefs(settings: dict) -> UpdatePrefs:
    """The three keys from settings.json, trusting nothing about their types: the
    file sits beside the exe where anyone can edit it."""
    settings = settings if isinstance(settings, dict) else {}
    default = UpdatePrefs()

    def typed(key: str, kind: type):
        value = settings.get(key)
        return value if isinstance(value, kind) else getattr(default, key)

    return UpdatePrefs(
        update_check=typed("update_check", bool),
        update_check_last=typed("update_check_last", str).strip()[:10],
        update_check_skipped=typed("update_check_skipped", str).strip()[:MAX_VERSION_LEN],
    )


def store_prefs(prefs: UpdatePrefs, settings: dict) -> dict:
    """Write the three keys into a settings dict, leaving every other key alone."""
    settings["update_check"] = bool(prefs.update_check)
    settings["update_check_last"] = prefs.update_check_last
    settings["update_check_skipped"] = prefs.update_check_skipped
    return settings


def today() -> str:
    return datetime.date.today().isoformat()


def should_check(prefs, day: str) -> bool:
    """Checking is on and has not been done today."""
    return bool(getattr(prefs, "update_check", True)) and getattr(prefs, "update_check_last", "") != day


def fetch(slug: str, version: str, *, opener: Callable = urllib.request.urlopen) -> dict | None:
    """The server's answer, or None on any failure."""
    query = urllib.parse.urlencode({"v": version, "os": "win"})
    req = urllib.request.Request(f"{API.format(slug=slug)}?{query}",
                                 headers={"User-Agent": f"chaekgalpi-tool/{slug}/{version}",
                                          "Accept": "application/json"})
    try:
        with opener(req, timeout=TIMEOUT) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            data = json.loads(resp.read(MAX_BYTES).decode("utf-8"))
    except Exception:  # offline, timeout, HTTP error, bad JSON: say nothing
        return None
    return data if isinstance(data, dict) else None


def interpret(data: dict | None, version: str, skipped: str = "") -> UpdateInfo | None:
    """An UpdateInfo only when the server says a newer version exists, it is not the
    version the user chose to skip, and the link points at the tools site."""
    if not isinstance(data, dict) or data.get("updateAvailable") is not True:
        return None
    latest, url = data.get("latest"), data.get("url")
    if not isinstance(latest, str) or not latest.strip() or len(latest) > MAX_VERSION_LEN:
        return None
    if not isinstance(url, str) or not url.startswith(SITE):
        return None
    latest = latest.strip()
    if latest == (skipped or "").strip():
        return None
    url_en = data.get("urlEn")
    if not isinstance(url_en, str) or not url_en.startswith(SITE):
        url_en = ""  # absent, null, not text, or another domain: the Korean page is used
    return UpdateInfo(latest, version, url, url_en)


def check_async(root, slug: str, version: str, prefs, on_result: Callable[[UpdateInfo | None, bool], None], *,
                fetcher: Callable | None = None, day: str | None = None) -> bool:
    """Schedule one check DELAY_MS after now. on_result(info, answered) runs on the Tk
    thread: `answered` is True when the server replied (the caller records the day then),
    `info` is set only when a notice should be shown. Returns False when no check is due."""
    if not should_check(prefs, day or today()):
        return False
    skipped = getattr(prefs, "update_check_skipped", "")
    box: dict = {}

    # The thread only fetches into `box`; it holds no Tk object. It used to hand the
    # result over with root.after(0, ...on_result...), which left the thread holding
    # the window and its callback: close the window while the request was out and
    # the thread became the last owner of the App, its Tk variables were freed off
    # the main thread, and Tcl aborted the whole process ("Tcl_AsyncDelete: async
    # handler deleted by the wrong thread"). The Tk thread polls for the answer
    # instead, so the worker never touches, or keeps alive, anything of Tk's.
    def work(box=box) -> None:
        # looked up when the thread runs, not when this function is defined, so a
        # test that replaces update_check.fetch is honoured
        box["data"] = (fetcher or fetch)(slug, version)

    def poll() -> None:
        """Runs on the Tk thread."""
        if "data" not in box:
            try:
                root.after(POLL_MS, poll)
            except Exception:  # the window is gone; there is nobody to tell
                pass
            return
        data = box["data"]
        on_result(interpret(data, version, skipped), data is not None)

    def start() -> None:
        threading.Thread(target=work, name="update-check", daemon=True).start()
        poll()

    try:
        root.after(DELAY_MS, start)
    except Exception:
        return False
    return True
