# ADAPT home screen

Run `adapt` in an interactive terminal, or `adapt console`, to open the main
screen. `adapt home` also returns to this screen from a command prompt.
The original green → white → gray shaded ADAPT artwork is rendered at the top
of every menu page. It uses the existing banner's terminal sizing, Unicode/ASCII
fallback and color preferences; the artwork itself is unchanged.

The main menu groups the existing commands into host/setup, devices/wireless,
app/developer testing, displays/DeX, tools/ROM workflows, AI/MCP/SSH, Rapid ADAPT,
and logs/help. **Rapid ADAPT is one option within this same application.**
All commands are also available through the dynamic command browser and their
original CLI paths.

- **H** or **0**: return to the main ADAPT screen.
- **B**: go back one screen.
- **N / P**: next/previous page of longer output.
- **Q**: exit the console.
- **R** on a command detail page: enter arguments and run that original ADAPT
  command. Use double quotes for paths containing spaces. No shell is evaluated.
  In the development tree (unreleased) the argument prompt also accepts `:b` to go
  back, `:h` (or `H`) for the main screen and `:q` to exit.

Opening menus performs no host/device checks or installations. Running a command
is explicit and retains its existing prompts, confirmations and exact-target
requirements. Operations use their native output/prompts; after completion choose
H for the main screen or Enter to return to the detail view. MCP's stdio server
is launched through an AI client or the original `adapt mcp` CLI, outside this
interactive browser.

With tips on, a command's detail page also lists its `COMMON FAILURES` (failure
ID and one-line summary; `adapt explain ID` shows causes and steps). When a run
ends with a non-zero exit code, the status line adds that the command's own
`Why:`/`Try:` notes are above it. `explain` is in the "Logs, help & preferences"
area.

The console redraws its viewport on transitions, keeps longer screens paged,
and restores the original terminal buffer on exit. It does not erase terminal
scrollback. This follows the clear-and-redraw navigation pattern visible in
[Airgeddon's upstream main and submenu code](https://github.com/v1s1t0r1sh3r3/airgeddon/blob/master/airgeddon.sh#L7715-L7800);
no Airgeddon code or artwork is bundled.

Use `adapt console --no-redraw` for a screen reader or basic terminal. `TERM=dumb`
also disables repaint controls. `NO_COLOR`, `ADAPT_NO_BANNER`, `--no-banner`,
`--no-tips`, `--no-progress` and the existing saved tips preference remain in use.
Small terminals use the existing compact logo and pagination.

Direct CLI commands and JSON/MCP output keep their original output contract;
only this explicitly interactive console clears/redraws. Redirected bare `adapt`
continues to print help and exit 2. A redirected `adapt console` refuses to read
input and points to `adapt --help` / `adapt help-me --all --json`.
