# Contributing to ADAPT

Thanks for helping. ADAPT is an alpha toolkit that touches real devices and a shared ADB daemon, so contributions are held to a few non-negotiable rules about safety, honesty and privacy. This guide covers them and the mechanics.

By contributing you agree that your contribution is licensed under the project's [MIT License](LICENSE). Include appropriate notices for any third-party material and update [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

## Ground rules

1. **Safety invariants stay intact.** ADAPT never runs `adb kill-server` or `sudo adb`, refuses destructive package commands, requires an exact device target (the documented exceptions: `adapt adb` and the read-only MCP tools use the only ready device when none is named, and the MCP tools that act on the phone default to the target of a one-target `--device` session), verifies the port-5037 server before talking to it, keeps outputs bounded, and writes device settings only through allowlisted, snapshotted, read-back, restorable paths. A change that weakens one needs an explicit design discussion first.
2. **Claims stay at the evidence.** Do not write that something "works", is "supported" or "certified" unless a test or recorded physical observation shows it. Prefer the project's vocabulary: *offline-tested*, *observed*, *planning*, *experimental*. Keep [known limitations](docs/known-limitations.md) current.
3. **No private data, ever.** See [Privacy](#privacy-and-fixtures).
4. **Machine output stays clean.** Results go to stdout; banners, tips, progress and logs go to stderr; `--json` and `adapt mcp` print nothing else on stdout.
5. **Plan first, act on explicit approval.** New mutating behavior needs a read-only inspection path, an offline `plan` where practical, an explicit `--yes` or interactive confirmation, and a recovery path.

## Set up from a checkout

Python 3.11 or newer. Users install the wheel from GitHub Releases (see the [README](README.md#download-and-install)); ADAPT is not on PyPI. Contributors work from the repository.

```text
python -m venv .venv
# Windows PowerShell:  .\.venv\Scripts\Activate.ps1      Linux/macOS shells:  source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[mcp]" pytest
python -m pytest -q
```

Pushes to `main` and pull requests run this test suite on Linux and Windows (GitHub Actions, `.github/workflows/ci.yml`).

The `mcp` extra (the MCP SDK and Pillow) is required to import the MCP modules. `pytest` is declared in the `dev` dependency group of `pyproject.toml`; `pip install --group dev` needs pip 25.1 or newer, so the command above installs it by name. The suite lives in `tests/` and is **offline**: it must not need a phone, a network, an SSH host, Tailscale, or a real `adb`. It mocks processes and uses `typer.testing.CliRunner`. Run a focused file while iterating, for example `python -m pytest -q tests/test_dev_options.py`.

Check the CLI without a device: `adapt --help`, `adapt help-me --all`, `adapt android versions`, any `plan` command.

The architecture and file map are in [docs/architecture.md](docs/architecture.md).

## Privacy and fixtures

This repository is public. Never commit, paste into issues, or put in fixtures, tests, docs, examples or screenshots:

- real device serials, IMEIs, MAC addresses, build fingerprints from your own devices, or Wi-Fi network names;
- real IP addresses, ports of your own devices, routes, tailnet names or hostnames;
- usernames, home-directory paths, email addresses, account or project identifiers;
- tokens, keys, pairing codes, passwords or SSH material;
- raw device logs, snapshots, `status.json` files, or **Android screenshots**.

Use placeholders and documentation-reserved values: `SERIAL`, `PHONE_PEER`, `PAIR_PORT`, `CONNECT_PORT`, `com.example.app`; IPv4 `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24` (RFC 5737); IPv6 `2001:db8::/32` (RFC 3849); `example.com` and `.test` names. Test fixtures should be synthetic and obviously fake. If you captured evidence from a device, describe the *result* ("the property was empty") rather than attaching the capture.

## Code conventions

- Small, typed functions with `from __future__ import annotations`; follow the surrounding module's style. No formatter or linter is enforced.
- Use `subprocess` with argument lists and `shell=False`; set timeouts; bound output sizes.
- Validate all user-controlled values (serials, packages, hosts, ports, paths) with the existing validators. Reject control characters and leading `-`.
- Return typed reports with `status`, an explicit `exit_code`, and a `claim_boundary` or `note` that says what was **not** established.
- Never add automatic fallbacks that widen scope (another device, another transport, a device-wide log dump).
- Do not add network calls to `--help`, `plan`, `status` or import paths.

## Adding or changing a CLI command

1. Implement it in the owning module and register it on that group's Typer app (root commands live in `src/adapt_adb/cli.py`).
2. Give every option a precise name; use `--serial/-s` for the target and `--json` for machine output; add `--yes` for side effects.
3. Add a `HOW_TO` topic in `src/adapt_adb/help_system.py` if the command deserves a tip, with a generic example, and give the command an effect class (`reads_host`, `reads_device`, `acts_on_device` or `changes_host`) in `COMMAND_EFFECTS` in `src/adapt_adb/agent_guide.py`; a test fails for a command without one.
4. Add tests (parsing, refusal paths, JSON stdout cleanliness, and safety boundaries) under `tests/`.
5. Update [docs/command-reference.md](docs/command-reference.md), [docs/features.md](docs/features.md) and `docs/feature-inventory.json` so the counts (79 visible leaf commands in 15 groups in the development tree at the time of writing; `docs/ADAPT-overview.pdf` is generated from the inventory by `scripts/build_public_overview.py`) stay true, and the console's screens in `src/adapt_adb/terminal_ui.py` if a new group is added.

## Adding or changing an MCP tool

1. Add its schema to `TOOL_SCHEMAS` in `src/adapt_adb/mcp/tools.py` (strict: `additionalProperties` is forced off, so list every property, with enums and bounds) and its implementation.
2. Classify it in `src/adapt_adb/mcp/access.py`: observation vs mutating, and whether it is device-bound (`DEVICE_TOOLS`). Mutating tools must stay hidden by `--read-only`; device-bound tools must honor `--device`.
3. Dispatch it in `src/adapt_adb/mcp/server.py`; keep errors generic and bounded. A tool that acts on the phone must not choose a device itself: it needs an explicit `device` unless the session was started with exactly one `--device`.
4. Extend `tests/test_mcp*.py` for the contract and access policy.
5. Update [docs/mcp-access.md](docs/mcp-access.md) (and the 25 / 20 / 5 / 19 counts). The tool's `title` and annotations are derived from the same classification (`tool_title` and `tool_annotation_hints` in `access.py`); give an acting tool that is not destructive or is idempotent an entry there, otherwise it is advertised as destructive and not idempotent. `adapt agent primer`, the MCP `instructions` and the `adapt://session` resource read the tool list from the code, so they need no edit. Check that the `help` topics and resources still describe the tool if it changes a workflow (`src/adapt_adb/mcp/resources.py`).

## Native receiver (`native/windows-receiver`)

Rust 1.99.1 (pinned in `rust-toolchain.toml`). Build with `cargo build --release --locked` (or `scripts\build_receiver.ps1` from the repository root in the development tree) and test with `cargo test --locked --offline` from that directory. The receiver's `status.json` is an interface that `adapt dex receiver-status` reads; change it together with `src/adapt_adb/dex_receiver.py`. Keep the upstream MIT [LICENSE](native/windows-receiver/LICENSE) and update `THIRD-PARTY-NOTICES.txt` and the root [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) whenever dependencies change. Do not claim compatibility from a build or from the offline tests; hardware claims need a recorded observation with its exact scope.

## Documentation

Keep the root [README](README.md) concise; put detail in `docs/`. When behavior changes, update the page that owns it and [known limitations](docs/known-limitations.md). Examples must be syntax-valid against the current source and use placeholders. Generated or illustrative material must be labeled as such and must not look like test evidence.

## Pull requests

- Keep changes focused; describe what changed, why, and how you tested it (command and result).
- Mention any safety, privacy or claim-boundary impact.
- Note which docs and counts you updated.
- Large or architectural changes: open an issue first.

## Reporting problems

For a bug, include ADAPT's version (`pip show adapt-adb`), host OS, Python version, the command (with placeholders) and the sanitized `status`/`note` fields. Do not attach raw logs or captures. For a suspected security issue, use the repository's private security advisory feature if available; otherwise open a minimal issue that omits exploit details and private data, and ask for a private channel.
