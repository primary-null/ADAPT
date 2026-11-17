"""An offline fake Android shell for the Developer Options tests.

It is installed in place of ``dev_options._run_process``.  Every argv it receives
must match an allowlisted shape in ``adapt_adb.safety`` (the fake fails the test
otherwise), and it models the state behind each control closely enough to test
apply, readback and restore.  Output formats follow the parsers' expectations;
they are written from AOSP source reading, not captured from a device.  All
values are synthetic.
"""

from __future__ import annotations

import subprocess

from adapt_adb import safety

PKG = "com.example.app"


class FakeDevice:
    def __init__(self, *, sdk: str = "36", manufacturer: str = "ExampleMaker", brand: str = "examplebrand", user: int = 10) -> None:
        self.serial = "SERIAL1"
        self.serialno = "physical-123"
        self.fingerprint = "vendor/device/product:16/BUILD/123:user/release-keys"
        self.user = user
        self.settings: dict[tuple[str, int | None, str], str] = {}
        self.props: dict[str, str] = {
            "ro.build.version.sdk": sdk,
            "ro.product.manufacturer": manufacturer,
            "ro.product.brand": brand,
            "ro.build.type": "user",
            "ro.build.characteristics": "default",
        }
        self.density_physical = 420
        self.density_override: int | None = None
        self.battery_stopped = False
        self.doze = "INACTIVE"
        self.night = "no"
        self.dexopt: dict[str, str] = {PKG: "verify"}
        self.dexopt_after_reset = "verify"
        self.buckets: dict[str, int] = {PKG: 20}
        self.appops: dict[str, str | None] = {PKG: None}
        self.locales: dict[str, list[str]] = {PKG: []}
        self.restrict_background = False
        self.page_size = "4096"
        self.features = "feature:android.software.picture_in_picture\nfeature:android.hardware.sensor.hinge_angle\n"
        self.device_policy = "Device policy state:\n"
        self.calls: list[list[str]] = []
        self.shapes: list[str] = []
        self.fail = lambda shell: None          # shell argv -> stderr text to fail with, or None
        self.ignore: set[str] = set()           # shell verbs whose write is silently ignored (a stored value is not an effect)
        self.before_write = None                # callback(shell) before each write
        self.slow_after: int | None = None

    # -- helpers -----------------------------------------------------------
    def writes(self) -> list[list[str]]:
        return [call for call, shape in zip(self.calls, self.shapes) if safety.dev_options_shape_writes(shape)]

    def put(self, table: str, key: str, value: str, user: int | None = None) -> None:
        self.settings[(table, user, key)] = value

    def _ok(self, args, out: str = ""):
        return subprocess.CompletedProcess(args, 0, out + ("\n" if out and not out.endswith("\n") else ""), "")

    def _err(self, args, text: str):
        return subprocess.CompletedProcess(args, 1, "", text + "\n")

    # -- the shell ---------------------------------------------------------
    def __call__(self, args: list[str], timeout: int):
        assert args[1:3] == ["-s", self.serial] or args[1] == "-s", args
        shell = list(args[3:])
        shape = safety.match_dev_options_shell(shell)
        assert shape is not None, f"not an allowlisted shape: {shell}"
        self.calls.append(shell)
        self.shapes.append(shape)
        if safety.dev_options_shape_writes(shape) and self.before_write:
            self.before_write(shell)
        failure = self.fail(shell)
        if failure is not None:
            return self._err(args, failure)
        body = shell[1:]
        return self._dispatch(args, shape, body)

    def _dispatch(self, args, shape: str, body: list[str]):
        verb = body[0]
        if verb == "getprop":
            value = {"ro.serialno": self.serialno, "ro.build.fingerprint": self.fingerprint}.get(body[1], self.props.get(body[1], ""))
            return self._ok(args, value)
        if verb == "setprop":
            if shape == "setprop" and "setprop" not in self.ignore:
                self.props[body[1]] = "" if body[2] == "''" else body[2]
            return self._ok(args)
        if body[:2] == ["am", "get-current-user"] or body == ["am", "get-current-user"]:
            return self._ok(args, str(self.user))
        if verb == "settings":
            return self._settings(args, body[1:])
        if body[0] == "wm":
            return self._wm(args, body[1:])
        if body[0] == "dumpsys":
            return self._dumpsys(args, body[1:])
        if body[0] == "cmd":
            return self._cmd(args, body[1:])
        if body[0] == "am":
            return self._am(args, body[1:])
        if body[0] == "getconf":
            return self._ok(args, self.page_size)
        if body[:2] == ["pm", "list"]:
            return self._ok(args, self.features)
        raise AssertionError(body)

    def _settings(self, args, body):
        user = None
        if body[0] == "--user":
            user = int(body[1])
            body = body[2:]
        action, table, key = body[:3]
        ident = (table, user, key)
        if action == "get":
            return self._ok(args, self.settings.get(ident, "null"))
        if action == "put":
            if "settings" not in self.ignore:
                self.settings[ident] = body[3]
            return self._ok(args)
        self.settings.pop(ident, None)
        return self._ok(args, "Deleted 1 rows")

    def _wm(self, args, body):
        if body == ["density"]:
            text = f"Physical density: {self.density_physical}"
            if self.density_override is not None:
                text += f"\nOverride density: {self.density_override}"
            return self._ok(args, text)
        if "wm" not in self.ignore:
            self.density_override = None if body[1] == "reset" else int(body[1])
        return self._ok(args)

    def _dumpsys(self, args, body):
        if body == ["battery"]:
            head = "Current Battery Service state:\n"
            if self.battery_stopped:
                head += "  (UPDATES STOPPED -- use 'reset' to restart)\n"
            powered = "false" if self.battery_stopped else "true"
            return self._ok(args, head + f"  AC powered: false\n  USB powered: {powered}\n  Wireless powered: false\n  level: 80")
        if body[:2] == ["deviceidle", "get"]:
            return self._ok(args, self.doze)
        if body[0] == "deviceidle":
            if "doze" not in self.ignore:
                self.doze = "IDLE" if body[1] == "force-idle" else "ACTIVE"
            return self._ok(args, "Now forced in to deep idle mode" if body[1] == "force-idle" else "Idle mode back to normal.")
        if body[0] == "package":
            status = self.dexopt[body[1]]
            return self._ok(args, f"Packages:\n  Package [{body[1]}]\nDexopt state:\n  [{body[1]}]\n    path: /data/app/x/base.apk\n      arm64: [status={status}] [reason=install]\n")
        if body == ["device_policy"]:
            return self._ok(args, self.device_policy)
        raise AssertionError(body)

    def _cmd(self, args, body):
        if body[0] == "battery":
            if "battery" not in self.ignore:
                self.battery_stopped = body[1] == "unplug"
            return self._ok(args)
        if body[0] == "uimode":
            if len(body) == 2:
                return self._ok(args, f"Night mode: {self.night}")
            if "night" not in self.ignore:
                self.night = body[2]
            return self._ok(args, f"Night mode: {body[2]}")
        if body[0] == "package":
            if body[2] == "--reset":
                self.dexopt[body[3]] = self.dexopt_after_reset
            else:
                if "compile" not in self.ignore:
                    self.dexopt[body[5]] = body[3]
            return self._ok(args, "Success")
        if body[0] == "appops":
            if body[1] == "get":
                mode = self.appops.get(body[4])
                return self._ok(args, "No operations." if mode is None else f"RUN_ANY_IN_BACKGROUND: {mode}; time=+1d")
            if "appops" not in self.ignore:
                self.appops[body[4]] = None if body[6] == "default" else body[6]
            return self._ok(args)
        if body[0] == "locale":
            if body[1] == "get-app-locales":
                tags = ",".join(self.locales.get(body[2], []))
                return self._ok(args, f"Locales for {body[2]} for user {body[4]} are [{tags}]")
            if "locale" not in self.ignore:
                self.locales[body[2]] = body[6].split(",") if len(body) > 5 and body[5] == "--locales" else []
            return self._ok(args)
        if body[0] == "netpolicy":
            if body[1] == "get":
                return self._ok(args, f"Restrict background status: {'enabled' if self.restrict_background else 'disabled'}")
            if "netpolicy" not in self.ignore:
                self.restrict_background = body[3] == "true"
            return self._ok(args)
        if body[0] == "connectivity":
            self.settings[("global", None, "airplane_mode_on")] = "1" if body[2] == "enable" else "0"
            return self._ok(args)
        raise AssertionError(body)

    def _am(self, args, body):
        if body[0] == "set-debug-app":
            wait = "-w" in body
            self.settings[("global", None, "debug_app")] = body[-1]
            self.settings[("global", None, "wait_for_debugger")] = "1" if wait else "0"
            return self._ok(args)
        if body[0] == "clear-debug-app":
            self.settings.pop(("global", None, "debug_app"), None)
            self.settings.pop(("global", None, "wait_for_debugger"), None)
            return self._ok(args)
        if body[0] == "get-standby-bucket":
            return self._ok(args, str(self.buckets[body[3]]))
        if body[0] == "set-standby-bucket":
            numbers = {"active": 10, "working_set": 20, "frequent": 30, "rare": 40, "restricted": 45}
            self.buckets[body[3]] = numbers[body[4]]
            return self._ok(args)
        raise AssertionError(body)
