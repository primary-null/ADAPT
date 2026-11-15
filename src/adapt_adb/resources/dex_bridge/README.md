# ADAPT Samsung display bridge

`AdaptDexBridge.java` is the auditable source for the small `app_process` helper.
The ADAPT helper source is covered by the repository's MIT license. The
reference links below record interoperability research; those external
projects are not vendored in this helper directory.
`adapt-dex-bridge.jar` is its locally compiled DEX archive, not a Samsung binary
or an APK. `build.json` binds its source and archive SHA-256 values. Runtime
checks refuse an inconsistent bundle. No helper is downloaded at runtime.

Build on a Windows/Linux development host with local Java and Android SDK:

```text
python scripts/build_dex_bridge.py --sdk PATH_TO_ANDROID_SDK
```

The script currently uses SDK platform 33 and build-tools 36.0.0, Java 8 bytecode
and D8 min API 29. The Python experimental workflow separately gates API 33–37.
Build output contains no workstation paths in its receipt. Normal users need
only the packaged helper, not Java/SDK compilation tooling.

The `probe` command observes Samsung desktop/Wi-Fi/display state through framework
APIs; `serve TOKEN` requests a loopback receiver session and holds it while host
heartbeats arrive. It refuses existing non-internal/Wi-Fi/DeX sessions and does
not preemptively disconnect them. Cleanup requests a disconnect only when both
the active display's name and address match this session token. Host heartbeat
loss is best-effort recovery, not a guarantee after crashes or disconnection.

The loopback interoperability approach is informed by the public
[ScrcpyDeX implementation](https://github.com/Aureliano021/ScrcpyDex/tree/b3d5b159ce445bbfc61a7326268dec21ce6f938b/server)
(upstream Apache-2.0); the simulated-display alternative is described in
[DX Manager](https://github.com/maze-mei/DX-Manager/tree/f391baae391b5aca39d68905645af3982fb777cc)
(upstream MIT). Those projects' claims are not ADAPT hardware acceptance.
Samsung framework classes are accessed on the device rather than redistributed.

Live testing on an Android 13 Samsung device found the API present but denied by
`CONFIGURE_WIFI_DISPLAY`. The helper does not grant permissions, request root,
change SELinux or bypass the denial. See [DeX for PC](../../../../docs/dex-pc.md)
for recorded limitations.
