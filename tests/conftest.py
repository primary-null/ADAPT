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


@pytest.fixture(autouse=True)
def _not_an_elevated_windows_session(monkeypatch):
    """CI Windows runners run as Administrator, which adds ADAPT's elevated-session warning to
    every human CLI run. Tests start from a normal session; the tests of that warning set
    `_is_windows_admin` (or `_privilege_warning_text`) themselves."""
    import adapt_adb.cli as cli_mod
    import adapt_adb.doctor as doctor_mod

    monkeypatch.setattr(cli_mod, "_is_windows_admin", lambda: False)
    monkeypatch.setattr(doctor_mod, "_is_windows_admin", lambda: False)
