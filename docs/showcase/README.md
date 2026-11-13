# ADAPT visual showcase

`index.html` is a self-contained, responsive page (inline CSS and JavaScript, no network
requests) with a view picker, an all-views mode, clickable highlights and a pausable
title animation. It honors `prefers-reduced-motion`: the title starts static for those users.

Ten equal views:

| # | View | Kind |
|---|------|------|
| 1 | Home screen with the original banner | generated |
| 2 | Host doctor | generated |
| 3 | Developer profiles | generated |
| 4 | Tool catalog and ROM plans | generated |
| 5 | Android capability matrix | generated |
| 6 | Tailscale connection stages (documented, not proven) | generated |
| 7 | Samsung DeX, native receiver (experimental) | generated |
| 8 | Rapid ADAPT test plan | generated |
| 9 | MCP and SSH access | generated |
| 10 | `adapt --no-banner --no-tips android versions` | **actual** |

* **Generated** views are source-derived illustrations. They call the real rendering and
  planning code in `src/adapt_adb` with synthetic values. No host, device or network was used.
* **Actual** view 10 is the real stdout of an offline command, with normalized line endings in
  `../assets/cli-android-versions.txt` with a hash in `../assets/cli-android-versions.provenance.json`.
  The PNG/SVG next to it are a *rendered capture*, not a Windows Terminal screenshot.
* "9 of 10" counts equal views. It is not a pixel measurement.
* There are no Android screenshots. The animated title is a branding asset for this page and the
  README; the terminal banner is unchanged and static.

Rebuild everything (needs Pillow; nothing is downloaded):

```text
python scripts/build_showcase.py
python scripts/build_showcase.py --verify
```
