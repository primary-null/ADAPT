"""Transaction, identity, and allowlist tests for bounded Developer Options."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from adapt_adb import dev_options
from adapt_adb.dev_options import DevOptionsError, dev_options_app


@pytest.fixture(autouse=True)
def isolated_server_gate(monkeypatch):
    monkeypatch.setattr(dev_options, "preflight_adb_server", lambda *a, **k: None)


class FakeAdb:
    def __init__(self) -> None:
        self.serialno = "physical-123"
        self.fingerprint = "vendor/device/product:16/BUILD/123:user/release-keys"
        self.user = 10
        self.sdk = "36"
        self.settings: dict[tuple[str, int | None, str], str] = {
            ("global", None, "development_settings_enabled"): "1",
            ("global", None, "adb_enabled"): "1",
            ("global", None, "adb_wifi_enabled"): "0",
            ("global", None, "window_animation_scale"): "1.0",
            ("global", None, "transition_animation_scale"): "0.5",
            ("global", None, "animator_duration_scale"): "2",
            ("system", 10, "show_touches"): "0",
            ("system", 10, "pointer_location"): "0",
            ("global", None, "always_finish_activities"): "0",
            ("secure", 10, "anr_show_background"): "0",
        }
        self.calls: list[list[str]] = []
        self.fail_put_key: str | None = None
        self.exit_zero_denied_key: str | None = None
        self.before_first_put = None
        self.before_put = None
        self._put_count = 0

    def __call__(self, args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(args))
        assert timeout == dev_options.ADB_TIMEOUT_SECONDS
        assert args[1:3] == ["-s", "endpoint-5555"]
        shell = args[3:]
        if shell[:2] == ["shell", "getprop"]:
            if shell[2] == "ro.build.version.sdk":
                return subprocess.CompletedProcess(args, 0, self.sdk + "\n", "")
            value = self.serialno if shell[2] == "ro.serialno" else self.fingerprint
            return subprocess.CompletedProcess(args, 0, value + "\n", "")
        if shell == ["shell", "am", "get-current-user"]:
            return subprocess.CompletedProcess(args, 0, f"{self.user}\n", "")
        assert shell[:2] == ["shell", "settings"]
        pos = 2
        setting_user = None
        if shell[pos] == "--user":
            setting_user = int(shell[pos + 1])
            pos += 2
        action, table, key = shell[pos : pos + 3]
        identity = (table, setting_user, key)
        if action == "get":
            if key == self.exit_zero_denied_key:
                return subprocess.CompletedProcess(args, 0, "", "Permission denial: blocked\n")
            return subprocess.CompletedProcess(args, 0, self.settings.get(identity, "null") + "\n", "")
        if action == "put":
            self._put_count += 1
            if self._put_count == 1 and self.before_first_put:
                self.before_first_put()
            if self.before_put:
                self.before_put(key)
            if key == self.fail_put_key:
                return subprocess.CompletedProcess(args, 1, "", "write failed\n")
            self.settings[identity] = shell[pos + 3]
            return subprocess.CompletedProcess(args, 0, "", "")
        if action == "delete":
            self.settings.pop(identity, None)
            return subprocess.CompletedProcess(args, 0, "Deleted 1 rows\n", "")
        raise AssertionError(shell)


def test_desktop_profile_restores_original_and_absent_keys(fake_adb, tmp_path):
    snapshot = tmp_path / "desktop.json"
    key = ("global", None, "enable_freeform_support")
    absent = ("global", None, "force_desktop_mode_on_external_displays")
    fake_adb.settings[key] = "0"
    report = dev_options.apply_profile("desktop-testing", "endpoint-5555", snapshot, adb_path="adb")
    assert report["ok"] and fake_adb.settings[key] == "1" and fake_adb.settings[absent] == "1"
    assert dev_options.restore_snapshot("endpoint-5555", snapshot, adb_path="adb")["ok"]
    assert fake_adb.settings[key] == "0" and absent not in fake_adb.settings


def test_desktop_profile_partial_failure_rolls_back(fake_adb, tmp_path):
    fake_adb.fail_put_key = "force_desktop_mode_on_external_displays"
    with pytest.raises(DevOptionsError, match="rollback"):
        dev_options.apply_profile("desktop-testing", "endpoint-5555", tmp_path / "desktop.json", adb_path="adb")
    assert ("global", None, "enable_freeform_support") not in fake_adb.settings


@pytest.mark.parametrize("sdk", ["28", "38", "unknown"])
def test_desktop_profile_sdk_gate_makes_no_snapshot_or_setting_writes(fake_adb, tmp_path, sdk):
    fake_adb.sdk = sdk
    snapshot = tmp_path / "desktop.json"
    with pytest.raises(DevOptionsError, match="API 29"):
        dev_options.apply_profile("desktop-testing", "endpoint-5555", snapshot, adb_path="adb")
    assert not snapshot.exists() and fake_adb._put_count == 0


@pytest.fixture
def fake_adb(monkeypatch, tmp_path) -> FakeAdb:
    fake = FakeAdb()
    monkeypatch.setattr(dev_options, "_run_process", fake)
    monkeypatch.setattr(dev_options, "_LOCK_ROOT", tmp_path / "locks")
    return fake


def test_inspect_reports_missing_denied_and_support_is_not_proven(fake_adb: FakeAdb):
    fake_adb.settings.pop(("system", 10, "pointer_location"))
    fake_adb.exit_zero_denied_key = "adb_wifi_enabled"

    report = dev_options.inspect_device("endpoint-5555", adb_path=Path("adb"))
    by_name = {item["name"]: item for item in report["settings"]}

    assert by_name["pointer_location"]["status"] == "missing"
    assert by_name["wireless_debugging"]["status"] == "denied"
    assert all(item["support"] == "not_proven" for item in report["settings"])
    assert all(call[1:3] == ["-s", "endpoint-5555"] for call in fake_adb.calls)


@pytest.mark.parametrize("original,expected", [("0", "2"), ("1", "3"), ("4", "6"), ("8", "10"), ("2", "2"), ("7", "7"), ("15", "15"), (None, "2")])
def test_stay_awake_adds_usb_preserves_other_power_bits_and_restores(fake_adb, tmp_path, original, expected):
    key = ("global", None, "stay_on_while_plugged_in")
    if original is not None:
        fake_adb.settings[key] = original
    path = tmp_path / "awake.json"
    report = dev_options.apply_profile("stay-awake", "endpoint-5555", path, adb_path="adb")
    assert fake_adb.settings[key] == expected
    saved = json.loads(path.read_text())
    assert saved["settings"][0]["applied_value"] == expected
    if original == expected:
        assert report["unchanged"] == ["stay_awake"] and fake_adb._put_count == 0
    assert dev_options.restore_snapshot("endpoint-5555", path, adb_path="adb")["ok"]
    assert fake_adb.settings.get(key) == original


@pytest.mark.parametrize("original", ["16", "-1", "2.5", "1e3"])
def test_unknown_stay_awake_mask_refuses_without_snapshot_or_write(fake_adb, tmp_path, original):
    fake_adb.settings[("global", None, "stay_on_while_plugged_in")] = original
    path = tmp_path / "awake.json"
    with pytest.raises(DevOptionsError):
        dev_options.apply_profile("stay-awake", "endpoint-5555", path, adb_path="adb")
    assert fake_adb._put_count == 0 and not path.exists()


def test_stay_awake_recovery_conflict_and_tampered_mask_are_not_overwritten(fake_adb, tmp_path):
    key = ("global", None, "stay_on_while_plugged_in")
    fake_adb.settings[key] = "1"
    path = tmp_path / "awake.json"
    dev_options.apply_profile("stay-awake", "endpoint-5555", path, adb_path="adb")
    fake_adb.settings[key] = "7"
    report = dev_options.restore_snapshot("endpoint-5555", path, adb_path="adb")
    assert report["ok"] is False and report["conflicts"]
    assert fake_adb.settings[key] == "7"
    saved = json.loads(path.read_text()); saved["settings"][0]["applied_value"] = "15"
    path.write_text(json.dumps(saved))
    before = len(fake_adb.calls)
    with pytest.raises(DevOptionsError, match="allowlisted"):
        dev_options.restore_snapshot("endpoint-5555", path, adb_path="adb")
    assert len(fake_adb.calls) == before


def test_inspect_json_is_plain_machine_output(fake_adb: FakeAdb, monkeypatch):
    monkeypatch.setattr(dev_options, "resolve_adb", lambda: Path("adb"))
    result = CliRunner().invoke(dev_options_app, ["inspect", "-s", "endpoint-5555", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["command"] == "dev-options inspect"
    assert "\x1b[" not in result.stdout


def test_apply_persists_originals_before_first_write(fake_adb: FakeAdb, tmp_path):
    snapshot = tmp_path / "fast-ui.json"

    def assert_recovery_ready() -> None:
        saved = json.loads(snapshot.read_text(encoding="utf-8"))
        assert saved["status"] == "applying"
        assert [item["original"]["value"] for item in saved["settings"]] == [
            "1.0",
            "0.5",
            "2",
        ]
        assert saved["device"]["ro_serialno"] == "physical-123"
        assert saved["device"]["android_user"] == 10

    fake_adb.before_first_put = assert_recovery_ready
    report = dev_options.apply_profile(
        "fast-ui", "endpoint-5555", snapshot, adb_path=Path("adb")
    )

    assert report["ok"] is True
    assert all(
        fake_adb.settings[("global", None, key)] == "0"
        for key in (
            "window_animation_scale",
            "transition_animation_scale",
            "animator_duration_scale",
        )
    )
    saved = json.loads(snapshot.read_text(encoding="utf-8"))
    assert saved["status"] == "applied"
    assert all(item["applied"] for item in saved["settings"])


def test_apply_refuses_to_overwrite_existing_snapshot(fake_adb: FakeAdb, tmp_path):
    snapshot = tmp_path / "existing.json"
    snapshot.write_text("keep me", encoding="utf-8")

    with pytest.raises(DevOptionsError, match="refusing to overwrite") as caught:
        dev_options.apply_profile("touches", "endpoint-5555", snapshot, adb_path=Path("adb"))

    assert caught.value.kind == "snapshot_exists"
    assert snapshot.read_text(encoding="utf-8") == "keep me"
    assert not any("put" in call for call in fake_adb.calls)


def test_partial_apply_rolls_back_and_retains_error_snapshot(fake_adb: FakeAdb, tmp_path):
    snapshot = tmp_path / "partial.json"
    fake_adb.fail_put_key = "transition_animation_scale"

    with pytest.raises(DevOptionsError) as caught:
        dev_options.apply_profile("fast-ui", "endpoint-5555", snapshot, adb_path=Path("adb"))

    assert caught.value.kind == "apply_failed"
    assert fake_adb.settings[("global", None, "window_animation_scale")] == "1.0"
    saved = json.loads(snapshot.read_text(encoding="utf-8"))
    assert saved["status"] == "partial_failure"
    assert saved["errors"]
    assert saved["settings"][0]["state"] == "rolled_back"


def test_restore_checks_identity_before_any_write(fake_adb: FakeAdb, tmp_path):
    snapshot = tmp_path / "touches.json"
    dev_options.apply_profile("touches", "endpoint-5555", snapshot, adb_path=Path("adb"))
    fake_adb.calls.clear()
    fake_adb.fingerprint = "different/build/fingerprint"

    with pytest.raises(DevOptionsError) as caught:
        dev_options.restore_snapshot("endpoint-5555", snapshot, adb_path=Path("adb"))

    assert caught.value.kind == "identity_mismatch"
    assert not any("put" in call or "delete" in call for call in fake_adb.calls)


def test_restore_deletes_originally_absent_key_and_checks_readback(fake_adb: FakeAdb, tmp_path):
    snapshot = tmp_path / "touches-absent.json"
    fake_adb.settings.pop(("system", 10, "show_touches"))
    dev_options.apply_profile("touches", "endpoint-5555", snapshot, adb_path=Path("adb"))

    report = dev_options.restore_snapshot(
        "endpoint-5555", snapshot, adb_path=Path("adb")
    )

    assert report["ok"] is True
    assert ("system", 10, "show_touches") not in fake_adb.settings
    saved = json.loads(snapshot.read_text(encoding="utf-8"))
    assert saved["status"] == "restored"
    assert saved["settings"][0]["state"] == "restored"


def test_restore_conflict_does_not_clobber_external_change(fake_adb: FakeAdb, tmp_path):
    snapshot = tmp_path / "touches-conflict.json"
    dev_options.apply_profile("touches", "endpoint-5555", snapshot, adb_path=Path("adb"))
    fake_adb.settings[("system", 10, "show_touches")] = "2"
    call_count = len(fake_adb.calls)

    report = dev_options.restore_snapshot(
        "endpoint-5555", snapshot, adb_path=Path("adb")
    )

    assert report["ok"] is False
    assert report["conflicts"]
    assert fake_adb.settings[("system", 10, "show_touches")] == "2"
    later_calls = fake_adb.calls[call_count:]
    assert not any("put" in call or "delete" in call for call in later_calls)


def test_malformed_snapshot_with_untrusted_key_never_reaches_adb(fake_adb: FakeAdb, tmp_path):
    snapshot = tmp_path / "malicious.json"
    dev_options.apply_profile("touches", "endpoint-5555", snapshot, adb_path=Path("adb"))
    payload = json.loads(snapshot.read_text(encoding="utf-8"))
    payload["settings"][0]["key"] = "evil; setprop persist.bad 1"
    snapshot.write_text(json.dumps(payload), encoding="utf-8")
    fake_adb.calls.clear()

    with pytest.raises(DevOptionsError) as caught:
        dev_options.restore_snapshot("endpoint-5555", snapshot, adb_path=Path("adb"))

    assert caught.value.kind == "untrusted_setting"
    assert fake_adb.calls == []


@pytest.mark.parametrize("serial", ["", "   ", "-d", " device", "x\nother"])
def test_serial_must_be_explicit_nonblank_and_safe(serial: str):
    with pytest.raises(DevOptionsError):
        dev_options.inspect_device(serial, adb_path=Path("adb"))


def test_timeout_is_classified(monkeypatch):
    def timeout(args, timeout):
        raise subprocess.TimeoutExpired(args, timeout)

    monkeypatch.setattr(dev_options, "_run_process", timeout)
    with pytest.raises(DevOptionsError) as caught:
        dev_options.inspect_device("endpoint-5555", adb_path=Path("adb"))
    assert caught.value.kind == "timeout"


def test_device_lock_refuses_overlapping_transaction(monkeypatch, tmp_path):
    monkeypatch.setattr(dev_options, "_LOCK_ROOT", tmp_path / "locks")
    identity = dev_options.DeviceIdentity("physical-123", "build/fingerprint", 10)

    with dev_options._device_lock(identity):
        with pytest.raises(DevOptionsError) as caught:
            with dev_options._device_lock(identity):
                pass

    assert caught.value.kind == "concurrent_operation"


def test_mdns_serial_remains_one_exact_target_argument(monkeypatch):
    serial = "adb-Example Phone (2)._adb-tls-connect._tcp"
    calls = []

    def run(args, timeout):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "1\n", "")

    monkeypatch.setattr(dev_options, "_run_process", run)
    assert dev_options._run_adb(Path("adb"), serial, ["shell", "getprop", "ro.serialno"]) == "1"
    assert calls[0][1:3] == ["-s", serial]


def test_device_global_lock_covers_other_android_users(monkeypatch, tmp_path):
    monkeypatch.setattr(dev_options, "_LOCK_ROOT", tmp_path / "locks")
    first = dev_options.DeviceIdentity("same-phone", "build", 0)
    second = dev_options.DeviceIdentity("same-phone", "build", 10)
    with dev_options._device_lock(first):
        with pytest.raises(DevOptionsError) as caught:
            with dev_options._device_lock(second):
                pytest.fail("Global settings transactions must not overlap across users")
    assert caught.value.kind == "concurrent_operation"


def test_unsafe_daemon_stops_profile_before_device_calls_or_snapshot(monkeypatch, tmp_path):
    def blocked(*args, **kwargs):
        raise dev_options.UnsafeAdbServerError("unverified")

    monkeypatch.setattr(dev_options, "preflight_adb_server", blocked)
    monkeypatch.setattr(dev_options, "_run_process", lambda *a, **k: pytest.fail("Device command reached"))
    snapshot = tmp_path / "must-not-exist.json"
    with pytest.raises(DevOptionsError) as caught:
        dev_options.apply_profile("touches", "phone", snapshot, adb_path=Path("adb"))
    assert caught.value.kind == "unsafe_adb_server"
    assert not snapshot.exists()


def _fast_ui_key(name: str) -> tuple[str, None, str]:
    return ("global", None, name)


def _change_transition_during_first_restore_write(fake_adb: FakeAdb, value: str = "0.25"):
    def external_change(key: str) -> None:
        # Another actor changes a later setting after preflight, while the
        # first setting is being restored.
        if key == "window_animation_scale":
            fake_adb.settings[_fast_ui_key("transition_animation_scale")] = value

    return external_change


def _writes_since(fake_adb: FakeAdb, start: int) -> list[list[str]]:
    return [call for call in fake_adb.calls[start:] if "put" in call or "delete" in call]


def test_restore_rechecks_each_setting_before_its_write(fake_adb: FakeAdb, tmp_path):
    snapshot = tmp_path / "fast-ui-race.json"
    dev_options.apply_profile("fast-ui", "endpoint-5555", snapshot, adb_path=Path("adb"))
    fake_adb.before_put = _change_transition_during_first_restore_write(fake_adb)

    report = dev_options.restore_snapshot("endpoint-5555", snapshot, adb_path=Path("adb"))

    assert report["ok"] is False
    assert report["status"] == "restore_partial"
    assert report["restored"] == ["window_animation_scale", "animator_duration_scale"]
    assert report["left_untouched"] == ["transition_animation_scale"]
    assert report["conflicts"] == [
        "transition_animation_scale: changed during restore; left untouched"
    ]
    assert fake_adb.settings[_fast_ui_key("window_animation_scale")] == "1.0"
    assert fake_adb.settings[_fast_ui_key("transition_animation_scale")] == "0.25"
    assert fake_adb.settings[_fast_ui_key("animator_duration_scale")] == "2"
    saved = json.loads(snapshot.read_text(encoding="utf-8"))
    assert saved["status"] == "restore_partial"
    assert saved["conflicts"] == report["conflicts"]
    states = {item["name"]: item["state"] for item in saved["settings"]}
    assert states == {
        "window_animation_scale": "restored",
        "transition_animation_scale": "conflict",
        "animator_duration_scale": "restored",
    }


@pytest.mark.parametrize("resolved_value,expected_state", [("0", "restored"), ("0.5", "already_restored")])
def test_second_restore_finishes_after_mid_restore_conflict_is_resolved(
    fake_adb: FakeAdb, tmp_path, resolved_value, expected_state
):
    snapshot = tmp_path / "fast-ui-race.json"
    dev_options.apply_profile("fast-ui", "endpoint-5555", snapshot, adb_path=Path("adb"))
    transition = _fast_ui_key("transition_animation_scale")
    fake_adb.before_put = _change_transition_during_first_restore_write(fake_adb)
    assert dev_options.restore_snapshot("endpoint-5555", snapshot, adb_path=Path("adb"))["ok"] is False
    fake_adb.before_put = None

    # The user resolves the conflict, back to the applied value or to the original.
    fake_adb.settings[transition] = resolved_value
    start = len(fake_adb.calls)
    report = dev_options.restore_snapshot("endpoint-5555", snapshot, adb_path=Path("adb"))

    assert report["ok"] is True
    assert report["status"] == "restored"
    assert report["restored"] == (["transition_animation_scale"] if resolved_value == "0" else [])
    assert fake_adb.settings[transition] == "0.5"
    assert all("transition_animation_scale" in call for call in _writes_since(fake_adb, start))
    saved = json.loads(snapshot.read_text(encoding="utf-8"))
    assert saved["status"] == "restored"
    assert saved["conflicts"] == []
    states = {item["name"]: item["state"] for item in saved["settings"]}
    assert states["transition_animation_scale"] == expected_state
    assert states["window_animation_scale"] == "restored"


def test_restore_write_failure_leaves_entries_skipped_as_changed(fake_adb: FakeAdb, tmp_path):
    snapshot = tmp_path / "fast-ui-race-fail.json"
    dev_options.apply_profile("fast-ui", "endpoint-5555", snapshot, adb_path=Path("adb"))
    fake_adb.before_put = _change_transition_during_first_restore_write(fake_adb)
    fake_adb.fail_put_key = "animator_duration_scale"

    with pytest.raises(DevOptionsError) as caught:
        dev_options.restore_snapshot("endpoint-5555", snapshot, adb_path=Path("adb"))

    assert caught.value.kind == "restore_failed"
    # The restored entry returns to the applied profile; the skipped one is left alone.
    assert fake_adb.settings[_fast_ui_key("window_animation_scale")] == "0"
    assert fake_adb.settings[_fast_ui_key("transition_animation_scale")] == "0.25"
    saved = json.loads(snapshot.read_text(encoding="utf-8"))
    assert saved["status"] == "restore_failed"
    assert "transition_animation_scale: changed during restore; left untouched" in saved["conflicts"]
    states = {item["name"]: item["state"] for item in saved["settings"]}
    assert states["window_animation_scale"] == "applied"
    assert states["transition_animation_scale"] == "conflict"


def _uncertain_touches_snapshot(tmp_path) -> Path:
    """Simulate a host crash after the settings write but before its acknowledgement."""
    snapshot = tmp_path / "touches-uncertain.json"
    dev_options.apply_profile("touches", "endpoint-5555", snapshot, adb_path=Path("adb"))
    saved = json.loads(snapshot.read_text(encoding="utf-8"))
    saved["status"] = "applying"
    saved["settings"][0].update(attempted=True, applied=False, state="pending")
    snapshot.write_text(json.dumps(saved), encoding="utf-8")
    return snapshot


def test_uncertain_entry_holding_applied_value_is_refused_by_default(fake_adb: FakeAdb, tmp_path):
    snapshot = _uncertain_touches_snapshot(tmp_path)
    key = ("system", 10, "show_touches")
    assert fake_adb.settings[key] == "1"
    start = len(fake_adb.calls)

    report = dev_options.restore_snapshot("endpoint-5555", snapshot, adb_path=Path("adb"))

    assert report["ok"] is False and report["status"] == "restore_conflict"
    assert len(report["conflicts"]) == 1
    message = report["conflicts"][0]
    assert "outcome is uncertain" in message and "--accept-uncertain" in message
    assert "equals the value ADAPT intended to write" in message
    assert fake_adb.settings[key] == "1"
    assert _writes_since(fake_adb, start) == []
    saved = json.loads(snapshot.read_text(encoding="utf-8"))
    assert saved["settings"][0]["state"] == "uncertain"


def test_uncertain_entry_holding_applied_value_restores_when_accepted(fake_adb: FakeAdb, tmp_path):
    snapshot = _uncertain_touches_snapshot(tmp_path)
    key = ("system", 10, "show_touches")

    report = dev_options.restore_snapshot(
        "endpoint-5555", snapshot, adb_path=Path("adb"), accept_uncertain=True
    )

    assert report["ok"] is True and report["status"] == "restored"
    assert report["restored"] == ["show_touches"]
    assert report["accepted_uncertain"] == ["show_touches"]
    assert fake_adb.settings[key] == "0"
    saved = json.loads(snapshot.read_text(encoding="utf-8"))
    assert saved["status"] == "restored"
    entry = saved["settings"][0]
    assert entry["state"] == "restored_uncertain"
    assert entry["attempted"] is False and entry["applied"] is False


@pytest.mark.parametrize("accept_uncertain", [False, True])
def test_uncertain_entry_holding_third_value_is_always_refused(
    fake_adb: FakeAdb, tmp_path, accept_uncertain
):
    snapshot = _uncertain_touches_snapshot(tmp_path)
    key = ("system", 10, "show_touches")
    fake_adb.settings[key] = "2"
    start = len(fake_adb.calls)

    report = dev_options.restore_snapshot(
        "endpoint-5555", snapshot, adb_path=Path("adb"), accept_uncertain=accept_uncertain
    )

    assert report["ok"] is False and report["status"] == "restore_conflict"
    assert "outcome is uncertain" in report["conflicts"][0]
    if accept_uncertain:
        assert "even with --accept-uncertain" in report["conflicts"][0]
    assert fake_adb.settings[key] == "2"
    assert _writes_since(fake_adb, start) == []
    saved = json.loads(snapshot.read_text(encoding="utf-8"))
    assert saved["settings"][0]["state"] == "uncertain"


@pytest.mark.parametrize("accept_uncertain", [False, True])
def test_uncertain_entry_holding_original_value_is_already_restored(
    fake_adb: FakeAdb, tmp_path, accept_uncertain
):
    snapshot = _uncertain_touches_snapshot(tmp_path)
    key = ("system", 10, "show_touches")
    fake_adb.settings[key] = "0"
    start = len(fake_adb.calls)

    report = dev_options.restore_snapshot(
        "endpoint-5555", snapshot, adb_path=Path("adb"), accept_uncertain=accept_uncertain
    )

    assert report["ok"] is True and report["status"] == "restored"
    assert report["restored"] == [] and report["accepted_uncertain"] == []
    assert fake_adb.settings[key] == "0"
    assert _writes_since(fake_adb, start) == []
    saved = json.loads(snapshot.read_text(encoding="utf-8"))
    assert saved["settings"][0]["state"] == "already_restored"
