"""Thin client for the Luminella daemon, plus autostart."""

import json
import os
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from luminella import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYTHON = os.path.join(ROOT, ".venv", "bin", "python")


def request(payload, timeout=5.0):
    """Send one request. Returns the reply dict, or None if unreachable."""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(config.SOCKET_PATH)
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        sock.close()
        if not data:
            return None
        return json.loads(data.split(b"\n", 1)[0].decode("utf-8"))
    except (OSError, ValueError):
        return None


def is_running():
    return request({"cmd": "ping"}, timeout=1.0) is not None


def ensure_running(wait=4.0):
    """Start the daemon if it isn't up. Returns True once it answers."""
    if is_running():
        return True
    os.makedirs(config.STATE_DIR, exist_ok=True)
    try:
        subprocess.Popen(
            [PYTHON, "-m", "luminella.daemon"],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError:
        return False
    deadline = time.time() + wait
    while time.time() < deadline:
        if is_running():
            return True
        time.sleep(0.15)
    return False


def set_state(state, revert_to=None, after=None):
    payload = {"cmd": "state", "state": state}
    if after is not None:
        payload["after"] = after
        payload["revert_to"] = revert_to or "idle"
    return request(payload, timeout=2.0)


def ask(tool, timeout):
    return request({"cmd": "ask", "tool": tool, "timeout": timeout}, timeout=timeout + 10)
