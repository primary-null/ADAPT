"""Doctor branches: pinned remediation text and failure IDs (failure guidance Phase 3).

The remediation strings below were captured from `run_doctor_checks` before its
text moved into the failure-guide registry, so any drift in the JSON
`remediation` field fails here. Offline only: injected DoctorEnv fakes and
monkeypatched probes; no adb, device or network.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path, PurePosixPath

import pytest

import adapt_adb.doctor as doctor_mod
from adapt_adb import failure_guide as fg
from adapt_adb.adb import MANAGED_PATH_MARKER, AdbProbeError, managed_path_export_line
from adapt_adb.doctor import (
    CHECK_IDS,
    DebianUsbState,
    DoctorEnv,
    PnpDeviceRecord,
    Server5037State,
    run_doctor_checks,
)

V35 = "Android Debug Bridge version 1.0.41\nVersion 35.0.2-12147458\n"
V36 = "Android Debug Bridge version 1.0.41\nVersion 36.0.0-13206524\n"
V33 = "Android Debug Bridge version 1.0.41\nVersion 33.0.3-8952118\n"
V29_DEBIAN = "Android Debug Bridge version 1.0.41\nVersion 29.0.6-debian\n"
V35_DISTRO = "Android Debug Bridge version 1.0.41\nVersion 35.0.2-debian\n"

# The adb on PATH and the Windows user-scope folder (fixed fake paths; nothing is read from disk).
WIN_HOME = Path("C:/Users/dev")
WIN_PT = WIN_HOME / "AppData" / "Local" / "Android" / "platform-tools"
WIN_ADB = WIN_PT / "adb.exe"
WIN_OTHER = Path("C:/tools/adb.exe")
WIN_SECOND = Path("C:/other/adb.exe")
LNX_HOME = Path("/home/dev")
LNX_PT = LNX_HOME / ".local" / "share" / "android" / "platform-tools"
LNX_ADB = LNX_PT / "adb"
DISTRO_ADB = Path("/usr/bin/adb")
RULES = PurePosixPath("/opt/adapt/data/51-android.rules")

INSTALL = "Run `adapt setup` to install user-scope Google platform-tools."
INSTALL_34 = "Run `adapt setup` to install Google platform-tools >= 34.0.0."
INSTALL_OFFICIAL_34 = "Run `adapt setup` to install official Google platform-tools >= 34.0.0."
INSTALL_OFFICIAL = "Run `adapt setup` to install official Google platform-tools."
INSTALL_PATH = "Run `adapt setup` to install platform-tools and configure user PATH."
OFF_PATH = (
    "Add that adb's folder to PATH so other tools find it too, or run `adapt setup` to put "
    "ADAPT's user-scope platform-tools on PATH (it downloads only when no healthy copy is installed)."
)
FORCE = "Consider running `adapt setup --force` to use verified Google platform-tools."
DEVICE_BLOCKER = (
    "Unlock the Android device screen, toggle USB debugging in Developer Options, "
    "and accept the 'Allow USB debugging' RSA prompt (or fix udev/plugdev permissions on Linux)."
)
RULE_LINE = (
    'SUBSYSTEM=="usb", ATTR{bInterfaceClass}=="ff", ATTR{bInterfaceSubClass}=="42", '
    'ATTR{bInterfaceProtocol}=="01", TAG+="uaccess", GROUP="plugdev", MODE="0660"'
)
RELOAD = "run `sudo udevadm control --reload-rules && sudo udevadm trigger`"
COPY = f"`sudo cp {RULES.as_posix()} /etc/udev/rules.d/51-android.rules`"
CREATE = f"create `/etc/udev/rules.d/51-android.rules` with `{RULE_LINE}`"


def udev_debian(source: str) -> str:
    return (f"Install udev rules (`sudo apt install android-sdk-platform-tools-common` or {source}), "
            f"{RELOAD}, and add your user to `plugdev` (`sudo usermod -aG plugdev $USER`), then log out and back in.")


def udev_linux(source: str) -> str:
    return (f"Install Android udev rules (your distribution's own Android udev rules package, or {source}), "
            f"{RELOAD}. For SSH or headless sessions, also add your user to the group the rules grant; "
            "ADAPT's file uses `plugdev`, but group and package names differ between distributions, "
            "so use your distribution's equivalent. Then log out and back in.")


def win_env(**changes) -> DoctorEnv:
    env = DoctorEnv(
        host_os="windows", arch="AMD64", uid_is_root=False, elevated=False, home_dir=WIN_HOME,
        environ={"LOCALAPPDATA": str(WIN_HOME / "AppData" / "Local"), "PATH": str(WIN_PT)},
        which_adb=WIN_ADB, which_set=True, path_executables=[WIN_ADB],
        existing_paths={WIN_ADB, WIN_PT}, source_properties={WIN_PT / "source.properties": "Pkg.Revision=35.0.2\n"},
        version_outputs={WIN_ADB: V35}, server_state=Server5037State(listening=False),
        windows_pnp_devices=[], devices_l_output="List of devices attached\n\n",
    )
    return replace(env, **changes)


def linux_env(host_os: str = "debian", **changes) -> DoctorEnv:
    env = DoctorEnv(
        host_os=host_os, arch="x86_64", uid_is_root=False, home_dir=LNX_HOME,
        environ={"PATH": str(LNX_PT)}, which_adb=LNX_ADB, which_set=True, path_executables=[LNX_ADB],
        existing_paths={LNX_ADB, LNX_PT}, source_properties={LNX_PT / "source.properties": "Pkg.Revision=35.0.2\n"},
        dpkg_owners={LNX_ADB: None}, version_outputs={LNX_ADB: V35}, server_state=Server5037State(listening=False),
        debian_usb_state=DebianUsbState(), devices_l_output="List of devices attached\n\n",
    )
    return replace(env, **changes)


def _no_adb(env: DoctorEnv) -> DoctorEnv:
    return replace(env, which_adb=None, path_executables=[], existing_paths=set(), version_outputs={})


def _off_path(version: str) -> DoctorEnv:
    return replace(_no_adb(win_env()), existing_paths={WIN_ADB}, version_outputs={WIN_ADB: version},
                   command_adb=WIN_ADB, command_adb_source="fallback_location")


def _registered_managed() -> DoctorEnv:
    # `adapt setup` registered LNX_PT in ~/.profile; this shell's PATH has only the distribution adb.
    return linux_env(environ={"PATH": "/usr/bin"}, which_adb=DISTRO_ADB, path_executables=[DISTRO_ADB],
                     existing_paths={DISTRO_ADB, LNX_ADB, LNX_PT}, dpkg_owners={DISTRO_ADB: "adb", LNX_ADB: None},
                     version_outputs={DISTRO_ADB: V35_DISTRO, LNX_ADB: V35},
                     debian_profile_texts={LNX_HOME / ".profile": f"\n{MANAGED_PATH_MARKER}\n{managed_path_export_line(LNX_PT)}\n"})


def _listening(**fields) -> Server5037State:
    base = dict(listening=True, pid=4242, owner="dev", owner_matches_current_user=True,
                server_version_raw=V35)
    base.update(fields)
    return Server5037State(**base)


def _devices(*states: str) -> str:
    return "List of devices attached\n" + "".join(f"SERIAL{i}\t{s}\n" for i, s in enumerate(states)) + "\n"


# name -> (env factory, {check_id: (status, failure_id, remediation)}). Checks not listed pass or skip
# with no remediation and no failure ID. Remediation strings are the pre-move doctor text.
SCENARIOS: dict[str, tuple] = {
    "windows_all_pass": (lambda: win_env(), {}),
    "endpoint_override": (lambda: win_env(environ={"LOCALAPPDATA": str(WIN_HOME / "AppData" / "Local"),
                                                    "PATH": str(WIN_PT), "ANDROID_ADB_SERVER_PORT": "5038"}),
                          {"adb_server_5037": ("fail", "adb.server.endpoint_unverified",
                                               "Use the default local ADB server endpoint on port 5037.")}),
    "macos_unsupported": (lambda: linux_env("macos", server_state=Server5037State(probe_error="no /proc")),
                          {"adb_server_5037": ("fail", "host.unsupported_platform", "Use ADAPT on a Windows or Linux computer.")}),
    "listener_probe_failed": (lambda: win_env(server_state=Server5037State(probe_error="netstat failed")),
                              {"adb_server_5037": ("fail", "adb.server.probe_failed",
                                                   "Restore read access to local listener information, then recheck.")}),
    "foreign_owner": (lambda: win_env(server_state=_listening(owner="other", owner_matches_current_user=False)),
                      {"adb_server_5037": ("fail", "adb.server.foreign_owner",
                                           "Stop the root/foreign-owned ADB daemon from the account that started it, "
                                           "remove root-owned /tmp/5037 if stale, and start adb as your unprivileged user.")}),
    "root_tmp_5037": (lambda: linux_env(server_state=Server5037State(tmp_5037_exists=True, tmp_5037_owner_is_root=True)),
                      {"adb_server_5037": ("fail", "adb.server.foreign_owner",
                                           "Stop the root/foreign-owned ADB daemon from the account that started it, "
                                           "remove root-owned /tmp/5037 if stale, and start adb as your unprivileged user.")}),
    "version_mismatch": (lambda: win_env(server_state=_listening(server_version_raw=V36)),
                         {"adb_server_5037": ("fail", "adb.server.version_mismatch",
                                              "Ensure all tools use the same user-scope Google platform-tools binary "
                                              "before restarting your session.")}),
    "server_unverified": (lambda: win_env(server_state=_listening(owner=None, server_version_raw=None)),
                          {"adb_server_5037": ("fail", "adb.server.unverified",
                                               "Verify the existing daemon owner and exact platform-tools version before using it.")}),
    "adbkey_root_owned": (lambda: linux_env(server_state=Server5037State(adbkey_exists=True, adbkey_owned_by_root=True)),
                          {"adb_server_5037": ("warn", "adb.adbkey_root_owned",
                                               "Fix ownership with: sudo chown $USER:$USER ~/.android/adbkey*")}),
    "override_invalid_adapt_adb": (lambda: win_env(environ={"LOCALAPPDATA": str(WIN_HOME / "AppData" / "Local"),
                                                           "PATH": str(WIN_PT), "ADAPT_ADB": "C:/missing/adb.exe"}),
                                   {"adb_path": ("warn", "adb.override_invalid", "Correct or unset ADAPT_ADB.")}),
    "override_invalid_adb_path": (lambda: win_env(environ={"LOCALAPPDATA": str(WIN_HOME / "AppData" / "Local"),
                                                          "PATH": str(WIN_PT), "ADB_PATH": "C:/missing/adb.exe"}),
                                  {"adb_path": ("warn", "adb.override_invalid", "Correct or unset ADB_PATH.")}),
    "off_path_usable": (lambda: _off_path(V35),
                        {"adb_path": ("warn", "adb.off_path", OFF_PATH),
                         "path_config": ("warn", "adb.off_path", OFF_PATH)}),
    "registered_managed_not_on_path": (_registered_managed,
                                       {"path_config": ("warn", "adb.managed_not_on_path",
                                                        "ADAPT already uses the registered copy. For plain adb in this kind "
                                                        f"of shell to match it, add {LNX_PT} to PATH in a startup file that "
                                                        "shell reads.")}),
    "off_path_too_old": (lambda: _off_path(V33),
                         {"adb_path": ("fail", "adb.too_old", INSTALL),
                          "adb_version": ("fail", "adb.too_old", INSTALL_OFFICIAL_34),
                          "adb_origin": ("fail", "adb.origin_untrusted", INSTALL_OFFICIAL),
                          "path_config": ("fail", "adb.too_old", INSTALL_PATH)}),
    "no_adb": (lambda: _no_adb(win_env()),
               {"adb_path": ("fail", "adb.not_found", INSTALL),
                "adb_version": ("fail", "adb.not_found", INSTALL_34),
                "adb_origin": ("fail", "adb.not_found", INSTALL_OFFICIAL),
                "path_config": ("fail", "adb.not_found", INSTALL_PATH)}),
    "path_adb_too_old": (lambda: win_env(version_outputs={WIN_ADB: V33}),
                         {"adb_version": ("fail", "adb.too_old", INSTALL_OFFICIAL_34),
                          "adb_origin": ("fail", "adb.origin_untrusted", INSTALL_OFFICIAL),
                          "path_config": ("fail", "adb.path_first_outdated",
                                          "Prepend user-scope Google platform-tools to PATH via `adapt setup`.")}),
    "distro_adb_meets_minimum": (lambda: linux_env(which_adb=DISTRO_ADB, path_executables=[DISTRO_ADB],
                                                   existing_paths={DISTRO_ADB}, version_outputs={DISTRO_ADB: V35_DISTRO}),
                                 {"adb_origin": ("warn", "adb.origin_distribution", FORCE)}),
    "distro_adb_too_old": (lambda: linux_env(which_adb=DISTRO_ADB, path_executables=[DISTRO_ADB],
                                             existing_paths={DISTRO_ADB}, version_outputs={DISTRO_ADB: V29_DEBIAN}),
                           {"adb_version": ("fail", "adb.too_old", INSTALL_OFFICIAL_34),
                            "adb_origin": ("fail", "adb.too_old",
                                           "Run `adapt setup` to unpack official Google platform-tools into "
                                           "~/.local/share/android/platform-tools and prepend it to PATH."),
                            "path_config": ("fail", "adb.path_first_outdated",
                                            "Prepend user-scope Google platform-tools to PATH via `adapt setup`.")}),
    "distro_adb_too_old_other_linux": (lambda: linux_env("linux", which_adb=DISTRO_ADB, path_executables=[DISTRO_ADB],
                                                         existing_paths={DISTRO_ADB}, version_outputs={DISTRO_ADB: V29_DEBIAN}),
                                       {"adb_version": ("fail", "adb.too_old", INSTALL_OFFICIAL_34),
                                        "adb_origin": ("fail", "adb.too_old",
                                                       "Run `adapt setup` to unpack official Google platform-tools into "
                                                       "~/.local/share/android/platform-tools and prepend it to PATH."),
                                        "path_config": ("fail", "adb.path_first_outdated",
                                                        "Prepend user-scope Google platform-tools to PATH via `adapt setup`.")}),
    "unknown_origin": (lambda: win_env(which_adb=WIN_OTHER, path_executables=[WIN_OTHER], existing_paths={WIN_OTHER},
                                       source_properties={}, version_outputs={WIN_OTHER: V35}),
                       {"adb_origin": ("warn", "adb.origin_unverified", FORCE)}),
    "path_shadowed": (lambda: win_env(path_executables=[WIN_ADB, WIN_SECOND], existing_paths={WIN_ADB, WIN_PT, WIN_SECOND}),
                      {"path_config": ("warn", "adb.path_shadowed", None)}),
    "android_home_incomplete": (lambda: win_env(environ={"LOCALAPPDATA": str(WIN_HOME / "AppData" / "Local"),
                                                        "PATH": str(WIN_PT), "ANDROID_HOME": "C:/Sdk"}),
                                {"android_home": ("warn", "host.android_home_incomplete",
                                                  "Verify ANDROID_HOME for Android Studio/Gradle, or use `adapt setup` for user-scope adb.")}),
    "pnp_probe_unavailable": (lambda: win_env(windows_pnp_devices=None),
                              {"windows_pnp": ("warn", "host.usb_driver_probe_failed",
                                               "Verify PowerShell Get-PnpDevice or pnputil is available, or inspect "
                                               "Device Manager for Android/USB devices with driver problem codes.")}),
    "pnp_android_problem": (lambda: win_env(windows_pnp_devices=[PnpDeviceRecord(
                                "USB\\VID_18D1&PID_4EE7\\SERIAL", "Pixel", "AndroidUsbDeviceClass", "Error", 28)]),
                            {"windows_pnp": ("fail", "host.usb_driver_problem",
                                             "Install the official Google USB Driver (https://developer.android.com/studio/run/win-usb) "
                                             "via `pnputil /add-driver android_winusb.inf /install` or your OEM USB composite driver "
                                             "(https://developer.android.com/studio/run/oem-usb).")}),
    "pnp_other_problem": (lambda: win_env(windows_pnp_devices=[PnpDeviceRecord(
                              "USB\\VID_046D&PID_0825\\X", "USB Camera", "Camera", "Error", 22)]),
                          {"windows_pnp": ("warn", "host.usb_driver_problem_other",
                                           "If your Android phone is among these devices, check it in Device Manager and install "
                                           "the official Google USB Driver (https://developer.android.com/studio/run/win-usb) "
                                           "or your OEM USB driver (https://developer.android.com/studio/run/oem-usb).")}),
    "udev_missing_debian": (lambda: linux_env(debian_usb_state=DebianUsbState(has_udev_rules=False)),
                            {"debian_usb_permissions": ("fail", "host.udev_missing", udev_debian(COPY))}),
    "udev_missing_debian_no_file": (lambda: linux_env(debian_usb_state=DebianUsbState(has_udev_rules=False)),
                                    {"debian_usb_permissions": ("fail", "host.udev_missing", udev_debian(CREATE))}),
    "udev_missing_linux": (lambda: linux_env("linux", debian_usb_state=DebianUsbState(has_udev_rules=False)),
                           {"debian_usb_permissions": ("fail", "host.udev_missing", udev_linux(COPY))}),
    "udev_headless_no_group": (lambda: linux_env(debian_usb_state=DebianUsbState(seat="headless", in_plugdev_group=False)),
                               {"debian_usb_permissions": ("fail", "host.udev_group_missing", udev_debian(COPY))}),
    "udev_no_access": (lambda: linux_env("linux", debian_usb_state=DebianUsbState(has_uaccess_tag=False, in_plugdev_group=False, seat=None)),
                       {"debian_usb_permissions": ("fail", "host.udev_no_access", udev_linux(COPY))}),
    "udev_local_seat_no_group": (lambda: linux_env(debian_usb_state=DebianUsbState(in_plugdev_group=False)),
                                 {"debian_usb_permissions": ("warn", "host.udev_group_recommended", udev_debian(COPY))}),
    "devices_enumeration_failed": (lambda: win_env(devices_l_output="RAISE"),
                                   {"devices_state": ("fail", "adb.enumeration_failed",
                                                      "Verify the ADB daemon and rerun `adapt doctor`.")}),
    "devices_unauthorized": (lambda: win_env(devices_l_output=_devices("unauthorized")),
                             {"devices_state": ("fail", "device.unauthorized", DEVICE_BLOCKER)}),
    "devices_no_permissions": (lambda: win_env(devices_l_output=_devices("no permissions (user in plugdev group)")),
                               {"devices_state": ("fail", "device.no_permissions", DEVICE_BLOCKER)}),
    "devices_mixed_blockers": (lambda: win_env(devices_l_output=_devices("unauthorized", "no permissions")),
                               {"devices_state": ("fail", "device.access_blocked", DEVICE_BLOCKER)}),
    "devices_offline": (lambda: win_env(devices_l_output=_devices("offline", "device")),
                        {"devices_state": ("warn", "device.offline",
                                           "Reconnect the USB cable or re-toggle USB debugging on the offline device.")}),
}

_NO_RULES_FILE = {"udev_missing_debian_no_file"}


def _run(name: str, monkeypatch) -> dict[str, dict]:
    factory, _ = SCENARIOS[name]
    env = factory()
    monkeypatch.setattr(doctor_mod, "find_udev_rules_file", lambda: None if name in _NO_RULES_FILE else RULES)
    monkeypatch.setattr(doctor_mod, "_probe_windows_pnp", lambda: (None, "probe unavailable"))
    if env.devices_l_output == "RAISE":
        env = replace(env, devices_l_output=None)

        def refuse(*_args, **_kwargs):
            raise AdbProbeError("listing failed")

        monkeypatch.setattr(doctor_mod, "_query_adb_devices_l", refuse)
    report = run_doctor_checks(env)
    return {check["id"]: check for check in report["checks"]}


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_remediation_text_is_pinned_per_branch(name, monkeypatch):
    checks = _run(name, monkeypatch)
    expected = SCENARIOS[name][1]
    assert set(checks) == set(CHECK_IDS)
    for check_id, check in checks.items():
        if check_id in expected:
            status, _failure_id, remediation = expected[check_id]
            assert (check["status"], check["remediation"]) == (status, remediation), (name, check_id, check["summary"])
        else:
            assert check["status"] in {"pass", "skip"}, (name, check_id, check["summary"])
            assert check["remediation"] is None, (name, check_id)


def _template_matches(template: str, text: str) -> bool:
    pattern = "".join(".+" if part.startswith("<") else re.escape(part)
                      for part in re.split(r"(<[A-Z_]+>)", template))
    return re.fullmatch(pattern, text, re.DOTALL) is not None


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_every_check_carries_its_branch_failure_id(name, monkeypatch):
    checks = _run(name, monkeypatch)
    expected = SCENARIOS[name][1]
    for check_id, check in checks.items():
        assert "failure_id" in check, (name, check_id)
        wanted = expected[check_id][1] if check_id in expected else None
        assert check["failure_id"] == wanted, (name, check_id, check["summary"])


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_remediation_comes_from_the_registry(name, monkeypatch):
    for check_id, check in _run(name, monkeypatch).items():
        failure_id = check["failure_id"]
        if failure_id is None:
            assert check["remediation"] is None, (name, check_id)
            continue
        guide = fg.GUIDES[failure_id]
        candidates = [t for t in (guide.doctor_remediation, *dict(guide.doctor_variants).values()) if t]
        if check["remediation"] is None:
            assert not candidates or check["status"] == "warn", (name, check_id)
            continue
        assert any(_template_matches(t, check["remediation"]) for t in candidates), (name, check_id)
        assert not re.search(r"<[A-Z_]+>", check["remediation"]), (name, check_id)


def test_scenarios_cover_every_failure_id_doctor_can_emit():
    source = (Path(doctor_mod.__file__)).read_text(encoding="utf-8")
    emitted = set(re.findall(r'"((?:adb|host|device)\.[a-z_.]+)"', source.split("def run_doctor_checks", 1)[1].split("def build_next_actions", 1)[0]))
    covered = {fid for _, expected in SCENARIOS.values() for (_status, fid, _text) in expected.values()}
    emitted.discard("adb.exe")  # the Windows binary name, not an ID
    assert emitted <= covered, sorted(emitted - covered)
    for failure_id in covered:
        assert failure_id in fg.GUIDES, failure_id
        assert fg.GUIDES[failure_id].doctor_check in CHECK_IDS, failure_id


def test_doctor_report_check_ids_point_at_the_check_they_explain(monkeypatch):
    # A guide's doctor_check names its main check; shared guides (adb.not_found, adb.too_old, adb.off_path)
    # cover sibling checks through doctor_variants or identical text.
    for name in SCENARIOS:
        for check_id, check in _run(name, monkeypatch).items():
            if check["failure_id"]:
                guide = fg.GUIDES[check["failure_id"]]
                assert guide.doctor_check == check_id or check_id in dict(guide.doctor_variants) or \
                    check["remediation"] == guide.doctor_remediation, (name, check_id, guide.id)
