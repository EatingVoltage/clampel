#!/usr/bin/env python3
"""clampel: Claude status lamp. Hook entry point, bridge daemon and CLI in one file.

    clampel hook <start|input|think|ask|done|fail|notify|end>
                                                     called by Claude Code hooks (stdin = hook JSON)
    clampel bridge                                   run the bridge in the foreground
    clampel start | stop                             start the bridge in the background / stop it
    clampel status                                   sessions, chosen state, lamp info
    clampel test <off|idle|done|think|ask|credits> [sec]   force a state for a few seconds
    clampel brightness <1-100>                       master brightness, stored on the lamp
    clampel raw "<line>"                             send one protocol line, print the replies
    clampel flash [firmware.bin]                     serial self-update (the only way after the eFuse)
    clampel fuse                                     burn DIS_USB_SERIAL_JTAG_DOWNLOAD_MODE (one-way)
    clampel install                                  start the bridge at logon (Startup folder shortcut)

Hooks only write one small JSON file per session. The bridge owns the COM port
(holding it open is half of the overwrite protection), decides which session
the lamp shows, and keeps the lamp's 5-second watchdog fed.
"""
import functools
import hashlib
import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time

ROOT       = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAMP_CFG   = os.path.join(ROOT, "lamp.json")
FIRMWARE   = os.path.join(ROOT, "firmware", ".pio", "build", "clampel", "firmware.bin")
STATE_DIR  = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "clampel")
SESS_DIR   = os.path.join(STATE_DIR, "sessions")
LOG_PATH   = os.path.join(STATE_DIR, "bridge.log")
CTRL       = ("127.0.0.1", 47811)

DONE_DECAY_S = 600          # bright green → dim green
STALE_S      = 12 * 3600    # session file with no owning process found
TICK_S       = 0.2
HELLO_S      = 5.0          # re-send `?`: keeps lamp info fresh, verifies a fresh OTA image
STATES       = ("off", "idle", "done", "think", "ask", "credits")
HOOK_STATE   = {"start": "idle", "input": "think", "think": "think", "ask": "ask", "done": "done"}
# `hook notify`: what each Notification type means for the lamp (others are ignored).
NOTIFY_STATE = {"permission_prompt": "ask", "elicitation_dialog": "ask",
                "elicitation_url_dialog": "ask", "agent_needs_input": "ask",
                "quota_auto_resume_fired": "think",     # limit reset, Claude carries on
                "quota_auto_resume_stale": "ask",       # reset while asleep: waits for Enter
                "quota_auto_resume_disabled": "done"}   # gave up waiting, your move
# `hook fail` (StopFailure): these errors mean "out of usage until the reset", the rest just end the turn.
CREDIT_ERRORS = ("rate_limit", "billing_error")


# ── small helpers ────────────────────────────────────────────────────────────

def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_json(path, obj):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f)
    for _ in range(20):                     # the bridge may be reading it right now
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.01)
    os.remove(tmp)


def remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def lamp_serial():
    return read_json(LAMP_CFG)["serial"].upper()


def log(msg):
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1_000_000:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


# ── Windows process table (no psutil) ────────────────────────────────────────

@functools.lru_cache(maxsize=None)
def _k32():
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    return ctypes, wintypes, k32


def process_table():
    """{pid: (parent_pid, exe_lower)}"""
    ctypes, wintypes, k32 = _k32()

    class PE(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                    ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_wchar * 260)]

    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PE)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PE)]
    snap = k32.CreateToolhelp32Snapshot(2, 0)          # TH32CS_SNAPPROCESS
    pe = PE()
    pe.dwSize = ctypes.sizeof(PE)
    table = {}
    ok = k32.Process32FirstW(snap, ctypes.byref(pe))
    while ok:
        table[pe.th32ProcessID] = (pe.th32ParentProcessID, pe.szExeFile.lower())
        ok = k32.Process32NextW(snap, ctypes.byref(pe))
    k32.CloseHandle(snap)
    return table


