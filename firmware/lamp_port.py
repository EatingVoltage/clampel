# Pin UPLOAD_PORT / MONITOR_PORT to the lamp by its USB serial number (MAC).
#
# Every ESP32-C3 is 303A:1001, so PlatformIO's autodetect can't tell the lamp
# from a dev board. If the lamp isn't plugged in, an upload must fail here,
# not fall through to autodetect and overwrite some other board.
import json
import os

from serial.tools import list_ports

Import("env")  # noqa: F821  (SCons global)

cfg = os.path.join(env.subst("$PROJECT_DIR"), "..", "lamp.json")  # noqa: F821
try:
    with open(cfg) as f:
        serial = json.load(f)["serial"].upper()
except (OSError, ValueError, KeyError):
    serial = None  # not paired yet: building is fine, uploading isn't

port = serial and next(
    (p.device for p in list_ports.comports() if (p.serial_number or "").upper() == serial),
    None,
)

if port:
    env.Replace(UPLOAD_PORT=port, MONITOR_PORT=port)  # noqa: F821
    print("clampel: lamp %s is on %s" % (serial, port))
elif any(t in COMMAND_LINE_TARGETS for t in ("upload", "monitor")):  # noqa: F821
    if serial:
        print("clampel: lamp %s not found, refusing to autodetect a port" % serial)
    else:
        print("clampel: no lamp paired. Plug in only the lamp, run `python host/clampel.py pair`, retry.")
    env.Exit(1)  # noqa: F821
