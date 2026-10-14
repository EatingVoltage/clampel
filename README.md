# CLAMPEL: a status lamp for Claude Code

eatingVoltage

One LED on a €3 board, plugged into your PC, that shows what Claude Code is doing, so you can stop watching the terminal.

**Windows only** for now (see [Porting](#porting)). Not affiliated with or endorsed by Anthropic.

| Lamp | Meaning |
|---|---|
| red, slow pulse | Claude is working |
| amber, blinking | Claude is blocked on you: a permission prompt, a question, a plan to approve. **Any** session can trigger this. |
| blue, slow "sleeping" breath | waiting for credits: a usage limit or billing error stopped the turn |
| green, bright | the turn just finished, your move (for 10 min) |
| green, dim | Claude is open and idle |
| off | no live Claude session, or the bridge isn't running, or the PC is asleep |
| short white blip | the lamp just booted |
| fast cyan breathing | firmware update in progress |

With several sessions open, the lamp follows the one you **last submitted a prompt in**. Amber from any other session still breaks through, and then blue does. Usage limits are per account, so one session waiting means all of them are. Blue stops overriding as soon as any session gets work through again after it.

## What you need

- **ESP32-C3 SuperMini**, the tiny USB-C board, about €3.
- **WS2812D-F5**, a 5 mm through-hole addressable RGB LED. Any single WS2812/WS2812B works, but check the colour order (below).
- A USB-C **data** cable, a soldering iron, and three short wires (or just the LED's legs).
- A Windows 10/11 PC with Claude Code, **Python 3** with `pyserial` (`python -m pip install pyserial`), and **PlatformIO** (the CLI or the VS Code extension).

## Build

```
 WS2812D-F5 (flat side = pin 1)          ESP32-C3 SuperMini
 1 DOUT  ── cut off, unused
 2 VDD   (longest leg) ─────────────────  5V     (part needs 3.7–5.3 V, NOT 3V3)
 3 GND   ───────────────────────────────  G
 4 DIN   ───────────────────────────────  GPIO4
```

- The C3's 3.3 V data line drives the LED directly (V_IH min 2.7 V at 5 V). No level shifter needed.
- **Colour order.** The datasheet says GRB, but the first unit read RGB, so the firmware defaults to RGB (`kLedRgbOrder` in `firmware/src/main.cpp`). If "working" pulses green instead of red, flip that constant.
- Use GPIO4. On the first build, DIN sat on GPIO3 and the LED stayed full blue whatever it was told.
- Keep GPIO20/21 free: they're the last-resort UART recovery path. Avoid GPIO2/8/9 (strapping pins; 8 is the blue onboard LED, 9 is BOOT).
- Paint over the board's red power LED (black nail polish or paint marker). The firmware keeps the blue one dark.

## Install

`clampel <cmd>` below is short for `python host\clampel.py <cmd>`, run from the repo root.

1. **Clone** to where it will live. The hooks point at this path, so if you move the clone later, redo step 5.

   ```
   git clone https://github.com/EatingVoltage/clampel
   cd clampel
   ```

2. **Pair.** Plug in the lamp and *no other* ESP32-C3, then run `clampel pair`. It writes the board's USB serial number (its MAC) to `lamp.json`. From then on, only that board counts as the lamp, because every ESP32-C3 has the same USB ID and the serial number is the only way to tell them apart. Pairing only lists ports; it never opens one.

3. **Flash** the firmware over USB:

   ```
   cd firmware
   pio run -t upload
   ```

   `lamp_port.py` aims the upload at the paired board and refuses to fall back to autodetect. This is the only USB flash the lamp needs. Later updates go over serial ([Using it](#using-it)).

4. **Start the bridge.** `clampel install` puts a shortcut in your Startup folder so it runs at logon, and `clampel start` starts it now. The bridge holds the lamp's COM port and decides what it shows.

5. **Hooks.** `clampel hooks` prints the hooks block for `~/.claude/settings.json`, with this clone's path and your Python's full path filled in. Merge it in: where an event already has hooks, append these to its list. Every hook is async, and errors are swallowed, so the lamp can never slow down or break Claude. New Claude Code sessions pick them up.

6. **Check.** `clampel status` must show `CLAMPEL <version> <mac> valid`. Then roll-call the states one at a time: `clampel test think 6`, then `ask`, `credits`, `done`, `idle`. You should see red, amber, blue, bright green, dim green.

If you also flash other ESP32-C3s on this PC, read [Protection against stray uploads](#protection-against-stray-uploads) next.

## Using it

```
clampel status                 who's tracked, what the lamp shows
clampel test ask 5             force a state for 5 s
clampel brightness 40          1–100, stored on the lamp (default 60)
clampel raw "?"                send one protocol line, print replies
clampel flash                  serial firmware update (build first: cd firmware; pio run)
clampel stop | start           release / grab the COM port
clampel install                start the bridge at logon (Startup folder)
clampel pair [--force]         pair the one ESP32-C3 plugged in (--force replaces an existing pairing)
clampel hooks                  print the hooks block for ~/.claude/settings.json
clampel fuse                   burn the USB-download eFuse (optional, one-way, see below)
```

**Updating the firmware:** bump `CLAMPEL_VERSION` in `firmware/platformio.ini`, then `cd firmware && pio run && python ..\host\clampel.py flash`. The image goes into the idle OTA slot. If the new firmware doesn't check in with the bridge within 30 s, the bootloader rolls back to the old one. `flash` notices the rollback by comparing version strings, which is why you bump it.

**After editing `host\clampel.py`**, run `clampel stop` and then `clampel start`, because the running bridge keeps the old code. Hooks pick up changes immediately.

Every lamp state change is logged with its reason. That's how to check what the lamp did while you weren't looking:

```
findstr "lamp" %LOCALAPPDATA%\clampel\bridge.log
2026-10-12 15:08:53 lamp think -> done (test)
2026-10-12 15:08:55 lamp done -> think (7b277fc0 via think)
```

---

## How it works

```
Claude Code hooks ──(async)──► clampel.py hook <event> ──► sessions\<id>.json
                                                                 │
                                    bridge (pythonw, at logon) ◄─┘  every 200 ms
                                      │ picks one state, holds COM port open
                                      ▼
                                 "S think\n" ──► lamp renders; dark after 5 s of silence
```

The host owns all the logic: which session counts, the done → idle decay, the credits override, interrupt detection. The firmware only renders a mode and protects itself, so behaviour changes need no reflash.

| Hook | Matcher | Writes |
|---|---|---|
| SessionStart | `startup\|resume\|clear` | idle (also starts the bridge if needed) |
| UserPromptSubmit | | think, marks "last input" |
| PreToolUse | `AskUserQuestion\|ExitPlanMode` | ask |
| Notification (`hook notify`) | `permission_prompt\|elicitation_dialog\|elicitation_url_dialog\|agent_needs_input` | ask |
| | `quota_auto_resume_fired` | think (limit reset, Claude carries on) |
| | `quota_auto_resume_stale` | ask (reset while the PC slept, waits for Enter) |
| | `quota_auto_resume_disabled` | done (gave up waiting) |
| PostToolUse, PostToolUseFailure | | think (also clears ask after you answer) |
| Stop | | done |
| StopFailure (`hook fail`) | | credits if `error` is `rate_limit` or `billing_error`, else done |
| SessionEnd | | deletes the session file |

The definition lives in `HOOKS` in `host/clampel.py`, and `clampel hooks` prints it. Sessions that die without `SessionEnd` are dropped once their `claude.exe` process is gone. An Esc interrupt, which may not fire `Stop`, is detected from the transcript tail.

Runtime state lives in `%LOCALAPPDATA%\clampel\`: one JSON file per session in `sessions\`, plus `bridge.log`.

## Talking to the lamp

USB serial (USB Serial/JTAG, baud rate irrelevant), ASCII lines ending in `\n`. Open the port with **DTR and RTS low**: on a C3, DTR/RTS edges can reset the chip. While the bridge runs it owns the port, so use `clampel raw "<line>"` instead of a serial monitor.

| Send | Reply | Effect |
|---|---|---|
| `?` | `CLAMPEL <version> <mac> valid\|pending` | identify; also confirms a freshly updated image |
| `S off\|idle\|done\|think\|ask\|credits` | `OK` | set the mode; the animation runs on the lamp |
| `B <1-100>` | `OK` | master brightness (perceptual), saved in NVS |
| `H` | | heartbeat only |
| `P h\|l\|z` | `OK` | diagnostic: hand the LED pin to plain GPIO (high / low / high-Z) until reboot. A real WS2812 keeps its colour whatever the pin does. A plain RGB LED follows the pin. |
| `R` | `OK` | reboot |
| `U <size> <md5>` | `READY` | start an update: then send the image in 1024-byte chunks, waiting for `ACK <total>` after each; ends with `DONE` and a reboot |

Any received line feeds the watchdog. With nothing received for 5 s the lamp goes dark, so a dead bridge or a sleeping PC never leaves it lying.

The protocol is open: anything that can write a line to a COM port can drive the lamp. Only Claude Code is wired up here.

## Protection against stray uploads

This only matters if you flash **other ESP32-C3 boards on the same PC**. Every C3 enumerates as `303A:1001 "USB JTAG/serial debug unit"`, which is fixed in silicon, so PlatformIO's port autodetect can't tell the lamp from a dev board and may overwrite the lamp. There are three layers; the first is always on.

1. **Port lock (always on).** The bridge holds the COM port open, so esptool or PlatformIO gets "could not open port". It covers you only while the bridge runs.

2. **COM1 (recommended).** Given two `303A:1001` boards, PlatformIO uploads to the one whose COM name sorts *last as a string*. "COM3" sorts after "COM12", so dev-board uploads would aim at the lamp. "COM1" always sorts first. Run `clampel stop`, then Device Manager → Ports → the lamp → Properties → Port Settings → Advanced → COM Port Number → **COM1**, then **replug the lamp**. If you rename while the bridge holds the port, Windows records COM1 but never creates the `COM1` device name, and nothing can open the port until a replug.

3. **eFuse (optional, one-way).** Burning `DIS_USB_SERIAL_JTAG_DOWNLOAD_MODE` makes the lamp refuse every USB flash, forever, bridge running or not. After that, `clampel flash` is the only way to update it, and if that ever breaks, the only way back is soldering a USB-UART adapter. Only do it once **all** of these have worked:
   - the roll-call from Install step 6,
   - one `clampel flash` round trip with a bumped version,
   - the rollback test: build it with `pio run -e rollback-test` in `firmware\`, then `clampel flash firmware\.pio\build\rollback-test\firmware.bin`. It must end with the *old* version back.

   Then `python -m pip install esptool "bitstring<5"` (PlatformIO's bundled espefuse lacks `cryptography`, and bitstring 5 broke espefuse) and run `clampel fuse`. It stops the bridge, asks you to type `BURN`, resets the lamp and restarts the bridge.

**What an upload attempt does to a fused lamp:** the uploader's USB reset drops the chip into ROM download mode. The ROM then refuses USB and just waits there: the lamp goes dark and its USB serial stops draining. The bridge spots that (a hello that times out, or 15 s without a hello) and hard-resets the lamp over RTS, so the lamp is back within seconds. Without the bridge, it comes back the next time the bridge starts, or when you replug it.

**Last-resort recovery** (both app slots bad on a fused lamp): use a 3.3 V USB-UART adapter on GPIO20 (RX) / GPIO21 (TX) / G, hold BOOT while plugging in, and flash with esptool over the adapter's port. The fuse only blocks *USB* download mode, not UART.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Lamp never lights, `status` lists no sessions | The hooks aren't firing. Re-run `clampel hooks` and check the block is merged into `~/.claude/settings.json`, then start a new Claude Code session. If you moved the clone, the old paths are dead. |
| Lamp dark, `status` says `not connected` | The bridge can't open the port; `bridge.log` says why. "cannot find the file specified" after a COM renumber means replug the lamp. |
| `clampel start` says no lamp paired | Run `clampel pair` with only the lamp plugged in. |
| Lamp dark, `status` shows the port but no `CLAMPEL …` line | The lamp sits in ROM download mode (something tried to flash it). The bridge resets it within about 15 s; replugging also works. |
| Lamp dark, everything connected | No live Claude session (`status` lists none), or the watchdog: the lamp got no line for 5 s. |
| Fixed colour, ignores every state | Data isn't reaching the LED. Check DIN (pin 4, opposite the flat side) and the solder joint. `clampel raw "P z"` releases the data pin: a real WS2812 keeps its colour, while a plain RGB LED goes dark (wrong part). `clampel raw "R"` reboots back to normal. |
| Red and green swapped | Colour order. Flip `kLedRgbOrder` in `firmware/src/main.cpp`, bump the version, `clampel flash`. |
| `clampel flash` ends with "same version string as before" | Either you flashed the same version, or the new image never said hello and was rolled back. Check that the version was bumped. |
| `pio run -t upload` in another C3 project goes for the lamp's port | The dev board isn't plugged in (or not enumerated), so autodetect fell back to the lamp. The port lock refuses (and, if fused, the ROM too; the bridge resets the lamp a few seconds later). Plug in the dev board. |

## Porting

The firmware is portable; the host isn't. `clampel.py` refuses to run anywhere but Windows, because these parts would break quietly:

- **Process table** (`process_table`, `process_ctime`, `find_claude_process`): kernel32 via ctypes, looking for an ancestor named `claude.exe`. It needs `/proc` on Linux and `ps` or libproc on macOS. An npm-installed Claude Code runs as `node`, not `claude`.
- **Bridge detach** (`ensure_bridge`): `pythonw` plus `DETACHED_PROCESS` flags. POSIX wants `start_new_session=True`.
- **Autostart** (`cmd_install`): a Startup-folder shortcut. macOS wants a launchd agent, Linux a systemd user unit.
- **Serial**: on Windows an open COM port is exclusive, which *is* protection layer 1. On POSIX it isn't, so pass `exclusive=True` to pyserial, and the COM1 trick has no equivalent. Linux also raises DTR and RTS when the tty opens, and pyserial then clears DTR first, briefly hitting RTS=1/DTR=0, the C3's reset condition. Check on real hardware that the bridge doesn't reset the lamp every time it connects. `pulse_reset()` works around a usbser.sys quirk that may not exist elsewhere.
- **State dir** (`STATE_DIR`): `%LOCALAPPDATA%`, falling back to the home directory.

A port PR is welcome. Please test it on a real lamp and say which OS and version you tested.

## Layout

```
host/clampel.py      hook entry point + bridge daemon + CLI, stdlib + pyserial, one file
firmware/            PlatformIO, Arduino-ESP32 2.0.17 (espressif32@6.10.0)
  src/main.cpp       renders a mode, watchdog, serial self-update with rollback
  lamp_port.py       pins upload/monitor to lamp.json's serial; refuses to autodetect
lamp.json            your lamp's USB serial number, written by `clampel pair` (gitignored)
CLAUDE.md            rules for Claude Code sessions working on this repo
.grill/              the design decisions and why
```

## Licence

GPL-3.0, see [LICENSE](LICENSE). Built for one desk and shared as-is: no support promised, but issues and PRs are welcome, a macOS/Linux port above all.