def process_ctime(pid):
    """Creation time as an int, or None if the process is gone. Guards against PID reuse."""
    ctypes, wintypes, k32 = _k32()
    h = k32.OpenProcess(0x1000, False, pid)             # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
            return None
        c, e, kt, ut = (wintypes.FILETIME() for _ in range(4))
        if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kt), ctypes.byref(ut)):
            return None
        return (c.dwHighDateTime << 32) | c.dwLowDateTime
    finally:
        k32.CloseHandle(h)


def find_claude_process():
    """Walk up from this hook process to the claude.exe that owns the session."""
    table = process_table()
    pid = os.getpid()
    for _ in range(12):
        entry = table.get(pid)
        if not entry:
            break
        if entry[1] == "claude.exe":
            return pid, process_ctime(pid)
        pid = entry[0]
    return None, None


# ── hook ─────────────────────────────────────────────────────────────────────

def cmd_hook(event):
    t = time.time()
    try:
        data = json.loads(sys.stdin.buffer.read())     # bytes: Windows stdin defaults to cp1252
    except ValueError:
        return
    sid = "".join(c for c in str(data.get("session_id", "")) if c.isalnum() or c in "-_")
    if event == "fail":
        state = "credits" if data.get("error") in CREDIT_ERRORS else "done"
    elif event == "notify":
        state = NOTIFY_STATE.get(data.get("notification_type"))
    else:
        state = HOOK_STATE.get(event)
    if not sid or not state and event != "end":
        return
    os.makedirs(SESS_DIR, exist_ok=True)
    path = os.path.join(SESS_DIR, sid + ".json")

    if event == "end":
        remove(path)
        return

    s = read_json(path) or {}
    if s.get("ts", 0) > t:                  # hooks run async: a later event already landed
        return
    s.update(ts=t, state=state, why=data.get("error") or data.get("notification_type") or event,
             transcript=data.get("transcript_path"), cwd=data.get("cwd"))
    if event == "input":
        s["last_input"] = t
    if not s.get("pid"):
        s["pid"], s["ctime"] = find_claude_process()
    write_json(path, s)

    if event in ("start", "input"):
        ensure_bridge()


# ── session → lamp state ─────────────────────────────────────────────────────

_interrupt_cache = {}


def interrupted(transcript):
    """True if the transcript's last user/assistant entry is an Esc interrupt.
    Esc doesn't reliably fire Stop, and without this the lamp would pulse forever."""
    if not transcript:
        return False
    try:
        st = os.stat(transcript)
    except OSError:
        return False
    key = (st.st_mtime_ns, st.st_size)
    hit = _interrupt_cache.get(transcript)
    if hit and hit[0] == key:
        return hit[1]

    result = False
    try:
        with open(transcript, "rb") as f:
            f.seek(max(0, st.st_size - 65536))
            lines = f.read().decode("utf-8", "replace").splitlines()[1:]
        for raw in reversed(lines):
            try:
                e = json.loads(raw)
            except ValueError:
                continue
            if e.get("type") not in ("user", "assistant"):
                continue
            if e.get("type") == "user":
                content = (e.get("message") or {}).get("content")
                texts = [content] if isinstance(content, str) else \
                        [b.get("text", "") for b in content or [] if isinstance(b, dict)]
                result = any(x.startswith("[Request interrupted by user") for x in texts)
            break
    except OSError:
        pass
    _interrupt_cache[transcript] = (key, result)
    return result


def load_sessions(now):
    sessions = []
    try:
        names = os.listdir(SESS_DIR)
    except OSError:
        return sessions
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(SESS_DIR, name)
        s = read_json(path)
        if not s:
            continue
        pid = s.get("pid")
        if pid:
            alive = process_ctime(pid)
            if alive is None or (s.get("ctime") and alive != s["ctime"]):
                remove(path)                # Claude died without SessionEnd
                continue
        elif now - s.get("ts", 0) > STALE_S:
            remove(path)
            continue
        state = s.get("state", "idle")
        if state in ("think", "ask", "credits") and interrupted(s.get("transcript")):
            state = "idle"
        if state == "done" and now - s.get("ts", 0) > DONE_DECAY_S:
            state = "idle"
        s["id"], s["effective"] = name[:-5], state
        sessions.append(s)
    return sessions


