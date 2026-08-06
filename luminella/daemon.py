"""Daemon that owns the Luminella serial port and serves Claude Code hooks.

Only one process can usefully hold the port, and hooks fire far too often to
each pay the open+handshake cost. So the daemon keeps the port open, renders
the LED animation, reads switch events, and exposes a newline-delimited JSON
protocol over a unix socket:

    {"cmd": "state",  "state": "busy"}          -> {"ok": true}
    {"cmd": "ask",    "tool": "Bash", "timeout": 30}
                                                -> {"decision": "allow"|"deny"|"timeout"}
    {"cmd": "ping"}                             -> {"ok": true, "device": true}
    {"cmd": "quit"}                             -> {"ok": true}

Note: this conflicts with Orbital2 Core / Luminella Core, which hold the same
port. Only one of them can run at a time.
"""

import json
import math
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from luminella import config, protocol


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}\n"
    try:
        with open(config.LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


class Daemon:
    def __init__(self, cfg):
        self.cfg = cfg
        self.ser = None
        self.write_lock = threading.Lock()
        self.state = "idle"
        self.state_lock = threading.Lock()
        self.revert_at = None       # timestamp to auto-return to idle
        self.revert_to = "idle"
        self.listeners = []         # list of (queue-ish list, event)
        self.listeners_lock = threading.Lock()
        self.running = True
        self.last_rgb = None
        self.ptt = None
        self.on_state_change = None
        self.stick_engaged = False
        self.last_stick = None

    # ---- serial ---------------------------------------------------------

    def connect(self):
        try:
            self.ser = protocol.open_port(self.cfg["port"])
        except Exception as exc:
            self.ser = None
            log(f"open failed: {exc}")
            return False
        self.last_rgb = None
        ok, raw = protocol.handshake(self.ser)
        log(f"handshake {'ok' if ok else 'no reply'} raw={raw!r}")
        return ok

    def write_rgb(self, rgb):
        if rgb == self.last_rgb:
            return
        with self.write_lock:
            try:
                self.ser.write(protocol.led(*rgb))
                self.ser.flush()
                self.last_rgb = rgb
            except Exception as exc:  # port yanked, device unplugged
                log(f"write failed: {exc}")
                self.running = False

    # ---- input ----------------------------------------------------------

    def reader_loop(self):
        buf = b""
        while self.running:
            try:
                chunk = self.ser.read(256)
            except Exception as exc:
                log(f"read failed: {exc}")
                self.running = False
                return
            if not chunk:
                continue
            buf += chunk
            frames, buf = protocol.parse(buf)
            for tag, payload in frames:
                if tag == b"JS" and len(payload) == 4:
                    # "X" <x> "Y" <y>, centre 0x80. The stick reports a level
                    # rather than an edge, so a dropped frame self-corrects on
                    # the next one -- unlike a switch, whose release can be
                    # lost for good.
                    self.on_stick(payload[1] - 128, payload[3] - 128)
                    continue
                if tag != b"SW":
                    continue
                text = payload.decode("ascii", "replace")
                if "=" not in text:
                    continue
                number, _, value = text.partition("=")
                self.dispatch(number, value)

    def on_stick(self, x, y):
        """Drive push-to-talk from stick deflection, with hysteresis."""
        if not self.ptt or self.cfg.get("ptt_mode") != "stick":
            return
        magnitude = max(abs(x), abs(y))
        if magnitude != self.last_stick:
            self.last_stick = magnitude
            log("stick %d (x=%d y=%d)" % (magnitude, x, y))
        on_at = int(self.cfg.get("ptt_stick_on", 45))
        off_at = int(self.cfg.get("ptt_stick_off", 20))
        if not self.stick_engaged and magnitude >= on_at:
            self.stick_engaged = True
            self.ptt.start()
        elif self.stick_engaged and magnitude <= off_at:
            self.stick_engaged = False
            self.ptt.stop()

    def dispatch(self, switch, value):
        with self.listeners_lock:
            for box, event in self.listeners:
                box.append((switch, value))
                event.set()

        # Push-to-talk is driven by the device, not by a hook, so it is
        # handled here rather than over the socket.
        ptt_switch = str(self.cfg.get("ptt_switch") or "")
        log("sw %s=%s (ptt=%r)" % (switch, value, ptt_switch))
        if self.ptt and ptt_switch and str(switch) == ptt_switch:
            if value == "1":
                # The device sometimes drops the release edge, so a press while
                # already recording ends it rather than being ignored: hold to
                # talk normally, press again to stop if the release went astray.
                if self.ptt.is_recording():
                    log("ptt: second press ends recording (release was lost)")
                    self.ptt.stop()
                else:
                    self.ptt.start()
            else:
                self.ptt.stop()

    # ---- animation ------------------------------------------------------

    def current(self):
        with self.state_lock:
            if self.revert_at and time.time() >= self.revert_at:
                self.state = self.revert_to
                self.revert_at = None
            return self.state

    def set_state(self, state, revert_to=None, after=None):
        with self.state_lock:
            self.state = state
            if after:
                self.revert_at = time.time() + after
                self.revert_to = revert_to or "idle"
            else:
                self.revert_at = None

    def animate_loop(self):
        period = 1.0 / max(1, self.cfg["fps"])
        previous = None
        while self.running:
            name = self.current()
            if name != previous:
                previous = name
                if self.on_state_change:
                    try:
                        self.on_state_change(name)
                    except Exception as exc:
                        log("state-change handler failed: %s" % exc)
            spec = self.cfg["states"].get(name, self.cfg["states"]["idle"])
            r, g, b = spec["color"]
            mode = spec.get("mode", "solid")
            t = time.time()

            if mode == "breathe":
                # 2.4s sine, floored at 15% so the ring never fully drops out
                k = 0.15 + 0.85 * (0.5 + 0.5 * math.sin(2 * math.pi * t / 2.4))
            elif mode == "blink":
                k = 1.0 if (t % 0.7) < 0.35 else 0.0
            else:
                k = 1.0

            self.write_rgb((int(r * k), int(g * k), int(b * k)))
            time.sleep(period)

    # ---- ask ------------------------------------------------------------

    def read_switch(self, timeout):
        """Wait for any switch press and report its number. Used for mapping."""
        box, event = [], threading.Event()
        with self.listeners_lock:
            self.listeners.append((box, event))
        try:
            deadline = time.time() + timeout
            while time.time() < deadline:
                if event.wait(min(0.25, max(0.01, deadline - time.time()))):
                    event.clear()
                    while box:
                        switch, value = box.pop(0)
                        if value == "1":
                            return switch
            return None
        finally:
            with self.listeners_lock:
                self.listeners = [(b, e) for b, e in self.listeners if b is not box]

    def read_hold(self, timeout):
        """Wait for a press, then confirm the switch also reports its release.

        Returns (switch, has_release). Push-to-talk needs the release edge to
        know when to stop; at least one switch on the device only ever sends
        the press.
        """
        box, event = [], threading.Event()
        with self.listeners_lock:
            self.listeners.append((box, event))
        try:
            deadline = time.time() + timeout
            pressed = None
            while time.time() < deadline:
                if not event.wait(min(0.25, max(0.01, deadline - time.time()))):
                    continue
                event.clear()
                while box:
                    switch, value = box.pop(0)
                    if pressed is None and value == "1":
                        pressed = switch
                        # Give the release a moment to arrive.
                        deadline = min(deadline, time.time() + 3.0)
                    elif pressed is not None and switch == pressed and value == "0":
                        return pressed, True
            return pressed, False
        finally:
            with self.listeners_lock:
                self.listeners = [(b, e) for b, e in self.listeners if b is not box]

    def ask(self, timeout):
        box, event = [], threading.Event()
        with self.listeners_lock:
            self.listeners.append((box, event))

        previous = self.current()
        self.set_state("ask")
        approve = str(self.cfg["approve_switch"])
        deny = str(self.cfg["deny_switch"])
        deadline = time.time() + timeout
        decision = "timeout"
        try:
            while time.time() < deadline:
                if not event.wait(min(0.25, max(0.01, deadline - time.time()))):
                    continue
                event.clear()
                while box:
                    switch, value = box.pop(0)
                    if value != "1":
                        continue
                    if switch == approve:
                        decision = "allow"
                        break
                    if switch == deny:
                        decision = "deny"
                        break
                if decision != "timeout":
                    break
        finally:
            with self.listeners_lock:
                self.listeners = [(b, e) for b, e in self.listeners if b is not box]

        if decision == "allow":
            self.set_state("done", revert_to=previous, after=0.6)
        elif decision == "deny":
            self.set_state("error", revert_to=previous, after=0.6)
        else:
            self.set_state(previous)
        return decision

    # ---- socket ---------------------------------------------------------

    def handle(self, conn):
        conn.settimeout(300)
        try:
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                data += chunk
            req = json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
            cmd = req.get("cmd")

            if cmd == "ping":
                reply = {"ok": True, "device": bool(self.ser)}
            elif cmd == "state":
                name = req.get("state", "idle")
                after = req.get("after")
                self.set_state(name, revert_to=req.get("revert_to", "idle"), after=after)
                reply = {"ok": True, "state": name}
            elif cmd == "ask":
                timeout = float(req.get("timeout", self.cfg["ask_timeout"]))
                reply = {"decision": self.ask(timeout)}
            elif cmd == "readhold":
                switch, has_release = self.read_hold(float(req.get("timeout", 20)))
                reply = {"switch": switch, "has_release": has_release}
            elif cmd == "readsw":
                timeout = float(req.get("timeout", 15))
                reply = {"switch": self.read_switch(timeout)}
            elif cmd == "quit":
                reply = {"ok": True}
                self.running = False
            else:
                reply = {"ok": False, "error": f"unknown cmd {cmd!r}"}

            conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
        except Exception as exc:
            log(f"handler error: {exc}")
            try:
                conn.sendall((json.dumps({"ok": False, "error": str(exc)}) + "\n").encode())
            except OSError:
                pass
        finally:
            conn.close()

    def serve(self):
        os.makedirs(config.STATE_DIR, exist_ok=True)
        if os.path.exists(config.SOCKET_PATH):
            os.unlink(config.SOCKET_PATH)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(config.SOCKET_PATH)
        os.chmod(config.SOCKET_PATH, 0o600)
        srv.listen(16)
        srv.settimeout(0.5)
        log(f"listening on {config.SOCKET_PATH}")
        while self.running:
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self.handle, args=(conn,), daemon=True).start()
        srv.close()
        try:
            os.unlink(config.SOCKET_PATH)
        except OSError:
            pass

    def run(self):
        if not self.connect():
            log("device did not answer handshake -- is Orbital2 Core holding the port?")
        threading.Thread(target=self.reader_loop, daemon=True).start()
        threading.Thread(target=self.animate_loop, daemon=True).start()
        try:
            self.serve()
        finally:
            self.running = False
            time.sleep(0.15)
            try:
                self.write_rgb((0, 0, 0))
                self.ser.close()
            except Exception:
                pass
            log("stopped")


def main():
    os.makedirs(config.STATE_DIR, exist_ok=True)
    cfg = config.load()
    Daemon(cfg).run()


if __name__ == "__main__":
    main()
