# Grill: Claude status lamp (CLAMPEL)
Date: 2026-10-12

## Intent
A desk lamp that shows at a glance what Claude Code is doing, so the user doesn't have to watch the terminal: red pulsing = working, green = your turn, off = no Claude open. With several sessions open it follows the one the user last typed into. It stays plugged in permanently, next to a bench where other ESP32-C3 SuperMinis get flashed all the time, and it must never be overwritten by one of those uploads.

## Constraints
- Hardware: ESP32-C3 SuperMini plus one WS2812D-F5 (5 mm through-hole, GRB). DIN goes to GPIO4 (the user moved it from GPIO3 after the lamp stuck at full blue on GPIO3), VDD to 5V (the part needs 3.7 V minimum, so not 3V3), GND to G. The 3.3 V data line is fine (V_IH min 2.7 V).
- The C3 only has the fixed-function USB Serial/JTAG. Every C3 enumerates as `303A:1001 "USB JTAG/serial debug unit"`, so the eatingVoltage naming rule (device string + CDC function string) **cannot be applied on this chip**. The only unique identifier is the USB serial number, which is the MAC. This lamp is `48:F6:EE:13:EC:44`.
- Any host can reset a stock C3 into ROM download mode over USB. Firmware can't refuse that; only an eFuse can.
- PlatformIO, given two `303A:1001` ports, takes the one whose COM name sorts **last as a string** (`finder.py::_reveal_device_port`). The lamp is on COM3, and "COM3" sorts after "COM12" and "COM24".
- Claude must never be slowed or broken by the lamp: hooks run async and fail silently.

## Key decisions
- Decision: protect with port lock + eFuse. Reason: the bridge holds the COM port open, so a stray upload gets "access denied"; `DIS_USB_SERIAL_JTAG_DOWNLOAD_MODE` covers the times the bridge isn't running. Alternative considered: port lock only (reversible, but unprotected whenever the bridge is down); a different board such as an RP2040-Zero (nameable USB, onboard LED). Rejected because the C3 is already built and costs about €3.
- Decision: move the lamp to COM1 in Device Manager. Reason: "COM1" sorts first, so PlatformIO autodetect always prefers the dev board over the lamp, and dev uploads stop failing on the locked lamp port.
- Decision: the eFuse is burned only after a serial self-update round trip has worked once, and only on the user's explicit go. Reason: it's a one-way door.
- Decision: updates after the fuse go through a serial self-update (`clampel flash`) into the second OTA slot, with bootloader rollback if the new image hasn't heard `?` from the bridge within 30 s. Last-resort recovery is a USB-UART adapter on GPIO20/21 with BOOT held, so those pins stay free. Alternative considered: UART adapter only, or never update.
- Decision: permission prompts and questions get their own state, amber blink. Reason: "blocked on you halfway through" is the costliest state to miss and must not look like "done".
- Decision: multi-session handling follows the session with the latest prompt submit, **except** that amber from any session breaks through. Alternative considered: strictly last input (a blocked background session stays invisible); worst-of-all (loses "last input").
- Decision: bright green for 10 min after a turn finishes, then dim green while the session stays open; off means no live session. Reason: the lamp neither lies nor shouts for hours.
- Decision (added later the same day): new state **credits**, a slow blue "sleeping" breath. It's entered via `StopFailure` with `error` = `rate_limit`/`billing_error`, and left via `quota_auto_resume_fired` (think), `_stale` (ask), `_disabled` (done), a new prompt, or Esc. Priority is ask > credits > last input, because limits are per account. Credits stops overriding once another session gets work through after it. Firmware update moved to cyan and boot blip to white, so blue only means credits.
- Decision: Claude-only, but the protocol is open. The `clampel` CLI (test, brightness, status, flash) and the serial protocol are documented. Other sources are not supported yet.
- Decision: logic lives on the host (session tracking, decay, selection). The firmware only renders a mode, goes dark after 5 s without host traffic (bridge dead or PC asleep), and handles updates.
- Decision: no housing. Paint over the red power LED (black nail polish or paint marker, reversible with acetone). Firmware keeps the blue GPIO8 LED dark.

## Surfaced assumptions
- "Last input" means the last prompt *submitted*, not the last window focused.
- "No Claude" has to survive sessions that die without `SessionEnd` (VS Code killed, crash). The bridge checks whether the owning `claude.exe` PID is still alive.
- Pressing Esc might not fire `Stop`. The bridge reads the transcript tail for `[Request interrupted by user` so the lamp doesn't pulse red forever.
- After a permission prompt is approved, no `UserPromptSubmit` follows. `PostToolUse` switches the lamp back to "think".

## Open questions
- (Answered 2026-10-12, after the burn) When an uploader resets a fused lamp into download mode, the ROM sits there until a real reset, with USB not drained. The bridge now detects that and hard-resets via RTS, rewriting DTR after each RTS change because Windows usbser.sys needs it.
- The right brightness (default 60 %) can only be judged after a few days of real use.

## Out of scope
- Housing (decided against for now).
- Other signal sources (builds, CI) feeding the lamp.
- Using the BOOT button for anything.
- Renaming the C3's USB identity (impossible in silicon).