def choose(sessions):
    """Amber from any session wins, then blue (credits); otherwise follow the last prompt submitted.

    Usage limits are per account, so one session waiting for credits means all of
    them are. That holds only until some session gets work through again (think
    or done after the credits event); then a stale blue stops overriding."""
    if not sessions:
        return "off", None
    asking = [s for s in sessions if s["effective"] == "ask"]
    if asking:
        return "ask", asking[0]
    waiting = [s for s in sessions if s["effective"] == "credits"]
    if waiting:
        newest = max(waiting, key=lambda s: s.get("ts", 0))
        if not any(s["effective"] in ("think", "done") and s.get("ts", 0) > newest.get("ts", 0)
                   for s in sessions):
            return "credits", newest
    cur = max(sessions, key=lambda s: (s.get("last_input") or 0, s.get("ts", 0)))
    return cur["effective"], cur


# ── bridge ───────────────────────────────────────────────────────────────────

class Lamp:
    def __init__(self, serial_no):
        import serial  # noqa: F401  (fail early if pyserial is missing)
        self.sn       = serial_no
        self.ser      = None
        self.lock     = threading.RLock()
        self.info     = None
        self.sent     = None
        self.last_tx  = 0.0
        self.last_try = 0.0
        self.last_hi  = 0.0
        self.last_seen  = 0.0               # last CLAMPEL hello from the lamp
        self.last_reset = 0.0
        self.buf      = b""
        self.override = None                # (state, until)
        self.shown    = "off"
        self.paused   = False               # True while flashing / fusing

    def find_port(self):
        from serial.tools import list_ports
        for p in list_ports.comports():
            if (p.serial_number or "").upper() == self.sn:
                return p.device
        return None

    def connect(self):
        import serial
        dev = self.find_port()
        if not dev:
            return False
        s = serial.Serial()
        s.port, s.baudrate, s.timeout, s.write_timeout = dev, 115200, 0, 2
        s.dtr = s.rts = False               # set BEFORE open: DTR/RTS edges can reset a C3
        s.open()
        self.ser, self.buf, self.sent = s, b"", None
        self.last_seen = time.time()        # grace period starts at connect
        try:
            self.info = self.query("?", ("CLAMPEL",), 2.0)
        except serial.SerialTimeoutException:
            # Nothing on the lamp drains USB: it sits in ROM download mode, which is
            # where any uploader leaves a fused lamp (the ROM then just waits).
            self.revive("hello write timed out")
            raise
        self.last_hi = time.time()
        log("connected %s: %s" % (dev, self.info))
        return True

    def revive(self, why):
        """Hard-reset the lamp through the open port, at most once a minute."""
        now = time.time()
        if now - self.last_reset < 60:
            return
        log("lamp %s, hard reset" % why)
        try:
            self.ser.reset_output_buffer()  # drop the write that timed out
        except Exception:
            pass
        pulse_reset(self.ser)
        self.last_reset = self.last_seen = now

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        self.ser, self.sent = None, None

    def send(self, line):
        self.ser.write((line + "\n").encode())
        self.last_tx = time.time()

    def lines(self):
        """Non-blocking: complete lines received so far."""
        n = self.ser.in_waiting
        if n:
            self.buf += self.ser.read(n)
        out = []
        while b"\n" in self.buf:
            raw, self.buf = self.buf.split(b"\n", 1)
            text = raw.decode("ascii", "replace").strip()
            if text.startswith("CLAMPEL"):
                self.info = text
                self.last_seen = time.time()
            if text:
                out.append(text)
        return out

    def expect(self, prefixes, timeout):
        end = time.time() + timeout
        while time.time() < end:
            for text in self.lines():
                if text.startswith(prefixes):
                    return text
                if text != "OK":
                    log("lamp: " + text)
            time.sleep(0.005)
        return None

    def query(self, line, prefixes, timeout=2.0):
        self.lines()
        self.send(line)
        return self.expect(prefixes, timeout)

    def tick(self):
        import serial
        now = time.time()
        with self.lock:
            if self.paused:
                return
            if not self.ser:
                if now - self.last_try < 2.0:
                    return
                self.last_try = now
                try:
                    self.connect()
                except (serial.SerialException, OSError) as e:
                    log("connect failed: %s" % e)
                    self.close()
                return
            try:
                for text in self.lines():
                    if text != "OK" and not text.startswith("CLAMPEL"):
                        log("lamp: " + text)
                if self.override and now < self.override[1]:
                    state, why = self.override[0], "test"
                else:
                    self.override = None
                    state, cur = choose(load_sessions(now))
                    why = "%s via %s" % (cur["id"][:8], cur.get("why")) if cur else "no sessions"
                if state != self.shown:
                    log("lamp %s -> %s (%s)" % (self.shown, state, why))
                self.shown = state
                if state != self.sent or now - self.last_tx > 1.0:   # doubles as watchdog heartbeat
                    self.send("S " + state)
                    self.sent = state
                if now - self.last_hi > HELLO_S:
                    self.send("?")
                    self.last_hi = now
                # Port open but no hello for 15 s: firmware hung, or stuck in ROM.
                if now - self.last_seen > 15:
                    self.revive("silent for %ds" % (now - self.last_seen))
            except (serial.SerialException, OSError) as e:
                log("lamp lost: %s" % e)
                self.close()

    def flash(self, data, progress):
        import serial
        md5 = hashlib.md5(data).hexdigest()
        with self.lock:
            if not self.ser:
                raise RuntimeError("lamp not connected")
            self.paused = True
            try:
                r = self.query("U %d %s" % (len(data), md5), ("READY", "ERR"), 5)
                if r != "READY":
                    raise RuntimeError(r or "no answer to U")
                for off in range(0, len(data), 1024):
                    chunk = data[off:off + 1024]
                    self.ser.write(chunk)
                    r = self.expect(("ACK", "ERR"), 10)
                    if not r or not r.startswith("ACK"):
                        raise RuntimeError(r or "no ACK at %d" % off)
                    progress(off + len(chunk), len(data))
                r = self.expect(("DONE", "ERR"), 20)
                if r != "DONE":
                    raise RuntimeError(r or "no DONE")
            finally:
                self.close()
                self.info = None
        # The lamp reboots into the new image. Whether or not Windows re-enumerates
        # the port, reconnecting sends `?`, which marks the image valid.
        time.sleep(2.0)
        end = time.time() + 45              # > the lamp's 30 s verify window, so a rollback is seen too
        while time.time() < end:
            with self.lock:
                try:
                    if self.ser or self.connect():
                        if self.info and "valid" in self.info:
                            self.paused = False
                            return self.info
                        self.info = self.query("?", ("CLAMPEL",), 2.0)
                except (serial.SerialException, OSError):
                    self.close()
            time.sleep(1.0)
        self.paused = False
        raise RuntimeError("lamp did not come back within 45 s")


