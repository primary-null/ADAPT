"""Shared test isolation.

`adb.resolve_adb_details` reads the real ~/.profile of whoever runs the tests to find the
platform-tools folder `adapt setup` registered. A developer who ran `adapt setup` on a POSIX
host and runs the tests from a shell without that folder on PATH would otherwise change the
outcome of every test that resolves adb for real. Tests of that lookup re-enable it
explicitly (tests/test_registered_managed_adb.py).
"""

from __future__ import annotations

import pytest

import adapt_adb.adb as adb_mod

REAL_MANAGED_REGISTRATION = adb_mod._real_managed_registration


@pytest.fixture(autouse=True)
def _ignore_the_runners_registered_managed_copy(monkeypatch):
    monkeypatch.setattr(adb_mod, "_real_managed_registration", lambda override_var: None)
