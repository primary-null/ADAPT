# Give Me Everything

Run the supported host setup and detection steps together:

```text
adapt everything --dry-run
adapt everything
adapt everything --yes --json
adapt everything --device SERIAL --no-path
```

`adapt give-me-everything` is an equivalent alias. The interactive doctor menu
also offers **Give Me Everything (preview and confirm)**. A normal interactive
run shows its scope before one confirmation. Automation requires `--yes`;
JSON execution also requires `--yes`. Dry-run needs no confirmation and performs
only local file/PATH observations: no programs, ADB calls, downloads or writes.

The batch checks host and shared-ADB safety, reuses or installs Platform-Tools
through the existing setup workflow, reuses or installs pinned scrcpy, rechecks
readiness, and inventories every catalog tool. The plan includes the Android
10–17 capability matrix. Existing healthy tools are reused without a forced
replacement. Platform-Tools can update the current user's PATH; `--no-path`
disables that update. scrcpy uses its existing user-scope managed installer.
Both acquisition paths keep their existing source, integrity and license checks.

Managed Platform-Tools batch acquisition is limited to x86-64 Windows/Linux.
scrcpy uses its own supported host/architecture matrix. Other catalog tools
receive detection results and upstream acquisition instructions. ADAPT does not
yet install OS packages, Android SDK components, ROM-build dependencies or
source-tree utilities automatically. A PATH match is labeled unverified.

No device is implicitly selected for extended inspection. Repeated
`--device SERIAL` arguments opt exact targets into read-only Android capability,
Developer Options, root/ROM/bootloader evidence, desktop and DeX/display inspection.
Saved debug-app configuration is also inspected. App-specific validation and log
extraction require a separately selected package and are not run automatically.
The batch also enumerates fastboot transports independently, without switching
device modes or querying a bootloader implicitly. These reports describe observations,
including missing or unknown evidence; an executed inspection does not certify
the feature. The batch does not pair devices, change settings, launch mirrors,
install APKs, build ROMs, unlock, flash, install privileged drivers or modify VMs.
It does not add installation tools to the MCP server.

A shared-daemon safety failure stops setup and ADB device probing; independent
fastboot enumeration can still run. Independent setup
failures are recorded; successful steps remain installed. Final JSON retains the
individual reports and refreshed inventory. Exit codes are `0` completed, `1`
failed step, `2` manual/incomplete work (also refusal), and `3` unsafe or unverified
shared-ADB state. Cancellation makes no changes and exits `0`. Missing manual
tools produce `completed_with_manual_steps`, never an “everything installed” claim.

## Activity indicator

Longer terminal operations repeat `adapting.`, `adapting..`, `adapting...` until
they return. The indicator starts after a short delay and clears on success,
failure or interruption. It appears during diagnostics, downloads/extraction,
pair/connect work and device inspections/profile operations, outside prompts.
It represents activity, not estimated progress or success.

```text
adapt --no-progress everything
```

`ADAPT_NO_PROGRESS=1` also disables it. JSON, MCP, redirected streams and
`TERM=dumb` suppress animation automatically. Tips and banner preferences are
independent controls.