class Ctrl(socketserver.StreamRequestHandler):
    def reply(self, **kw):
        self.wfile.write((json.dumps(kw) + "\n").encode())
        self.wfile.flush()

    def handle(self):
        lamp = self.server.lamp
        raw = self.rfile.readline().strip()
        if not raw:                         # bridge_running() probe: connect, close
            return
        try:
            req = json.loads(raw.decode())
            cmd = req.get("cmd")
            if cmd == "status":
                now = time.time()
                sessions = load_sessions(now)
                state, cur = choose(sessions)
                self.reply(ok=True, state=lamp.shown, chosen=cur and cur["id"],
                           port=lamp.ser.port if lamp.ser else None, info=lamp.info,
                           sessions=[{k: s.get(k) for k in ("id", "effective", "state", "why", "cwd", "ts", "last_input", "pid")}
                                     for s in sessions])
            elif cmd == "test":
                lamp.override = (req["state"], time.time() + float(req.get("seconds", 8)))
                self.reply(ok=True)
            elif cmd in ("brightness", "raw"):
                line = "B %d" % int(req["value"]) if cmd == "brightness" else req["line"]
                with lamp.lock:
                    if not lamp.ser:
                        raise RuntimeError("lamp not connected")
                    lamp.lines()
                    lamp.send(line)
                    time.sleep(0.3)
                    self.reply(ok=True, lines=lamp.lines())
            elif cmd == "flash":
                with open(req["path"], "rb") as f:
                    data = f.read()
                last = [0]

                def progress(done, total):
                    pct = done * 100 // total
                    if pct >= last[0] + 5 or done == total:
                        last[0] = pct
                        self.reply(progress=pct)
                before = lamp.info
                self.reply(ok=True, info=lamp.flash(data, progress), before=before)
            elif cmd == "stop":
                with lamp.lock:
                    lamp.paused = True
                    lamp.close()
                self.reply(ok=True)
                log("stopped by client")
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.reply(ok=False, error="unknown cmd %r" % cmd)
        except Exception as e:
            log("ctrl %r failed: %s" % (self.rfile, e))
            try:
                self.reply(ok=False, error=str(e))
            except OSError:
                pass


