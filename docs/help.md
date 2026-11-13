# Built-in help and contextual tips

The guide is available offline. It does not probe devices, install tools, or
change host settings. Its command inventory is built from the running CLI, so
only registered features appear and newly integrated command groups are included.

```text
adapt help-me
adapt help-me --all
adapt help-me wireless
adapt help-me "dev-options apply"
adapt help-me "tools plan"
adapt help-me everything
adapt help-me app
adapt help-me logs
adapt help-me debug
adapt help-me rapid
adapt help-me "logs diagnostic"
adapt help-me --all --json
```

The starter view suggests a first workflow and lists available topics. `--all`
includes every registered command, its description, arguments/options, and
available usage instructions. A topic shows a shorter procedure, examples, and
prerequisites. Existing `adapt COMMAND --help` flags remain available.

Interactive command groups and numbered doctor actions show a short `HOW TO`
tip for the chosen option. For example, choosing Device Manager explains that
it opens Windows' driver interface and rechecks afterward, rather than claiming
to repair a driver. Choosing a Developer Options profile explains the snapshot
and restoration workflow. Tips add no pause or confirmation step of their own.

```text
adapt tips off
adapt tips on
adapt tips status
adapt tips status --json
adapt --no-tips doctor
```

`tips on/off` persists for the current OS user. `--no-tips` suppresses tips for
one invocation. `ADAPT_TIPS=0` also suppresses instructional tips. These controls
do not disable errors, safety checks, permission decisions, or confirmations.
Explicitly requested help remains available when tips are off.

**Rapid ADAPT** groups the short check/prepare/reproduce/capture/restore loop.
`adapt rapid plan` previews it; `adapt logs guide` explains deeper Samsung/vendor,
window and print diagnostics. Neither guide enables logging or runs a device
workflow. See [the workflow and recovery limits](rapid-adapt.md).

Tips are written only to stderr and only when stdin, stdout, and stderr are
interactive terminals. JSON invocations and the MCP server suppress automatic
tips. Redirected output and automation receive no instructional chatter.

Preferences are stored in `%LOCALAPPDATA%/adapt/preferences.json` on Windows or
`${XDG_CONFIG_HOME:-~/.config}/adapt/preferences.json` on Linux. Setting
`ADAPT_CONFIG_DIR` selects a different preferences directory. Reading help and
running ordinary commands never creates a preferences file; only an explicit
`tips on/off` writes it. Updates preserve other preference fields. Malformed
preferences suppress automatic tips without blocking unrelated commands;
`tips status` reports the problem.

`adapt everything --dry-run` previews the combined acquisition and detection
workflow. `adapt everything` displays the plan and asks for confirmation.
See [Give Me Everything](everything.md) for exact scope and exit codes.
Longer terminal operations repeat an `adapting...` activity indicator. Disable
it separately with `adapt --no-progress COMMAND` or `ADAPT_NO_PROGRESS=1`.
JSON, MCP and redirected streams suppress animation automatically.
