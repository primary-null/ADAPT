# Third-party notices

ADAPT (Android Device Agent Provisioning Toolkit) is released under the **MIT License**: see [`LICENSE`](LICENSE), "Copyright (c) 2026 ADAPT Contributors". The root license covers the Python package, tests, scripts, documentation and the helper source `src/adapt_adb/resources/dex_bridge/AdaptDexBridge.java`.

This file lists the third-party material that is **included in this repository**, material that is **derived from** other projects, and material ADAPT **uses but does not distribute**. It is a good-faith summary, not legal advice; each component's own license text is authoritative.

## 1. Included or derived source

### Native Windows receiver (`native/windows-receiver`)

| Item | License | Notes |
|---|---|---|
| **DeXDesk** by DeXDesk contributors, [github.com/adirh3/DeXDesk](https://github.com/adirh3/DeXDesk) at commit `5070ccda5c363f5ed0b46d1fe7cf2c15c47fc125` | MIT, "Copyright (c) 2026 DeXDesk contributors" | The receiver source is derived from this revision. The upstream license is preserved verbatim in [`native/windows-receiver/LICENSE`](native/windows-receiver/LICENSE). ADAPT-specific additions in that tree are covered by the root MIT license |
| Rust crate dependencies pinned in `Cargo.lock` | MIT, Apache-2.0 and Unicode-3.0 terms as reproduced in the notices file | `anyhow`, `itoa`, `memchr`, `proc-macro2`, `quote`, `serde`, `serde_core`, `serde_derive`, `serde_json`, `socket2`, `syn`, `unicode-ident`, `zmij`, and Microsoft's `windows*` crates. Where a crate offers MIT or Apache-2.0, MIT was selected. `unicode-ident` retains its additional Unicode license notice |
| Rust / LLVM and GNU toolchain runtime notices | Apache-2.0 with LLVM exceptions; GPL-3.0 with the GCC Runtime Library Exception | Reproduced because a binary built with the `x86_64-pc-windows-gnu` toolchain can link those runtimes. They apply to such binaries, not to ADAPT's source |

The complete texts are in [`native/windows-receiver/THIRD-PARTY-NOTICES.txt`](native/windows-receiver/THIRD-PARTY-NOTICES.txt). Its `resource_stamp` tool embeds that file and the upstream license into a built executable; **keep both with any binary you redistribute.** Windows system DLLs are not redistributed. See the receiver's [README](native/windows-receiver/README.md).

### Samsung display helper (`src/adapt_adb/resources/dex_bridge`)

`AdaptDexBridge.java` is ADAPT's own source (MIT). `adapt-dex-bridge.jar` is its locally compiled DEX archive, with source and archive SHA-256 values recorded in `build.json`; it is neither a Samsung binary nor an APK. Its design is informed by two public interoperability projects that are **not vendored**: [ScrcpyDeX](https://github.com/Aureliano021/ScrcpyDex) (Apache-2.0) and [DX Manager](https://github.com/maze-mei/DX-Manager) (MIT). Samsung framework classes are used on the device through its own firmware, never redistributed.

## 2. Python dependencies (installed by pip, not vendored)

| Package | Use | License (per upstream) |
|---|---|---|
| `typer` | Command-line framework (required) | MIT |
| `packaging` | Version parsing (required) | Apache-2.0 OR BSD-2-Clause |
| `mcp` | MCP SDK for `adapt mcp` (optional `mcp` extra) | MIT |
| `Pillow` | Screenshot scaling for the MCP `screenshot` tool (optional `mcp` extra) | MIT-CMU (HPND-style) |

Transitive dependencies (for example the packages `typer` and `mcp` pull in) keep their own licenses; review the metadata of the environment you install into. No Python dependency's source is copied into this repository.

## 3. Software ADAPT downloads or invokes (not distributed here)

| Software | How ADAPT uses it | Terms |
|---|---|---|
| **Android SDK Platform-Tools** (`adb`, `fastboot`) | `adapt setup` downloads Google's official archive from `dl.google.com` into user scope | [Android SDK License Agreement](https://developer.android.com/studio/terms) |
| **scrcpy** (Genymobile) | `adapt tools install scrcpy` fetches the pinned release (v5.0.1) from its GitHub releases with SHA-256 verification | Apache-2.0, [tagged license](https://github.com/Genymobile/scrcpy/blob/v5.0.1/LICENSE); bundled components keep their own licenses |
| OpenSSH client, Tailscale client | Detected and driven if you already installed them | Their own licenses and terms |
| Microsoft Wireless Display optional feature | Described by `adapt dex pc-receiver-plan`; `scripts/install_windows_wireless_display.ps1` is an optional, reviewed, elevated installer you run yourself | Microsoft terms |
| Other catalog tools (Git, repo, GnuPG, JDK, Python, ccache, Perfetto, SDK command-line tools, AOSP host tools, payload-dumper-go) | Guidance and PATH observation only; ADAPT does not download them | Each upstream's license |

## 4. Data and references

The Android 10-17 / API 29-37 matrix and capability notes were compiled from Android's public documentation; the catalog links to each tool's upstream source and license page. Documentation links point to primary sources and were checked on the dates stated in the individual guides.

## 5. Trademarks

Android, Google, Samsung, Samsung DeX, Galaxy, Windows, Microsoft, Wi-Fi Direct, Miracast, Tailscale, scrcpy and other names are the property of their respective owners. ADAPT is an independent project and is not affiliated with, endorsed by, or sponsored by any of them. No Samsung software, firmware or proprietary client is included.

## 6. Reporting an omission

If a notice is missing or wrong, open an issue or pull request that names the file and the correct license text. See [CONTRIBUTING.md](CONTRIBUTING.md).
