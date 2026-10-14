# CLAUDE.md

Guidance for Claude Code working in this repository. The README covers what the lamp does, the wiring, the protocol and setup; this file covers what you must not break.

## What this is

CLAMPEL is a status lamp for Claude Code: an ESP32-C3 SuperMini with one 5 mm WS2812D-F5, permanently plugged into a Windows PC, which shows what Claude is doing. You are probably driving it right now: this session's hooks write its state.

```
firmware/src/main.cpp   the whole firmware: renders a mode, watchdog, serial self-update
firmware/lamp_port.py   PlatformIO pre-script: upload/monitor port = lamp.json's USB serial, never autodetect
host/clampel.py         hook entry point + bridge daemon + CLI (stdlib + pyserial, one file on purpose)
lamp.json               the paired lamp's USB serial (= its MAC), written by `clampel pair`. Gitignored.
CLAUDE.local.md         this clone's lamp: fused or not, its COM port, local conventions. Gitignored.
.grill/                 design decisions and the reasoning behind them
```

Division of labour: the **host owns all logic** (which session counts, done→idle decay, credits override, interrupt detection). The **firmware only renders** `S <mode>` and protects itself. Keep it that way: a behaviour change belongs in `clampel.py`, which needs no reflash.

## Hard rules

**Know whether the lamp is eFuse-locked before any USB flash.** `CLAUDE.local.md` says so for this clone. If it doesn't exist or doesn't say, ask the user, and treat the lamp as fused until told otherwise. Never probe with esptool or espefuse to find out: that resets the lamp into ROM download mode.

- **Fused** (`DIS_USB_SERIAL_JTAG_DOWNLOAD_MODE` burned): never run `pio run -t upload` or esptool against it. That fails and leaves the lamp dark in ROM download mode until the bridge resets it. The only update path is `cd firmware && pio run && python ../host/clampel.py flash`.
- **Not fused**: the first flash is `pio run -t upload` (guarded by `lamp_port.py`). Every later update still goes through `clampel flash`, so that path stays tested.

**Bump `CLAMPEL_VERSION` in `platformio.ini` on every firmware change.** `clampel flash` detects a rollback only by comparing version strings.

**If you touch the update path, re-run the rollback test before trusting it.** That means the `U` command, `pumpUpdate()`, `verifyRollbackLater()`, the `?` handler that marks the image valid, or `Lamp.flash()`. Build it with `pio run -e rollback-test` (it isn't a default env), then run `clampel flash .pio/build/rollback-test/firmware.bin`; it must end with the *old* version back. On a fused lamp, a broken updater means soldering a USB-UART adapter to GPIO20/21.

**Never identify the lamp by COM number or VID:PID.** Every ESP32-C3 is `303A:1001`, and people flash other C3s on the same PC. Only `lamp.json`'s USB serial identifies the lamp. Never write a probe that opens other C3 ports. `clampel pair` filters by VID:PID only to *list* candidates, never opens a port, and refuses unless exactly one is plugged in.

**Don't open the lamp's port directly while the bridge runs.** It holds the port on purpose; that's the first protection layer. Use `clampel raw "<line>"`. If you must open it yourself (bridge stopped), set `dtr = rts = False` **before** `open()`, because DTR/RTS edges reset a C3. To reset it on purpose, use `pulse_reset()`: Windows' usbser.sys sends an RTS change only when DTR is rewritten.

**After editing `host/clampel.py`, restart the bridge** (`clampel stop`, then `clampel start`). The running bridge keeps the old code. Hooks pick up changes immediately, since every hook is a fresh process.

**Hooks must never slow or break Claude.** They are async, `main()` swallows every exception from `hook`, and the hook only writes one JSON file. Don't add network, serial or anything slow to the hook path.

**The hook definition lives in `HOOKS` in `clampel.py`; `clampel hooks` prints it for `~/.claude/settings.json`.** The user's settings file is user-global, not in this repo. If you change hook events or arguments, change `HOOKS` and the README's hook table, then tell the user to re-merge. Don't edit their settings.json without asking, and never touch their other hooks.

**Windows only.** `main()` refuses other platforms on purpose. A port must replace the process table, the bridge detach, `install`, and the port-lock and DTR assumptions together; the README's Porting section lists them. Don't half-port.

## Hardware facts learned the hard way

- LED DIN is on **GPIO4**. On GPIO3 the first build's LED sat at full blue regardless of data; re-soldering to GPIO4 fixed it.
- **The first unit reads RGB, not the datasheet's GRB** (`kLedRgbOrder = true`). With GRB, "think" pulsed green. Other batches may differ.
- The LED needs 5 V (3.7–5.3 V); the 3.3 V data line is fine (V_IH min 2.7 V).
- Keep GPIO20/21 free (UART recovery). Avoid GPIO2/8/9 (strapping; 8 = onboard blue LED, 9 = BOOT).
- **The C3's USB identity can't be renamed.** Its USB Serial/JTAG is fixed silicon, `"USB JTAG/serial debug unit"`. There's no TinyUSB on a C3, so don't try to patch descriptors.
- PlatformIO's autodetect uploads to the string-sorted *last* matching port. Putting the lamp on COM1 keeps dev boards winning. Renaming the port while the bridge holds it leaves no DOS device name until a replug.

## Colour language

Blue means **credits only** (slow breath). Don't reuse it: the update indicator moved to cyan and the boot blip to white for exactly that reason. Priority across sessions is ask (amber) > credits (blue) > the session with the latest prompt. The README has the full table.

## Verifying a change

- Logic: run the hook path offline by pointing `LOCALAPPDATA` at a scratch dir, piping fake hook JSON into `python -I host/clampel.py hook <event>`, and calling `load_sessions()` / `choose()`. Use bash `printf` or forward slashes, because `echo` mangles `\\` in JSON.
- On the lamp: `clampel test <state> <sec>` overrides for a few seconds. **You can't see the LED**, so ask the user what it showed. A roll-call (think → ask → credits → done → idle → off) is the quickest check.
- After the fact: `bridge.log` records every `lamp <old> -> <new> (<session> via <event>)`.

## Git

Never commit `firmware/.pio/`, `lamp.json` or `CLAUDE.local.md`. The repo is public under GPL-3.0.
