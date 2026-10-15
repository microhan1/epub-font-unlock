"""The block in conftest.py is really in place (pytest only; unittest finds nothing here)."""
from __future__ import annotations

import urllib.error
import urllib.request

import pytest

import update_check


def test_nothing_in_the_suite_can_reach_the_update_server(network_attempts) -> None:
    guard = update_check.fetch.__kwdefaults__["opener"]
    assert guard.__module__ == "conftest", "fetch's default opener is not the guard"
    assert urllib.request.urlopen is guard, "urllib.request.urlopen is not the guard"
    assert update_check.check_async.__kwdefaults__["fetcher"] is None

    # no opener given: the default one is used, recorded, and fails like offline
    assert update_check.fetch("epubfont", "0.0.0") is None
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen("https://chaekgalpi.co.kr/")

    assert network_attempts == [
        "https://chaekgalpi.co.kr/api/tools/epubfont/latest?v=0.0.0&os=win",
        "https://chaekgalpi.co.kr/",
    ]
    network_attempts.clear()  # these two were on purpose; the teardown check stays quiet
