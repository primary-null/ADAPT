"""Experimental Samsung DeX PC sessions with invocation-owned recovery.

The helper is compiled locally from bundled source. Capability presence is not
permission proof; neither backend is labeled successful without Samsung state.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Any

import typer

from adapt_adb import dev_options
from adapt_adb.dex import dex_app, resolve_scrcpy, _check_scrcpy_version, _run_process, _stop_owned_process
from adapt_adb.device_state import validate_target
from adapt_adb.progress import adapting

RESOURCE = Path(__file__).parent / "resources" / "dex_bridge"
ENTRY = "org.adaptadb.dex.AdaptDexBridge"
TERMINAL = {"failed", "stopped", "cleanup_conflict"}


class PcSessionError(ValueError):
    pass


def helper_path() -> Path:
    try:
        receipt = json.loads((RESOURCE / "build.json").read_text(encoding="utf-8"))
        jar = RESOURCE / "adapt-dex-bridge.jar"
        if hashlib.sha256(jar.read_bytes()).hexdigest() != receipt["jar_sha256"] or hashlib.sha256((RESOURCE / "AdaptDexBridge.java").read_bytes()).hexdigest() != receipt["source_sha256"]:
            raise ValueError()
        return jar
    except (OSError, ValueError, KeyError) as exc:
        raise PcSessionError("Bundled DeX helper/source integrity failed; rebuild from source with scripts/build_dex_bridge.py.") from exc


def _validate_request(serial: str, method: str, size: str, dpi: int) -> None:
    validate_target(serial)
    match = re.fullmatch(r"([0-9]{3,4})x([0-9]{3,4})", size)
    if method not in {"loopback", "overlay"} or not match or not all(320 <= int(v) <= 4096 for v in match.groups()) or type(dpi) is not int or not 80 <= dpi <= 640:
        raise PcSessionError("Choose loopback/overlay, a 320–4096 pixel WIDTHxHEIGHT and DPI 80–640.")


def plan(serial: str, *, method: str = "loopback", size: str = "1920x1080", dpi: int = 160, view_only: bool = False) -> dict[str, Any]:
    _validate_request(serial, method, size, dpi)
    return {"schema_version": 1, "status": "plan_only", "serial": serial, "method": method,
            "overlay_value": f"{size}/{dpi},hdmi", "view_only": view_only,
            "device_access": False, "helper_verified": bool(helper_path()),
            "requirements": ["Observed Samsung Android 13–17 / API 33–37", "No active DeX, wireless or non-internal display", "Exact physical/build/user-bound recovery snapshot before activation", "Positive Samsung DeX enabled/dual-display evidence before scrcpy launch"],
            "effects": ["Push and run the bundled helper JAR as Android shell; no APK installation/root", "Loopback: request a local Samsung wireless display; requires vendor permission", "Overlay: change only overlay_display_devices and restore it if still invocation-owned", "Create one controllable scrcpy window; preserve existing mirrors and ADB daemon"],
            "claim_boundary": "Experimental firmware-dependent activation. API presence and an ordinary secondary display are not proof of DeX. Loopback size is firmware-negotiated; size/DPI configure the overlay backend."}


def _adb(adb: Path, serial: str, args: list[str]) -> str:
    result = subprocess.run([str(adb), "-s", serial, *args], capture_output=True, text=True, encoding="utf-8", timeout=10, check=False)
    text = result.stdout or ""
    if result.returncode or re.search(r"permission deni|securityexception|(?:^|\n)error:", (result.stderr or "") + "\n" + text, re.I):
        raise PcSessionError("A targeted DeX device operation failed or was denied; inspect the private session record.")
    return text.strip()


def _save(path: Path, state: dict[str, Any]) -> None:
    state["updated_at"] = time.time()
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex)
    try:
        with os.fdopen(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600), "wb") as stream:
            stream.write((json.dumps(state, indent=2) + "\n").encode("utf-8"))
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or path.stat().st_size > 256 * 1024:
            raise ValueError()
        state = json.loads(path.read_text(encoding="utf-8"))
        if state.get("schema_version") != 1 or not re.fullmatch(r"[a-f0-9]{32}", state.get("token", "")):
            raise ValueError()
        _validate_request(state["serial"], state["method"], state["size"], state["dpi"])
        if state["original_overlay"] not in {"null", "none"} or set(state["identity"]) != {"ro_serialno", "build_fingerprint", "android_user"}:
            raise ValueError()
        identity = state["identity"]
        if type(identity["android_user"]) is not int or identity["android_user"] < 0 or any(not isinstance(identity[k], str) or not identity[k] or len(identity[k]) > 2048 or re.search(r"[\x00-\x1f\x7f-\x9f]", identity[k]) for k in ("ro_serialno", "build_fingerprint")):
            raise ValueError()
        if type(state["view_only"]) is not bool or state["status"] not in TERMINAL | {"prepared", "starting", "running"}:
            raise ValueError()
        if state["overlay_value"] != f"{state['size']}/{state['dpi']},hdmi":
            raise ValueError()
        return state
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PcSessionError("Invalid or unreadable DeX session snapshot; no device action performed.") from exc


def _identity(state: dict[str, Any], adb: Path) -> dev_options.DeviceIdentity:
    current = dev_options._read_identity(adb, state["serial"])
    if asdict(current) != state["identity"]:
        raise PcSessionError("Physical device, Android build or current user differs from the recovery snapshot.")
    return current


def _lock(identity: dev_options.DeviceIdentity):
    # Separate from ordinary Developer Options transactions; same physical
    # device is exclusive among PC sessions/recovery, including other users.
    return dev_options._device_lock(dev_options.DeviceIdentity("adapt-dex-pc:" + identity.ro_serialno, identity.build_fingerprint, identity.android_user))


def _remote(state: dict[str, Any]) -> str:
    return "/data/local/tmp/adapt-dex-" + state["token"] + ".jar"


def _helper_args(state: dict[str, Any], mode: str) -> list[str]:
    return ["shell", "CLASSPATH=" + _remote(state), "app_process", "/", ENTRY, mode, *([state["token"]] if mode != "probe" else [])]


def _probe(adb: Path, state: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(_adb(adb, state["serial"], _helper_args(state, "probe")))
        if value.get("event") != "probe" or type(value.get("wifi_state")) is not int or type(value.get("dex_enabled")) is not int or type(value.get("dex_display_type")) is not int:
            raise ValueError()
        if not isinstance(value.get("displays"), list) or not value["displays"] or any(not isinstance(d, dict) or type(d.get("id")) is not int or type(d.get("type")) is not int for d in value["displays"]):
            raise ValueError()
        ids = [d["id"] for d in value["displays"]]
        if 0 not in ids or len(ids) != len(set(ids)) or any(d < 0 for d in ids):
            raise ValueError()
        return value
    except (ValueError, TypeError, AttributeError) as exc:
        raise PcSessionError("Samsung helper returned unrecognized state; refusing to infer a desktop.") from exc


def classify(before: dict[str, Any], after: dict[str, Any]) -> int | None:
    old = {d["id"] for d in before["displays"]}
    candidates = [d["id"] for d in after["displays"] if d["id"] not in old and d["id"] > 0
                  and d.get("type") in {2, 4} and str(d.get("unique_id", "")).startswith("overlay:")]
    if after["dex_enabled"] == 4 and after["dex_display_type"] == 102 and len(candidates) == 1:
        return candidates[0]
    return None


def _restore_overlay(adb: Path, state: dict[str, Any]) -> str:
    _identity(state, adb)
    current = _adb(adb, state["serial"], ["shell", "settings", "get", "global", "overlay_display_devices"])
    if current == state["original_overlay"]:
        return "already_original"
    if current != state["overlay_value"]:
        raise PcSessionError("Overlay changed externally; recovery left it untouched.")
    args = ["shell", "settings", "delete", "global", "overlay_display_devices"] if state["original_overlay"] == "null" else ["shell", "settings", "put", "global", "overlay_display_devices", "none"]
    _adb(adb, state["serial"], args)
    if _adb(adb, state["serial"], ["shell", "settings", "get", "global", "overlay_display_devices"]) != state["original_overlay"]:
        raise PcSessionError("Overlay recovery readback failed; retain this snapshot.")
    return "restored_verified"


def _default_snapshot() -> Path:
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) if os.name == "nt" else Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state")))
    base = root / "adapt" / "dex-sessions" / uuid.uuid4().hex
    base.mkdir(parents=True, mode=0o700)
    return base / "session.json"


def _log(path: Path):
    return os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600), "wb")


def receiver_plan() -> dict[str, Any]:
    return {"schema_version": 1, "status": "manual_receiver_setup", "windows_only": True,
            "feature": "App.WirelessDisplay.Connect~~~~0.0.1.0", "requires_administrator": True,
            "settings_uri": "ms-settings:project", "changes_performed": False,
            "steps": ["Install Microsoft's Wireless Display optional feature on the PC", "Open its receiver window and configure projection using Windows", "On the phone, select this PC in Samsung DeX's wireless receiver picker", "Run pc-probe to inspect actual Samsung mode and logical IDs", "Use pc-attach with the observed sole external display ID for a controllable scrcpy window"],
            "source": "https://support.microsoft.com/en-us/windows/hardware/display-graphics/screen-mirroring-and-projecting-to-your-pc-or-wireless-display",
            "note": "A supported receiver alternative when vendor shell activation is denied. This plan installs or starts nothing; actual device/driver pairing and desktop operation still need validation."}


def runtime_probe(serial: str, *, yes: bool = False) -> dict[str, Any]:
    validate_target(serial)
    if not yes:
        raise PcSessionError("Samsung API inspection transfers/runs the bundled helper temporarily; use --yes.")
    adb = dev_options._resolve_adb_path(None)
    identity = dev_options._read_identity(adb, serial)
    manufacturer = _adb(adb, serial, ["shell", "getprop", "ro.product.manufacturer"])
    api = _adb(adb, serial, ["shell", "getprop", "ro.build.version.sdk"])
    if manufacturer.lower() != "samsung" or not re.fullmatch(r"[0-9]{1,3}", api) or not 33 <= int(api) <= 37:
        raise PcSessionError("Samsung API inspection is limited to observed Samsung API 33–37.")
    state = {"serial": serial, "token": uuid.uuid4().hex, "identity": asdict(identity)}
    pushed = False
    try:
        _adb(adb, serial, ["push", str(helper_path()), _remote(state)]); pushed = True
        result = _probe(adb, state)
    finally:
        if pushed:
            _identity(state, adb)
            _adb(adb, serial, ["shell", "rm", "-f", _remote(state)])
    return {"schema_version": 1, "status": "observed", "serial": serial, "api": int(api),
            "identity_binding": hashlib.sha256(json.dumps(asdict(identity), sort_keys=True).encode("utf-8")).hexdigest(),
            "observations": result, "temporary_helper_removed": True, "display_changes": False,
            "note": "Framework API observations on this device/build, not an activation or permission guarantee. Temporary shell helper execution is not an APK installation."}


def _attach_display_evidence(observed: dict[str, Any], display: int) -> str | None:
    """Recognize only observed active DeX layouts, never a generic virtual screen."""
    state = observed["observations"]
    if state["dex_enabled"] != 4 or state["dex_display_type"] != 102:
        return None
    external = [d for d in state["displays"] if d["type"] != 1]
    if len(external) == 1 and external[0]["id"] == display and external[0]["type"] in {2, 3}:
        return "sole_physical_or_wireless_display"
    # Live Fold3/API 33: Samsung's system-owned Desktop compositor is distinct
    # from its Wi-Fi sink display. IDs are dynamic; an overlay is not equivalent.
    if observed.get("api") != 33 or state.get("wifi_state") != 2 or len(external) != 2:
        return None
    desktops = [d for d in external if d["type"] == 5 and
                re.fullmatch(r"virtual:android,1000,Desktop,[0-9]+", d.get("unique_id", ""))]
    sinks = [d for d in external if d["type"] == 3]
    if (len(desktops) == len(sinks) == 1 and desktops[0]["id"] == display and
            desktops[0]["id"] != sinks[0]["id"]):
        return "samsung_api33_desktop_with_active_wifi_sink"
    return None


def attach(serial: str, display: int, *, yes: bool = False, view_only: bool = False) -> dict[str, Any]:
    validate_target(serial)
    if not yes or type(display) is not int or display <= 0:
        raise PcSessionError("Attaching requires --yes and an observed nonzero --display ID from pc-probe.")
    observed = runtime_probe(serial, yes=True)
    evidence = _attach_display_evidence(observed, display)
    if evidence is None:
        raise PcSessionError("The selected display is not a recognized desktop of an observed active Samsung dual DeX session.")
    adb = dev_options._resolve_adb_path(None)
    identity = dev_options._read_identity(adb, serial)
    if hashlib.sha256(json.dumps(asdict(identity), sort_keys=True).encode("utf-8")).hexdigest() != observed["identity_binding"]:
        raise PcSessionError("The device/build/user changed after Samsung API inspection; no mirror launched.")
    client = resolve_scrcpy(); _check_scrcpy_version(client, _run_process)
    argv = [str(client), "-s", serial, f"--display-id={display}", "--no-audio", "--window-title", "ADAPT DeX PC"]
    if view_only: argv.append("--no-control")
    env = os.environ.copy(); env["ADB"] = str(adb)
    receipt = _default_snapshot()
    with _log(receipt.with_suffix(".scrcpy.log")) as log:
        process = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    time.sleep(.3)
    report = {"schema_version": 1, "status": "process_started" if process.poll() is None else "failed", "serial": serial,
              "display_id": display, "pid": process.pid, "healthy_streaming": "not_verified", "receipt": str(receipt),
              "display_ownership": "external_user_session", "display_evidence": evidence, "observations": observed,
              "note": "Close this scrcpy window to stop its mirror. The externally started DeX/receiver connection is left to the user and is never disconnected by this command."}
    try:
        dev_options._create_snapshot(receipt, report)
    except Exception:
        if process.poll() is None: _stop_owned_process(process)
        raise
    return report


def public(state: dict[str, Any], path: Path) -> dict[str, Any]:
    return {"schema_version": 1, "status": state["status"], "serial": state["serial"], "method": state["method"],
            "session": str(path), "display_id": state.get("display_id"), "cleanup": state.get("cleanup"),
            "error": state.get("error"), "healthy_streaming": "not_verified", "experimental": True,
            "note": "Only positive Samsung enabled/dual-display evidence permits launch. Close the owned scrcpy window or request pc-stop; retain the snapshot if recovery fails."}


def start(serial: str, *, yes: bool = False, method: str = "loopback", size: str = "1920x1080", dpi: int = 160,
          view_only: bool = False, snapshot: Path | None = None) -> dict[str, Any]:
    planned = plan(serial, method=method, size=size, dpi=dpi, view_only=view_only)
    if not yes:
        raise PcSessionError("PC session activation requires --yes. Review dex pc-plan first.")
    adb = dev_options._resolve_adb_path(None)
    identity = dev_options._read_identity(adb, serial)
    manufacturer = _adb(adb, serial, ["shell", "getprop", "ro.product.manufacturer"])
    api = _adb(adb, serial, ["shell", "getprop", "ro.build.version.sdk"])
    if manufacturer.lower() != "samsung" or not re.fullmatch(r"[0-9]{1,3}", api) or not 33 <= int(api) <= 37:
        raise PcSessionError("Experimental PC activation requires observed Samsung Android 13–17 / API 33–37.")
    original = _adb(adb, serial, ["shell", "settings", "get", "global", "overlay_display_devices"])
    if original not in {"null", "none"}:
        raise PcSessionError("An existing or unknown simulated-display setting will not be replaced.")
    path = (snapshot or _default_snapshot()).absolute()
    state = {"schema_version": 1, "token": uuid.uuid4().hex, "serial": serial, "identity": asdict(identity),
             "method": method, "size": size, "dpi": dpi, "view_only": view_only, "overlay_value": planned["overlay_value"],
             "original_overlay": original, "status": "prepared", "events": [], "created_at": time.time(), "cleanup": "not_started"}
    dev_options._create_snapshot(path, state)
    try:
        with _log(path.parent / (path.name + ".worker.log")) as log:
            subprocess.Popen([sys.executable, "-m", "adapt_adb.dex_pc", "--worker", str(path)], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0, start_new_session=os.name != "nt")
    except OSError:
        state.update(status="failed", error="Could not start the owned supervisor; no activation attempted.")
        _save(path, state)
        return public(state, path)
    with adapting("Activating experimental DeX PC session"):
        deadline = time.monotonic() + 70
        while time.monotonic() < deadline:
            state = _load(path)
            if state["status"] == "running" or state["status"] in TERMINAL:
                return public(state, path)
            time.sleep(.15)
    stop(path)
    raise PcSessionError(f"Session startup timed out; a stop was requested. Inspect pc-status with snapshot {path}.")


def stop(path: Path) -> dict[str, Any]:
    state = _load(path)
    if state["status"] not in TERMINAL:
        request = path.with_name(path.name + ".stop")
        try:
            with request.open("x", encoding="ascii") as stream:
                stream.write(state["token"])
        except FileExistsError:
            if request.read_text(encoding="ascii") != state["token"]:
                raise PcSessionError("Unexpected stop request; no process was killed.")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = _load(path)
            if state["status"] in TERMINAL:
                break
            time.sleep(.2)
    report = public(state, path)
    if state["status"] not in TERMINAL:
        report.update(status="stop_pending", note="Stop requested but not confirmed by the supervisor. Retain this snapshot; no saved PID was killed.")
    return report


def restore(path: Path, serial: str, *, yes: bool = False) -> dict[str, Any]:
    validate_target(serial)
    state = _load(path)
    if not yes or serial != state["serial"]:
        raise PcSessionError("Recovery requires --yes and the snapshot's exact --serial target.")
    adb = dev_options._resolve_adb_path(None)
    identity = _identity(state, adb)
    with _lock(identity):
        if state["method"] != "overlay":
            raise PcSessionError("Loopback recovery is delegated to the helper's STOP/heartbeat watchdog; no blind global disconnect is performed.")
        state["cleanup"] = _restore_overlay(adb, state)
        state["status"] = "stopped"; _save(path, state)
    return public(state, path)


def worker(path: Path) -> None:
    state = _load(path); adb = dev_options._resolve_adb_path(None)
    identity = _identity(state, adb)
    helper = mirror = None; changed = False; pushed = False; events: queue.Queue[str] = queue.Queue()
    def stopping():
        request = path.with_name(path.name + ".stop")
        return request.is_file() and request.read_text(encoding="ascii") == state["token"]
    with _lock(identity):
        try:
            state["status"] = "starting"; _save(path, state)
            scrcpy = resolve_scrcpy(); _check_scrcpy_version(scrcpy, _run_process)
            _adb(adb, state["serial"], ["push", str(helper_path()), _remote(state)]); pushed = True
            before = _probe(adb, state)
            if before["wifi_state"] != 0 or before["dex_enabled"] == 4 or any(d["type"] != 1 for d in before["displays"]):
                raise PcSessionError("An existing wireless/DeX/non-internal display will not be replaced.")
            if state["method"] == "overlay":
                if _adb(adb, state["serial"], ["shell", "settings", "get", "global", "overlay_display_devices"]) != state["original_overlay"]:
                    raise PcSessionError("Overlay changed since planning; no overwrite performed.")
                changed = True  # Mark uncertain writes before invoking ADB.
                _adb(adb, state["serial"], ["shell", "settings", "put", "global", "overlay_display_devices", state["overlay_value"]])
            else:
                helper = subprocess.Popen([str(adb), "-s", state["serial"], *_helper_args(state, "serve")], stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", bufsize=1,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
                def read():
                    for line in helper.stdout:
                        if len(line) <= 8192 and events.qsize() < 100:
                            events.put(line)
                threading.Thread(target=read, daemon=True).start()
            deadline = time.monotonic() + 45; last_ping = 0.; display = None
            while time.monotonic() < deadline and not stopping():
                if state["method"] == "overlay":
                    after = _probe(adb, state); display = classify(before, after)
                    if display is not None: break
                    if time.monotonic() > deadline - 35 and after["dex_enabled"] == 2:
                        raise PcSessionError("A simulated display was created but Samsung DeX remained disabled. This is not a working DeX desktop.")
                    time.sleep(.5)
                else:
                    if helper.poll() is not None:
                        # Drain the final error event before reporting EOF.
                        time.sleep(.05)
                    if time.monotonic() - last_ping > 3 and helper.poll() is None:
                        helper.stdin.write("PING\n"); helper.stdin.flush(); last_ping = time.monotonic()
                    try:
                        message = json.loads(events.get(timeout=.25))
                    except queue.Empty:
                        if helper.poll() is not None: raise PcSessionError("Samsung helper exited before DeX was confirmed.")
                        continue
                    state["events"] = (state["events"] + [message])[-30:]
                    if message.get("event") == "error":
                        raise PcSessionError("Samsung loopback activation refused: " + str(message.get("message", message.get("kind", "unknown"))))
                    if message.get("event") == "ready" and type(message.get("display_id")) is int and message["display_id"] > 0 and message.get("enabled") == 4 and message.get("display_type") == 102:
                        display = message["display_id"]; break
            if display is None:
                raise PcSessionError("DeX activation was stopped or timed out; no mirror launched.")
            env = os.environ.copy(); env["ADB"] = str(adb)
            argv = [str(scrcpy), "-s", state["serial"], f"--display-id={display}", "--no-audio", "--window-title", "ADAPT DeX PC"]
            if state["view_only"]: argv.append("--no-control")
            with _log(path.parent / (path.name + ".scrcpy.log")) as log:
                mirror = subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
            time.sleep(.3)
            if mirror.poll() is not None:
                raise PcSessionError("Owned scrcpy client exited during startup; see its session log.")
            state.update(status="running", display_id=display); _save(path, state)
            while mirror.poll() is None and not stopping():
                if helper is not None:
                    if helper.poll() is not None: raise PcSessionError("The Samsung display helper exited.")
                    helper.stdin.write("PING\n"); helper.stdin.flush()
                time.sleep(1)
            state["status"] = "stopped"
        except Exception as exc:
            state.update(status="failed", error=str(exc)[:1200])
        finally:
            if mirror is not None and mirror.poll() is None: _stop_owned_process(mirror)
            if helper is not None and helper.poll() is None:
                try: helper.stdin.write("STOP\n"); helper.stdin.flush(); helper.wait(timeout=8)
                except Exception: _stop_owned_process(helper)
            try:
                _identity(state, adb)
                if changed: state["cleanup"] = _restore_overlay(adb, state)
                elif state["method"] == "loopback" and pushed:
                    deadline = time.monotonic() + 3
                    active = _probe(adb, state)["wifi_state"]
                    while active != 0 and time.monotonic() < deadline:
                        time.sleep(.2); active = _probe(adb, state)["wifi_state"]
                    state["cleanup"] = "wireless_inactive_verified" if active == 0 else "wireless_state_not_original"
                    if active != 0: state["status"] = "cleanup_conflict"
                else: state["cleanup"] = "not_changed"
            except Exception as exc:
                state.update(status="cleanup_conflict", cleanup="unverified", cleanup_error=str(exc)[:600])
            if pushed:
                try:
                    _identity(state, adb)
                    _adb(adb, state["serial"], ["shell", "rm", "-f", _remote(state)])
                except Exception: state["remote_jar_cleanup"] = "unverified"
            _save(path, state)


def _worker_entry(path: Path) -> None:
    try:
        worker(path)
    except Exception as exc:
        state = _load(path)
        state.update(status="failed", error=str(exc)[:1200], cleanup="activation_not_started_or_supervisor_failed")
        _save(path, state)


def _emit(fn, json_output: bool, **kwargs):
    try: report = fn(**kwargs)
    except (ValueError, dev_options.DevOptionsError, OSError, subprocess.SubprocessError) as exc:
        report = {"status": "refused", "error": str(exc)}
    typer.echo(json.dumps(report, indent=2) if json_output else "ADAPT / EXPERIMENTAL DeX PC\n" + json.dumps(report, indent=2))
    if report.get("status") in TERMINAL - {"stopped"} or report.get("status") == "refused": raise typer.Exit(1)
    if report.get("status") == "stop_pending": raise typer.Exit(2)


@dex_app.command("pc-plan")
def plan_command(serial: str = typer.Option(..., "--serial", "-s"), method: str = typer.Option("loopback", "--method"), size: str = typer.Option("1920x1080", "--size"), dpi: int = typer.Option(160, "--dpi"), view_only: bool = typer.Option(False, "--view-only"), json_output: bool = typer.Option(False, "--json")):
    """Offline preview of experimental activation; no device or process access."""
    _emit(plan, json_output, serial=serial, method=method, size=size, dpi=dpi, view_only=view_only)


@dex_app.command("pc-receiver-plan")
def receiver_plan_command(json_output: bool = typer.Option(False, "--json")):
    """Explain Microsoft's receiver setup without installs or device actions."""
    _emit(receiver_plan, json_output)