def cmd_bridge():
    os.makedirs(SESS_DIR, exist_ok=True)
    socketserver.ThreadingTCPServer.allow_reuse_address = False
    socketserver.ThreadingTCPServer.daemon_threads = True
    try:
        server = socketserver.ThreadingTCPServer(CTRL, Ctrl)   # doubles as the single-instance lock
    except OSError:
        print("clampel: bridge already running")
        return
    lamp = Lamp(lamp_serial())
    server.lamp = lamp
    log("bridge up, pid %d, lamp %s" % (os.getpid(), lamp.sn))

    def loop():
        while True:
            lamp.tick()
            time.sleep(TICK_S)

    threading.Thread(target=loop, daemon=True).start()
    server.serve_forever()
    log("bridge down")


def bridge_running():
    try:
        socket.create_connection(CTRL, timeout=0.3).close()
        return True
    except OSError:
        return False


def ensure_bridge():
    if bridge_running():
        return
    os.makedirs(SESS_DIR, exist_ok=True)
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(exe):
        exe = sys.executable
    args = [exe, "-I", os.path.abspath(__file__), "bridge"]
    base = 0x00000008 | 0x00000200                  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    for flags in (base | 0x01000000, base):         # try CREATE_BREAKAWAY_FROM_JOB first: outlive the hook
        try:
            subprocess.Popen(args, creationflags=flags, close_fds=True, cwd=STATE_DIR,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            continue


# ── CLI ──────────────────────────────────────────────────────────────────────

def ctrl(req):
    """Yield every JSON line the bridge sends back for one request."""
    try:
        s = socket.create_connection(CTRL, timeout=5)
    except OSError:
        sys.exit("clampel: bridge not running (run `clampel start`)")
    s.settimeout(120)
    s.sendall((json.dumps(req) + "\n").encode())
    with s, s.makefile("r", encoding="utf-8") as f:
        for line in f:
            yield json.loads(line)


def ctrl_one(req):
    msg = next(ctrl(req))
    if not msg.get("ok"):
        sys.exit("clampel: " + msg.get("error", "failed"))
    return msg


def cmd_status():
    m = ctrl_one({"cmd": "status"})
    print("lamp   : %s  %s" % (m["port"] or "not connected", m["info"] or ""))
    print("shows  : %s" % m["state"])
    now = time.time()
    for s in sorted(m["sessions"], key=lambda s: -(s.get("last_input") or 0)):
        mark = "*" if s["id"] == m["chosen"] else " "
        li = "%4ds ago" % (now - s["last_input"]) if s.get("last_input") else "  never "
        print(" %s %-7s input %s  %-24s %s  (%s)" % (mark, s["effective"], li, "via " + str(s.get("why")),
                                                     s.get("cwd") or "?", s["id"][:8]))
    if not m["sessions"]:
        print("  no live Claude sessions")


def cmd_flash(path):
    path = os.path.abspath(path or FIRMWARE)
    if not os.path.exists(path):
        sys.exit("clampel: %s not found (build with `pio run` in firmware/)" % path)
    print("flashing %s (%d bytes)" % (path, os.path.getsize(path)))
    for msg in ctrl({"cmd": "flash", "path": path}):
        if "progress" in msg:
            print("  %3d%%" % msg["progress"])
        elif msg.get("ok"):
            print("done: %s" % msg["info"])
            if msg["info"] == msg.get("before"):
                print("note: same version string as before. If you expected a new one, the new image "
                      "never confirmed itself and the bootloader rolled back.")
        else:
            sys.exit("clampel: flash failed: %s" % msg.get("error"))


def cmd_stop():
    if bridge_running():
        ctrl_one({"cmd": "stop"})
        print("bridge stopped, COM port released")
    else:
        print("bridge not running")


def cmd_fuse():
    """Stop the bridge, burn the eFuse (espefuse asks you to type BURN), reset, restart.

    Needs `python -m pip install esptool "bitstring<5"` for this interpreter:
    PlatformIO's bundled espefuse lacks `cryptography`, and bitstring 5 dropped the
    BitStream class espefuse imports (both seen 2026-10-12, esptool 5.5.0).
    The lamp 48:F6:EE:13:EC:44 was fused on 2026-10-12; this is for a new lamp."""
    import importlib.util
    from serial.tools import list_ports
    if importlib.util.find_spec("espefuse") is None:
        sys.exit('clampel: espefuse missing; run: python -m pip install esptool "bitstring<5"')
    sn = lamp_serial()
    print("This permanently disables flashing the lamp (%s) over USB." % sn)
    print("Afterwards only `clampel flash` or a USB-UART adapter on GPIO20/21 + BOOT can update it.")
    cmd_stop()
    time.sleep(1.0)
    port = next((p.device for p in list_ports.comports() if (p.serial_number or "").upper() == sn), None)
    if not port:
        sys.exit("clampel: lamp %s not found" % sn)
    subprocess.call([sys.executable, "-m", "espefuse", "--chip", "esp32c3", "--port", port,
                     "burn-efuse", "DIS_USB_SERIAL_JTAG_DOWNLOAD_MODE", "1"])
    reset_lamp(port)                        # espefuse leaves the chip in ROM download mode
    ensure_bridge()
    print("bridge restarted. Check with `clampel status`.")


def pulse_reset(s):
    """USB-Serial/JTAG hard reset on an open port: RTS high with DTR low, then release.
    Windows' usbser.sys only sends the control-line request when DTR is written,
    so DTR is rewritten after every RTS change (esptool does the same)."""
    s.dtr = False
    s.rts = True
    s.dtr = False
    time.sleep(0.2)
    s.rts = False
    s.dtr = False
    time.sleep(0.2)


def reset_lamp(port):
    import serial
    s = serial.Serial()
    s.port = port
    s.dtr = s.rts = False
    s.open()
    pulse_reset(s)
    s.close()


def cmd_install():
    startup = os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Start Menu\Programs\Startup")
    lnk = os.path.join(startup, "clampel bridge.lnk")
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
          "$s.TargetPath='%s';$s.Arguments='-I \"%s\" bridge';$s.WorkingDirectory='%s';$s.Save()"
          % (lnk, exe, os.path.abspath(__file__), ROOT))
    subprocess.check_call(["powershell", "-NoProfile", "-Command", ps])
    print("installed: %s" % lnk)


def main(argv):
    if len(argv) < 2:
        sys.exit(__doc__)
    cmd, args = argv[1], argv[2:]
    if cmd == "hook":
        try:
            cmd_hook(args[0] if args else "")
        except Exception:
            pass                            # a lamp must never break Claude
    elif cmd == "bridge":
        cmd_bridge()
    elif cmd == "start":
        ensure_bridge()
        print("bridge running" if (time.sleep(1.0) or bridge_running()) else "bridge failed to start, see %s" % LOG_PATH)
    elif cmd == "stop":
        cmd_stop()
    elif cmd == "status":
        cmd_status()
    elif cmd == "test" and args and args[0] in STATES:
        ctrl_one({"cmd": "test", "state": args[0], "seconds": float(args[1]) if len(args) > 1 else 8})
    elif cmd == "brightness" and args:
        print(" ".join(ctrl_one({"cmd": "brightness", "value": int(args[0])})["lines"]))
    elif cmd == "raw" and args:
        print("\n".join(ctrl_one({"cmd": "raw", "line": " ".join(args)})["lines"]))
    elif cmd == "flash":
        cmd_flash(args[0] if args else None)
    elif cmd == "fuse":
        cmd_fuse()
    elif cmd == "install":
        cmd_install()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
