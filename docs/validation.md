# Validation scope

The initial public export is an alpha source release. Automated source tests,
physical observations, research topologies and release artifacts are separate
forms of evidence; their counts must not be added together as a pass rate.

## Automated checks

- Python: the complete offline suite is run from the public export. The latest
  publication result is recorded in `publication-checks.json`.
- Native receiver: all 15 Rust offline tests passed from the public source with
  `cargo test --locked --offline`; see `publication-checks.json`. These tests
  do not start the receiver or involve a phone.
- Source and distribution privacy checks cover the selected public tree,
  generated assets, packaged metadata and the reachable initial Git history.
  Automated scanners cannot guarantee the absence of every sensitive value.

## Physical observations before publication

Two Samsung phones provided physical observations: Android 13/API 33 and
Android 17/API 37. These observations do not certify every baseline in the
Android 10-17 capability matrix or every OEM implementation.

- USB and Wi-Fi ADB transports, host preflight and selected-device inspection
  were exercised in bounded sessions.
- A private routed VPN/hotspot path returned authenticated ADB commands and a
  retained connection recheck. Direct Tailnet ADB acceptance remains unproven:
  the strict verifier expects an ADB TLS-port property that was empty on the
  tested firmware. Pairing success and an open port alone do not prove an
  established ADB transport.
- A Windows native wireless DeX prototype decoded desktop frames on Android
  13; one controlled reconnect and separate phone/native desktop keyboard
  observations passed after a focus-routing change. The receiver remains a
  manual-build experimental component, outside the Python wheel.
- The stay-awake profile was inspected on physical devices. Profile rollback,
  conflicts and partial-failure recovery are also covered offline.

## Outstanding acceptance

Native Debian runtime, other OEMs, intermediate Android versions, fresh host
provisioning across architectures, external MCP clients, live SSH workers,
secondary-display MCP input, direct Tailnet validation and long-duration native
receiver behavior remain separate acceptance work.

The showcase contains nine generated source-derived illustrations and one
rendered capture of actual offline CLI output. It contains no Android screen
images. Generated examples are not physical-test evidence.
