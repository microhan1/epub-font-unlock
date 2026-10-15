"""Keeps every pytest run off the chaekgalpi server.

The window asks https://chaekgalpi.co.kr/api/tools/{slug}/latest 1.5 s after it
opens, and each test gets fresh settings, so the once-a-day guard is always open.
In another tool of the series that let the GUI tests reach the live server: 51
requests per full run, 511 over two days, all counted as people starting the tool.
tests/fixtures.py already replaces fetch's default connection when it is imported;
this makes the block independent of imports and turns any attempt into a failure.

- `fetch`'s default `opener` and `urllib.request.urlopen` itself are swapped for a
  guard that records the URL and fails like an offline machine. The default has to
  be changed in `__kwdefaults__`: it was bound when `fetch` was defined, so
  replacing `urllib.request.urlopen` alone would not reach it.
- `check_async` keeps its default `fetcher=None`: here that looks up
  `update_check.fetch` when the thread runs, which the GUI tests replace with
  their own answers and which otherwise ends at the guard.
- The guard is installed for the whole session and never taken down, so a request
  thread outliving its test still meets it; the attempt then fails the next test.
"""
from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import update_check  # noqa: E402

ATTEMPTS: list[str] = []


def _guard(req, timeout=None, *args, **kwargs):
    ATTEMPTS.append(getattr(req, "full_url", str(req)))
    raise urllib.error.URLError("tests must not reach the network")


def _install() -> None:
    urllib.request.urlopen = _guard
    update_check.fetch.__kwdefaults__["opener"] = _guard


_install()


@pytest.fixture(autouse=True)
def _no_update_requests():
    # again for every test: importing tests/fixtures.py puts its own opener back
    _install()
    ATTEMPTS.clear()
    yield
    if ATTEMPTS:
        attempts, ATTEMPTS[:] = list(ATTEMPTS), []
        pytest.fail(f"tried to reach the network ({len(attempts)}): {attempts}", pytrace=False)


@pytest.fixture
def network_attempts() -> list[str]:
    """The record, for the test that checks the guard itself."""
    return ATTEMPTS