@dex_app.command("pc-probe")
def probe_command(serial: str = typer.Option(..., "--serial", "-s"), yes: bool = typer.Option(False, "--yes"), json_output: bool = typer.Option(False, "--json")):
    """Inspect live Samsung desktop/display APIs using a temporary local-built helper."""
    _emit(runtime_probe, json_output, serial=serial, yes=yes)


@dex_app.command("pc-attach")
def attach_command(serial: str = typer.Option(..., "--serial", "-s"), display: int = typer.Option(..., "--display"), yes: bool = typer.Option(False, "--yes"), view_only: bool = typer.Option(False, "--view-only"), json_output: bool = typer.Option(False, "--json")):
    """Mirror a positively observed externally started DeX desktop; preserve its receiver."""
    _emit(attach, json_output, serial=serial, display=display, yes=yes, view_only=view_only)


@dex_app.command("pc-start")
def start_command(serial: str = typer.Option(..., "--serial", "-s"), method: str = typer.Option("loopback", "--method"), size: str = typer.Option("1920x1080", "--size"), dpi: int = typer.Option(160, "--dpi"), view_only: bool = typer.Option(False, "--view-only"), snapshot: Path | None = typer.Option(None, "--snapshot"), yes: bool = typer.Option(False, "--yes"), json_output: bool = typer.Option(False, "--json")):
    """Start an owned, confirmed DeX display session; no silent method fallback."""
    _emit(start, json_output, serial=serial, method=method, size=size, dpi=dpi, view_only=view_only, snapshot=snapshot, yes=yes)


@dex_app.command("pc-status")
def status_command(snapshot: Path = typer.Option(..., "--snapshot"), json_output: bool = typer.Option(False, "--json")):
    """Read the saved supervisor state only; no live device queries."""
    _emit(lambda: public(_load(snapshot), snapshot), json_output)


@dex_app.command("pc-stop")
def stop_command(snapshot: Path = typer.Option(..., "--snapshot"), json_output: bool = typer.Option(False, "--json")):
    """Ask the matching supervisor to close its own mirror and recover its state."""
    _emit(stop, json_output, path=snapshot)


@dex_app.command("pc-restore")
def restore_command(serial: str = typer.Option(..., "--serial", "-s"), snapshot: Path = typer.Option(..., "--snapshot"), yes: bool = typer.Option(False, "--yes"), json_output: bool = typer.Option(False, "--json")):
    """Recover an inactive owned overlay session without overwriting external changes."""
    _emit(restore, json_output, path=snapshot, serial=serial, yes=yes)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker": _worker_entry(Path(sys.argv[2]))
    else: raise SystemExit("Internal session supervisor; use adapt dex pc-start.")
