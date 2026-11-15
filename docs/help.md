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
adapt help-me adb.not_found
adapt explain --list
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
do not disable errors, safety checks, permission decisions, or confirmations;
they shorten failure guidance to its one-line pointer (see below).
Explicitly requested help remains available when tips are off.

**Rapid ADAPT** groups the short check/prepare/reproduce/capture/restore loop.
`adapt rapid plan` previews it; `adapt logs guide` explains deeper Samsung/vendor,
window and print diagnostics. Neither guide enables logging or runs a device
workflow. See [the workflow and recovery limits](rapid-adapt.md).

Tips are written only to stderr and only when stdin, stdout, and stderr are
interactive terminals. JSON invocations and the MCP server suppress automatic
tips. Redirected output and automation receive no instructional chatter; the
only additions are the one-line failure pointer described below and the
root/Administrator safety warning.

**Root and Administrator warning.** When ADAPT runs as root (for example under
`sudo`) or in an elevated Windows session, commands that can start or talk to
adb (`doctor`, `setup`, `devices`, `adb`, `pair`, `connect`, `everything` and the
device command groups) print one `Warning:` line on stderr per invocation. As
root it explains that an adb server started from there runs as root and can
leave root-owned files that later block normal use, and asks you to run ADAPT
as your normal user; when elevated it explains that tools in non-elevated
sessions cannot share that server. It is a safety notice, not a tip: tips
controls do not hide it, but `--json` does, and it never prompts or changes an
exit code. `adapt doctor` also shows it in its report
([doctor contract](doctor.md#33-host-platform-and-privilege)).

## Failure guidance

When a command fails, its existing error line is printed first, unchanged, with
the same exit code. ADAPT then adds guidance on stderr: what it observed
(`Why:`), possible causes, most likely first, and what to try (`Try:`), each
step tagged `[read-only]`, `[changes this PC]`, `[changes the phone]`,
`[on the phone]` or `[manual]`. Causes are possibilities, not verdicts. When a
step depends on something ADAPT cannot see (your OS, an environment variable,
the phone's screen), it is labelled, for example `On Linux:`, and the block
offers a read-only check such as `adapt doctor`. Guidance never runs anything,
never prints serials, addresses, user names or paths beyond what the error line
already showed, and never suggests stopping the shared ADB server, running adb
with sudo or as Administrator, deleting the ADB key, rooting or unlocking.
Those appear only in `Avoid:` notes.

```text
Error: Explicit target is unavailable or not in state 'device'.

Why: ADB can see this device, but it is in state 'unauthorized': the phone has
  not approved this computer for USB debugging.
  Possible causes, most likely first:
  - The "Allow USB debugging?" prompt is waiting on the phone, or was dismissed.
  ...
Try:
  1. Unlock the phone. When "Allow USB debugging?" appears, choose Allow ...   [on the phone]
  ...
More: adapt explain device.unauthorized   |   adapt help-me devices
```

How much is shown:

| Situation | Human stderr | `--json` / MCP |
|---|---|---|
| stderr is a terminal | full block | not applicable |
| stderr is redirected (pipes, CI, IDE tasks) | one line: `More: adapt explain ID` | not applicable |
| `--no-tips`, `adapt tips off`, `ADAPT_TIPS=0` | one line | unaffected |
| `ADAPT_FAILURE_GUIDANCE=full`, `short` or `off` | as set (beats the preference) | unaffected |
| `"failure_guidance": "full"/"short"/"off"` in preferences.json | as set | unaffected |
| `--json` | nothing on stderr | a `failure` object in the error payload |
| MCP server | nothing | a `failure` object in every error result ([MCP access](mcp-access.md#errors)) |

The `failure` object in JSON output is additive (existing keys never change):
`schema`, `id`, `summary`, `certainty`, `causes`, `recommendations` (each with
`text`, `command`, `effect`, `changes_state`, `needs_admin` and `applies`, which
is `"unknown"` when the step depends on something ADAPT could not tell),
`avoid`, `help_topic`, `docs`, `explain` and `context_unknown`. A broken
preferences file gives the one-line form and never blocks the command.

Where guidance appears:

- `devices`, `adb`, `pair`, `connect`, `setup` and `mcp`.
- `dev-options`, `dex` (`inspect`, `list-displays`, `plan-dual-mirror`, `mirror`)
  and `android capabilities`: after the `Error:` line, and as a `failure` key in
  their `--json` error payloads (`{"ok": false, "error": KIND, "message": ...}`
  keeps its keys). The `failure.id` names the situation more precisely than the
  `error` kind; for example a failed `dev-options apply` is
  `devopts.apply_rolled_back` when ADAPT read every changed setting back at its
  original value, and `devopts.apply_rollback_incomplete` when it could not
  confirm that. Both say how to check the phone (`adapt dev-options inspect`)
  and the snapshot file. A `dev-options restore` that stops on conflicts adds
  guidance after its `Conflict:` lines.
- `help-me` (unknown topic), `tips` and `everything` refusals, including a
  `failure` key in their JSON error payloads.
- `adapt doctor`: every check in the JSON report carries `failure_id` (null
  when it passed or was skipped). The terminal view shows `More: adapt explain ID`
  under each blocked or waiting check; redirected output adds one line naming
  them all. See [doctor](doctor.md).
- `app`, `logs`, `debug`, `rapid plan`, `detect`, `desktop`, `dex pc-*`, `ssh`,
  `tailscale`, `tools` and `everything` results (`adapt devices` too, when adb
  itself exits non-zero): after the existing report, which keeps its keys, stream
  and exit code, and as a `failure` key in `--json` reports that are refused,
  failed, partial or inconclusive. A device that did not answer is named as such
  (`device.unanswered`) instead of as the symptom the command looked for. A result
  that exits 0 but could not observe some values adds one line,
  `Some values were not observed: adapt explain ID`; a started scrcpy window that
  ADAPT does not watch adds `If no window appears: adapt explain ID`.
- `dex receiver-status`, `receiver-start` and `receiver-stop`: after the existing
  report on stdout, and as a `failure` key in `--json` reports that are refused,
  failed or pending, or (for `receiver-status`) unreadable, stale, unverified or
  not working. What ADAPT itself observed or refused is a
  `dexpc.native_receiver.*` guide (for example
  `dexpc.native_receiver.exe_not_found` or `dexpc.native_receiver.stop_unconfirmed`);
  a reason code the receiver recorded is its `dexpc.receiver.CODE` guide. A report
  that exits 0 but records a failure (a receiver that exited without recording a
  shutdown, or a phone attempt the running receiver recorded as failed) adds only
  the `More:` line. `receiver-plan` runs nothing and never fails.
- The MCP tools `dex_inspect`, `dex_displays`, `android_capabilities` and
  `developer_options_inspect` keep their `error` and `message` and carry the
  specific `failure` object; so do `app_validate`, `app_logs`,
  `debug_app_inspect` and `diagnostic_logs`.
- In the interactive console (`adapt` or `adapt console`), a command's detail
  screen lists its common failures under `COMMON FAILURES` when tips are on, and
  after a command exits with a non-zero code the status line adds
  `Its Why/Try notes are above; adapt explain lists all failures.`

### `adapt explain`

```text
adapt explain                          # same as --list
adapt explain --list [--json]          # every failure ID with a one-line summary
adapt explain device.unauthorized      # all causes, all steps, setup variations
adapt explain "failed to authenticate" # search IDs, summaries and known messages
adapt explain unauthorized --json
```

One match shows the full guide; several are listed; no match exits `2` with
`{"error": "unknown_failure", ...}` under `--json`. Explaining runs nothing and
shows every platform variant, labelled. `adapt help-me ID` accepts the same
failure IDs, and `adapt help-me TOPIC` lists the topic's common failures. IDs
are stable: scripts and AI clients may match on them. The native Windows
receiver's reason codes are included as `dexpc.receiver.CODE` (for example
`adapt explain dexpc.receiver.port_in_use`; `adapt explain dexpc.receiver`
lists all of them), and the outcomes of the `dex receiver-*` commands as
`dexpc.native_receiver.OUTCOME` (`adapt explain dexpc.native_receiver` lists
them). A help topic of several words is printed in quotes, for
example `adapt help-me "dev-options apply"`.

### Unexpected errors

If ADAPT itself fails unexpectedly, it prints
`Error: ADAPT stopped unexpectedly while ACTIVITY (ExceptionType).` and a
generic block instead of a Python traceback; the exception text is not printed
because it can contain paths. Exit code stays `1`. With `--json`, stdout gets
`{"status": "error", "exit_code": 1, "error": "internal_error", "failure": {...}}`
and stderr stays empty. Run the command again with `ADAPT_DEBUG=1` to see the
traceback.

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
