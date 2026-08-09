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

from luminella import actions, config, protocol


def log(msg):
    line = f"{time.strftime('%H:%M:%S')} {msg}\n"
    try:
        with open(config.LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


# Which state wins when several sessions are in different ones. A session
# waiting on you outranks everything; nothing else stops work from happening.
STATE_RANK = {"ask": 0, "notify": 1, "error": 2, "done": 3, "busy": 4, "idle": 5, "off": 6}

# Sessions that stop reporting are dropped rather than left waiting forever --
# a crashed session should not pin the ring on "busy".
SESSION_TTL = 1800.0


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
        self.on_ask = None
        self.on_notify = None
        self.on_resolve_pid = None
        self.stick_engaged = False
        self.stick_direction = None
        self.stick_ready_at = 0.0
        self.last_stick = None
        self.session_pid = None
        self.busy_tool = None
        # session_id -> {cwd, state, tool, seen, revert_at, revert_to, pid}
        self.sessions = {}
        # Push-to-talk feedback is about the device in your hand, not about any
        # session, so it is held separately and shown ahead of them.
        self.local_state = None
        self.local_revert_at = None
        # Nobody is reading a status light in a dark room. Set while the
        # displays are asleep; the ring goes out and stays out.
        self.display_off = False

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

    @staticmethod
    def direction_of(x, y):
        """Which way the stick is pushed: up, down, left or right.

        Four sectors rather than eight -- the device reports eight, but on a
        stick this small the diagonals are hard to hit deliberately and easy
        to hit by accident.
        """
        if abs(x) >= abs(y):
            return "right" if x > 0 else "left"
        # Measured on the device: pushing away from you gives positive y.
        # (The opposite was assumed at first, which swapped up and down.)
        return "up" if y > 0 else "down"

    def on_stick(self, x, y):
        """Route stick deflection to whatever that direction is bound to.

        Direction is latched on the way out and only released once the stick
        comes back to centre, so a hold stays with the direction it started
        in even if the angle drifts.
        """
        if self.cfg.get("ptt_mode") != "stick":
            return
        magnitude = max(abs(x), abs(y))
        if magnitude != self.last_stick:
            self.last_stick = magnitude
        on_at = int(self.cfg.get("ptt_stick_on", 45))
        off_at = int(self.cfg.get("ptt_stick_off", 20))
        bindings = self.cfg.get("stick_actions") or {}

        if not self.stick_engaged and magnitude >= on_at:
            # A spring-loaded stick overshoots on the way back: let go of "up"
            # and it can swing past centre far enough to look like a
            # deliberate "down". Ignore anything that arrives too soon after
            # the previous gesture ended.
            if time.time() < self.stick_ready_at:
                return
            self.stick_engaged = True
            self.stick_direction = self.direction_of(x, y)
            action = bindings.get(self.stick_direction) or {}
            log("stick %s (x=%d y=%d) -> %s" % (
                self.stick_direction, x, y, action.get("type") or "なし"))
            if action.get("type") == "ptt":
                if self.ptt:
                    self.ptt.start()
            elif action.get("type"):
                threading.Thread(
                    target=actions.perform,
                    args=(action, self.session_pid, log),
                    daemon=True,
                ).start()
        elif self.stick_engaged and magnitude <= off_at:
            held = self.stick_direction
            self.stick_engaged = False
            self.stick_direction = None
            self.stick_ready_at = time.time() + float(self.cfg.get("stick_settle", 0.5))
            if (bindings.get(held) or {}).get("type") == "ptt" and self.ptt:
                self.ptt.stop()

    def dispatch(self, switch, value):
        with self.listeners_lock:
            for box, event in self.listeners:
                box.append((switch, value))
                event.set()

        # Push-to-talk is driven by the device, not by a hook, so it is
        # handled here rather than over the socket.
        if value == "1":
            bound = (self.cfg.get("switch_actions") or {}).get(str(switch))
            if bound and bound.get("type"):
                log("sw %s -> %s" % (switch, bound.get("type")))
                threading.Thread(
                    target=actions.perform,
                    args=(bound, self.session_pid, log),
                    daemon=True,
                ).start()

        ptt_switch = str(self.cfg.get("ptt_switch") or "")
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
        """The state the ring should show right now."""
        with self.state_lock:
            now = time.time()

            if self.local_revert_at and now >= self.local_revert_at:
                self.local_state = None
                self.local_revert_at = None
            if self.local_state:
                self.busy_tool = None
                return self.local_state

            best, best_rank, best_tool = None, 99, None
            for sid in list(self.sessions):
                s = self.sessions[sid]
                if now - s.get("seen", 0) > SESSION_TTL:
                    del self.sessions[sid]
                    continue
                if s.get("revert_at") and now >= s["revert_at"]:
                    s["state"] = s.get("revert_to", "idle")
                    s.pop("revert_at", None)
                rank = STATE_RANK.get(s["state"], 50)
                if rank < best_rank:
                    best, best_rank, best_tool = s["state"], rank, s.get("tool")
            self.busy_tool = best_tool if best == "busy" else None
            return best or "idle"

    def set_state(self, state, revert_to=None, after=None):
        """Show something about the device itself, ahead of any session.

        "idle" clears the override rather than pinning the ring, so the
        sessions become visible again once push-to-talk is done.
        """
        with self.state_lock:
            if state == "idle" and after is None:
                self.local_state = None
                self.local_revert_at = None
                return
            self.local_state = state
            self.local_revert_at = time.time() + after if after else None

    def set_session_state(self, session_id, state, cwd=None, tool=None,
                          revert_to=None, after=None, pid=None):
        with self.state_lock:
            if state == "off":
                self.sessions.pop(session_id, None)
                return
            s = self.sessions.setdefault(session_id, {})
            s["state"] = state
            s["seen"] = time.time()
            s["tool"] = tool
            if cwd:
                s["cwd"] = cwd
            if pid:
                s["pid"] = pid
            if after:
                s["revert_at"] = time.time() + after
                s["revert_to"] = revert_to or "idle"
            else:
                s.pop("revert_at", None)

    def session_list(self):
        """[(label, state, session_id)] newest first, for display."""
        now = time.time()
        rows = []
        with self.state_lock:
            for sid, s in self.sessions.items():
                if now - s.get("seen", 0) > SESSION_TTL:
                    continue
                label = os.path.basename(s.get("cwd") or "") or sid[:8]
                rows.append((label, s.get("state", "idle"), sid, s.get("seen", 0)))
        rows.sort(key=lambda r: (STATE_RANK.get(r[1], 50), -r[3]))
        return [(a, b, c) for a, b, c, _ in rows]

    def animate_loop(self):
        period = 1.0 / max(1, self.cfg["fps"])
        previous = None
        while self.running:
            if self.display_off and self.cfg.get("off_when_display_sleeps", True):
                self.write_rgb((0, 0, 0))
                time.sleep(period)
                continue
            name = self.current()
            if name != previous:
                previous = name
                if self.on_state_change:
                    try:
                        self.on_state_change(name)
                    except Exception as exc:
                        log("state-change handler failed: %s" % exc)
            spec = self.cfg["states"].get(name, self.cfg["states"]["idle"])
            if name == "busy" and self.busy_tool:
                spec = (self.cfg.get("tool_states") or {}).get(self.busy_tool, spec)
            r, g, b = spec["color"]
            mode = spec.get("mode", "solid")
            t = time.time()

            if mode == "breathe":
                # sine, floored at 15% so the ring never fully drops out
                cycle = float(spec.get("period", 2.4))
                k = 0.15 + 0.85 * (0.5 + 0.5 * math.sin(2 * math.pi * t / cycle))
            elif mode == "blink":
                cycle = float(spec.get("period", 0.7))
                k = 1.0 if (t % cycle) < cycle / 2 else 0.0
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
                pids = req.get("pids")
                if pids:
                    for pid in pids:
                        if self.on_resolve_pid and self.on_resolve_pid(pid):
                            self.session_pid = pid
                            break
                name = req.get("state", "idle")
                after = req.get("after")
                session_id = req.get("session_id")
                if session_id:
                    self.set_session_state(
                        session_id, name,
                        cwd=req.get("cwd"), tool=req.get("tool"),
                        revert_to=req.get("revert_to", "idle"), after=after,
                        pid=self.session_pid,
                    )
                    # Waiting for input is as much a stop as waiting for
                    # approval; the ring alone cannot reach you across the room.
                    if name == "notify" and self.on_notify:
                        try:
                            self.on_notify(req)
                        except Exception as exc:
                            log("on_notify failed: %s" % exc)
                else:
                    self.set_state(name, revert_to=req.get("revert_to", "idle"), after=after)
                reply = {"ok": True, "state": name}
            elif cmd == "ask":
                timeout = float(req.get("timeout", self.cfg["ask_timeout"]))
                if self.on_ask:
                    try:
                        self.on_ask(req)
                    except Exception as exc:
                        log("on_ask failed: %s" % exc)
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
