# Grill: open-sourcing CLAMPEL
Date: 2026-10-15

## Intent
Make the existing private repo (`EatingVoltage/clampel`) public and open source, with a README written for a stranger who wants to build their own lamp. Until now the README described one lamp on one PC.

## Constraints
- Licence GPL-3.0, same as KNOBZ.
- The host runs on Windows only: a kernel32 process walk, a Startup-folder shortcut, `pythonw` and the detach flags. There's no Mac or Linux machine to test a port on, so no port ships untested.
- The user's own lamp stays exactly as it is: fused, on COM1, paired to `48:F6:EE:13:EC:44`. Nothing in the release may change how it's updated or identified.

## Key decisions
- Decision: go public now, before there's a photo. Reason: the state table carries the README. A photo or GIF gets added later. Alternative considered: hold the flip until the photo exists.
- Decision: reader = a Claude Code user who can solder and has run PlatformIO. No beginner hand-holding. Alternatives considered: total beginners (much longer doc), or firmware people (README leans on the technical story).
- Decision: say "Windows only" up front. Reason: nothing to test a port on, and an untested port produces bug reports that can't be reproduced. Alternative considered: port to macOS/Linux before release (rejected for now, see open questions).
- Decision: one README in builder order: what it is → build → install → use, then a rule, then how it works, protocol, hardening, troubleshooting, porting. Alternative considered: the eatingLights split (README + HARDWARE.md + FIRMWARE.md), too much for a project this small.
- Decision: the eFuse is optional and gets its own "Hardening (one-way)" section, framed as worth it only if you flash other ESP32-C3s on the same PC, and only after a `clampel flash` round trip plus the rollback test. Alternative considered: keep it as a setup step (more strangers would burn a fuse they don't need), or leave it undocumented.
- Decision: a new `clampel hooks` command prints the hook block with this clone's path and the absolute `python.exe` filled in. It never writes settings.json. Reason: hand-editing nine paths is error-prone, and a bare `python` can be the Microsoft Store stub; since hooks are async and swallow errors, the lamp would silently stay dark. Alternative considered: have `clampel install` merge settings.json (it would edit the user's global config).
- Decision: the code is the canonical hook definition (`HOOKS` in `clampel.py`). The README shows the table and the command, not the JSON.
- Decision: `lamp.json` leaves git. A new `clampel pair` lists `303A:1001` ports **without opening any** and writes `lamp.json` only when exactly one is plugged in. If `lamp.json` already names a different board, it refuses without `--force`. A missing `lamp.json` still builds; upload and monitor refuse and say "run clampel pair". Alternative considered: keep the user's MAC committed as a safe-fail default (confusing error, a dirty file in every clone).
- Decision: the public `CLAUDE.md` states its rules conditionally ("if the lamp is fused…"). This lamp's facts (fused 2026-10-12, COM1, MAC, the eatingVoltage naming-rule note, dingpicker) move into a gitignored `CLAUDE.local.md`. Reason: as shipped, a stranger's Claude would refuse their first flash, `pio run -t upload`. `.grill/` stays public as design history.
- Decision: build from source only. No binaries; tag the public commit `v1.1.3`. Alternatives considered: GitHub Releases with firmware.bin, or a browser flasher (where the lamp.json guard can't protect the first flash).
- Decision: shared as-is, no support promised, PRs welcome (a macOS/Linux port above all). A Porting section lists exactly the Windows-specific spots. Issues stay on.
- Decision: present it as "a status lamp for Claude Code" plus "Not affiliated with or endorsed by Anthropic", in the README and the GitHub description.

## Surfaced assumptions
- "Make this a repo" turned out to mean "make it public". The repo already existed, private and pushed.
- The commit email is already public on KNOBZ and eatingLights, so the flip exposes nothing new.
- A stranger's lamp starts out unfused: their first flash *is* `pio run -t upload`, which the old CLAUDE.md forbade outright.
- On POSIX a port would lose protection layer 1 (serial ports aren't exclusive) and COM1 sorting. Linux also raises DTR and RTS on open, then pyserial clears DTR first, briefly hitting the C3's reset condition. Only hardware testing can show whether the bridge then resets the lamp on every connect.
- An npm-installed Claude Code runs as `node`, not `claude`, so the process walk would need another rule.

## Open questions
- macOS/Linux port: waits for a test rig or a contributor's PR.
- Photo or GIF for the top of the README.
- Whether to point the user's own `~/.claude/settings.json` hooks at the absolute python path, which `clampel hooks` now prints. It isn't changed in this pass.

## Out of scope
- Binary releases, web flasher.
- Auto-editing anyone's settings.json.
- Active maintenance or a support promise.
