# CLAMPEL: Claude status lamp

An ESP32-C3 SuperMini with one WS2812D-F5 on top, permanently plugged into the PC.

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

## Wiring

```
 WS2812D-F5 (flat side = pin 1)          ESP32-C3 SuperMini
 1 DOUT  ── cut off, unused
 2 VDD   (longest leg) ─────────────────  5V     (part needs 3.7–5.3 V, NOT 3V3)
 3 GND   ───────────────────────────────  G
 4 DIN   ───────────────────────────────  GPIO4
```

- Its 3.3 V data line drives the D-F5 directly (V_IH min 2.7 V at 5 V). No level shifter needed.
- **Colour order: this unit reads RGB**, although the datasheet says GRB. With GRB, "think" pulsed green. The firmware swaps for it (`kLedRgbOrder` in `main.cpp`). Flip that constant for a GRB part.
- DIN was on GPIO3 first and the LED sat at full blue whatever it was told. After re-soldering onto GPIO4 it worked. If a new build shows that symptom, see [Troubleshooting](#troubleshooting).
- Keep GPIO20/21 free. They're the last-resort UART recovery path. Avoid GPIO2/8/9 (strapping pins; 8 is the blue onboard LED, 9 is BOOT).
- Paint over the board's red power LED (black nail polish or paint marker). The firmware keeps the blue GPIO8 LED dark.

## Setup on a PC

Needs Python 3 with `pyserial` (`python -m pip install pyserial`) and PlatformIO. The lamp on this PC is already set up; these steps are for a fresh PC or a new lamp.

1. **Pair.** Put the new board's USB serial number into `lamp.json`. Plug in only that board and read it from `python -c "from serial.tools import list_ports; [print(p.device, p.serial_number) for p in list_ports.comports()]"`.
2. **First flash.** `cd firmware && pio run -t upload`. This is the only USB flash a lamp ever gets; `lamp_port.py` refuses to upload unless it finds the paired serial.
3. **Bridge at logon.** `python host\clampel.py install` puts a shortcut in the Startup folder, and `python host\clampel.py start` starts it now.
4. **Hooks.** Merge this into `~/.claude/settings.json` (adjust the path if the repo lives elsewhere). Every hook is async, and `hook` swallows all errors, so the lamp can never slow down or break Claude.

   ```json
   "hooks": {
     "SessionStart":       [{ "matcher": "startup|resume|clear", "hooks": [{ "type": "command", "command": "python", "args": ["-I", "C:/Projects/clampel/host/clampel.py", "hook", "start"],  "async": true }] }],
     "UserPromptSubmit":   [{ "hooks": [{ "type": "command", "command": "python", "args": ["-I", "C:/Projects/clampel/host/clampel.py", "hook", "input"],  "async": true }] }],
     "PreToolUse":         [{ "matcher": "AskUserQuestion|ExitPlanMode", "hooks": [{ "type": "command", "command": "python", "args": ["-I", "C:/Projects/clampel/host/clampel.py", "hook", "ask"], "async": true }] }],
     "PostToolUse":        [{ "hooks": [{ "type": "command", "command": "python", "args": ["-I", "C:/Projects/clampel/host/clampel.py", "hook", "think"],  "async": true }] }],
     "PostToolUseFailure": [{ "hooks": [{ "type": "command", "command": "python", "args": ["-I", "C:/Projects/clampel/host/clampel.py", "hook", "think"],  "async": true }] }],
     "Notification":       [{ "matcher": "permission_prompt|elicitation_dialog|elicitation_url_dialog|agent_needs_input|quota_auto_resume_fired|quota_auto_resume_stale|quota_auto_resume_disabled",
                              "hooks": [{ "type": "command", "command": "python", "args": ["-I", "C:/Projects/clampel/host/clampel.py", "hook", "notify"], "async": true }] }],
     "Stop":               [{ "hooks": [{ "type": "command", "command": "python", "args": ["-I", "C:/Projects/clampel/host/clampel.py", "hook", "done"],   "async": true }] }],
     "StopFailure":        [{ "hooks": [{ "type": "command", "command": "python", "args": ["-I", "C:/Projects/clampel/host/clampel.py", "hook", "fail"],   "async": true }] }],
     "SessionEnd":         [{ "hooks": [{ "type": "command", "command": "python", "args": ["-I", "C:/Projects/clampel/host/clampel.py", "hook", "end"],    "async": true }] }]
   }
   ```

5. **Check.** Run `python host\clampel.py status`; the lamp line must show `CLAMPEL <version> <mac> valid`. Then do a roll-call, one state at a time: `test think 6`, `ask`, `credits`, `done`, `idle`.
6. **COM1, then the eFuse.** See [Overwrite protection](#overwrite-protection). Do the eFuse only after the roll-call and one `clampel flash` round trip have worked.

## Layout

```
lamp.json            the lamp's USB serial number (= MAC). Single source of truth.
firmware/            PlatformIO, Arduino-ESP32 2.0.17 (espressif32@6.10.0)
  lamp_port.py       pins upload/monitor port to lamp.json's serial; refuses to autodetect
host/clampel.py      hook entry point + bridge daemon + CLI, stdlib + pyserial
.grill/              the design decisions and why
```

Runtime state lives in `%LOCALAPPDATA%\clampel\`: one JSON file per session in `sessions\`, plus `bridge.log`.

## How it fits together

```
Claude Code hooks ──(async)──► clampel.py hook <event> ──► sessions\<id>.json
                                                                 │
                                    bridge (pythonw, at logon) ◄─┘  every 200 ms
                                      │ picks one state, holds COM port open
                                      ▼
                                 "S think\n" ──► lamp renders; dark after 5 s of silence
```

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

Sessions that die without `SessionEnd` are dropped when their `claude.exe` process is gone. An Esc interrupt, which may not fire `Stop`, is detected from the transcript tail.

## CLI

```
python host\clampel.py status                 who's tracked, what the lamp shows
python host\clampel.py test ask 5             force a state for 5 s
python host\clampel.py brightness 40          1–100, stored on the lamp
python host\clampel.py raw "?"                send one protocol line, print replies
python host\clampel.py flash                  serial self-update (builds: cd firmware; pio run)
python host\clampel.py stop | start           release / grab the COM port
python host\clampel.py install                start the bridge at logon (Startup folder)
python host\clampel.py fuse                   burn the USB-download eFuse (new lamps only, one-way)
```

Every lamp state change is logged with its reason, which is the way to check what the lamp did while you weren't looking:

```
findstr "lamp" %LOCALAPPDATA%\clampel\bridge.log
2026-10-12 15:08:53 lamp think -> done (test)
2026-10-12 15:08:55 lamp done -> think (7b277fc0 via think)
```

## Talking to the lamp

USB serial (USB Serial/JTAG, baud rate irrelevant), ASCII lines ending in `\n`. Open the port with **DTR and RTS low**: on a C3, DTR/RTS edges can reset the chip.

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

While the bridge runs it owns the port, so go through `clampel raw "<line>"` instead of a serial monitor.

## Overwrite protection

Every ESP32-C3 enumerates as `303A:1001 "USB JTAG/serial debug unit"`, and that's fixed in silicon. The lamp can't be renamed, so the eatingVoltage USB-naming rule can't apply. It is told apart only by its USB serial number, `48:F6:EE:13:EC:44`.

1. **Port lock (active).** The bridge holds the COM port open, so esptool or PlatformIO gets "could not open port". It starts at logon from `Startup\clampel bridge.lnk`.
2. **COM1 (done 2026-10-12).** Device Manager → Ports → the lamp → Properties → Port Settings → Advanced → COM Port Number → **COM1**. Run `clampel stop` first and **replug the lamp afterwards**. If you rename while the bridge holds the port, Windows records COM1 but never creates the `COM1` device name, and nothing can open the port until a replug. Reason: given two `303A:1001` boards, PlatformIO's autodetect uploads to the one whose COM name sorts *last as a string*. "COM3" sorts after "COM12", so every dev-board upload would aim at the lamp. "COM1" always sorts first.
3. **eFuse (burned 2026-10-12).** `DIS_USB_SERIAL_JTAG_DOWNLOAD_MODE` = 1. Nothing can flash the lamp over USB any more, bridge running or not. Verified: with the bridge stopped, `esptool chip_id` against the lamp fails with "Write timeout". Updates go through `clampel flash` only (also verified after the burn). For a *new* lamp, run `clampel fuse` after `python -m pip install esptool "bitstring<5"`. PlatformIO's bundled espefuse lacks `cryptography`, and bitstring 5 broke espefuse.

**What an upload attempt does to the fused lamp:** the uploader's USB reset drops the chip into ROM download mode. The ROM then refuses USB and just waits there: the lamp goes dark and its USB serial stops draining. The bridge spots that (a hello that times out, or 15 s without a hello) and hard-resets the lamp over RTS. If the bridge is running, the lamp is back within seconds; otherwise it comes back the next time the bridge starts. Replugging fixes it too.

### Updating after the fuse

`cd firmware && pio run`, then `python host\clampel.py flash`. The image goes into the idle OTA slot. The new firmware must hear `?` from the bridge within 30 s, or the bootloader rolls back to the old one. The `rollback-test` env proves that net: `clampel flash .pio\build\rollback-test\firmware.bin` must end with the *old* version back. Run it once more if you ever change the update code.

### Last-resort recovery

If both app slots are bad: use a USB-UART adapter (3.3 V) on GPIO20 (RX) / GPIO21 (TX) / G, hold BOOT while plugging in, and flash with esptool over the adapter's port. The fuse only blocks *USB* download mode, not UART.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Lamp dark, `status` says `not connected` | Bridge can't open the port. `bridge.log` says why. "cannot find the file specified" after a COM renumber means replug the lamp. |
| Lamp dark, `status` shows the port but no `CLAMPEL …` line | The lamp sits in ROM download mode (something tried to flash it). The bridge resets it within about 15 s; replugging also works. |
| Lamp dark, everything connected | No live Claude session (`status` lists none), or the watchdog: the lamp got no line for 5 s. |
| Fixed colour, ignores every state | Data isn't reaching the LED. Check DIN (pin 4, opposite the flat side) and the solder joint. `clampel raw "P z"` releases the data pin: a real WS2812 keeps its colour, while a plain RGB LED goes dark (wrong part). `raw "R"` reboots back to normal. |
| Red and green swapped | Colour order. Flip `kLedRgbOrder` in `firmware/src/main.cpp`, bump the version, `clampel flash`. |
| `clampel flash` ends with "same version string as before" | Either you flashed the same version, or the new image never said hello and was rolled back. Check that the version was bumped. |
| `pio run -t upload` in another C3 project fails on COM1 | The dev board isn't plugged in (or not enumerated), so autodetect fell back to the lamp. Harmless: the fuse refuses, and the bridge resets the lamp a few seconds later. Plug in the dev board. |
