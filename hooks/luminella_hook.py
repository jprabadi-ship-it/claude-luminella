#!/usr/bin/env python3
"""Claude Code hook entry point for the Luminella glow ring.

Standard library only, on purpose: this file gets installed to
~/.claude/luminella/hook.py and runs under /usr/bin/python3, so it must not
depend on the app bundle's interpreter or on pyserial. All it does is talk to
the menu bar app over a unix socket.

Failure policy: if the app is not running or the device is gone, every path
exits 0 without emitting a decision, leaving Claude Code's normal permission
flow untouched. The ring is an indicator and a convenience, never a gate that
can fail open.
"""

import json
import os
import socket
import subprocess
import sys
import time

STATE_DIR = os.path.join(os.path.expanduser("~"), ".claude", "luminella")
SOCKET_PATH = os.path.join(STATE_DIR, "daemon.sock")
CONFIG_PATH = os.path.join(STATE_DIR, "config.json")
BUNDLE_ID = "com.rabadi.clauminella"

DEFAULT_GATED_TOOLS = []
DEFAULT_ASK_TIMEOUT = 30.0
DEFAULT_DONE_HOLD = 2.0


def load_config():
    cfg = {
        "gated_tools": DEFAULT_GATED_TOOLS,
        "ask_timeout": DEFAULT_ASK_TIMEOUT,
        "done_hold": DEFAULT_DONE_HOLD,
        "approve_switch": "1",
        "deny_switch": "5",
    }
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def request(payload, timeout=5.0):
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(SOCKET_PATH)
        sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        data = b""
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        sock.close()
        return json.loads(data.split(b"\n", 1)[0].decode("utf-8")) if data else None
    except (OSError, ValueError):
        return None


def is_running():
    return request({"cmd": "ping"}, timeout=1.0) is not None


def ensure_running(wait=5.0):
    """Launch the menu bar app if it isn't up. -g keeps it from stealing focus."""
    if is_running():
        return True
    try:
        subprocess.run(
            ["/usr/bin/open", "-g", "-b", BUNDLE_ID],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    deadline = time.time() + wait
    while time.time() < deadline:
        if is_running():
            return True
        time.sleep(0.2)
    return False


SESSION = {}
# Which hook event is being handled. Sent with every state change so the app
# can tell an event that can only follow an approval from one that precedes it.
EVENT = {}


def set_state(state, revert_to=None, after=None, tool=None, pids=None):
    payload = {"cmd": "state", "state": state}
    # Which session this is, and a name a person can read. Sent on every
    # change so the app can hold a register of what is running where.
    payload.update(SESSION)
    payload.update(EVENT)
    if after is not None:
        payload["after"] = after
        payload["revert_to"] = revert_to or "idle"
    if tool:
        payload["tool"] = tool
    if pids:
        payload["pids"] = pids
    return request(payload, timeout=2.0)


def ancestor_pids(limit=15):
    """This process's parents, nearest first.

    The hook runs inside the Claude Code process tree, so the application the
    session belongs to is somewhere up this chain. Deciding which entry is a
    real GUI app needs AppKit, which the hook deliberately cannot import, so
    the list is handed to the app to resolve.
    """
    pids, pid = [], os.getpid()
    for _ in range(limit):
        try:
            out = subprocess.run(
                ["/bin/ps", "-o", "ppid=", "-p", str(pid)],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            break
        if not out:
            break
        pids.append(pid)
        try:
            pid = int(out)
        except ValueError:
            break
        if pid <= 1:
            break
    return pids


def ask(tool, timeout):
    payload = {"cmd": "ask", "tool": tool, "timeout": timeout, "pids": ancestor_pids()}
    # Which session is asking, so the notification can say so. With several
    # running, "waiting for approval" on its own does not tell you where to go.
    payload.update(SESSION)
    return request(payload, timeout=timeout + 10)


def emit_pretooluse(decision, reason):
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    sys.stdout.write("\n")


def emit_permission_request(behavior):
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": behavior},
            }
        },
        sys.stdout,
    )
    sys.stdout.write("\n")


def main():
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0

    event = payload.get("hook_event_name", "")
    EVENT["event"] = event
    cfg = load_config()
    if payload.get("session_id"):
        SESSION["session_id"] = payload["session_id"]
    if payload.get("cwd"):
        SESSION["cwd"] = payload["cwd"]

    if event == "SessionStart":
        ensure_running()
        # Walking the process tree costs a handful of ps calls, so it happens
        # once per session rather than on every event. The app keeps the
        # result and aims interrupts and canned prompts at that window.
        set_state("idle", pids=ancestor_pids())
        return 0

    if not is_running() and not ensure_running(wait=2.0):
        return 0

    if event == "UserPromptSubmit":
        set_state("busy")

    elif event == "PermissionRequest":
        # Fires only when a tool call actually needs approval. Blink amber and
        # wait for a switch; on timeout emit nothing so the on-screen prompt
        # (and any other PermissionRequest hook) still decides.
        decision = (ask(payload.get("tool_name", ""), cfg["ask_timeout"]) or {}).get("decision")
        if decision == "allow":
            emit_permission_request("allow")
        elif decision == "deny":
            emit_permission_request("deny")
        else:
            set_state("busy")

    elif event == "PreToolUse":
        tool = payload.get("tool_name", "")
        if tool in cfg["gated_tools"]:
            decision = (ask(tool, cfg["ask_timeout"]) or {}).get("decision")
            if decision == "allow":
                emit_pretooluse("allow", f"Luminella: switch {cfg['approve_switch']} pressed")
            elif decision == "deny":
                emit_pretooluse("deny", f"Luminella: switch {cfg['deny_switch']} pressed")
            else:
                set_state("busy")
            return 0
        set_state("busy", tool=tool)

    elif event == "PostToolUse":
        set_state("busy", tool=payload.get("tool_name"))

    elif event == "Notification":
        set_state("notify")

    elif event in ("Stop", "SubagentStop"):
        set_state("done", revert_to="idle", after=cfg["done_hold"])

    elif event == "SessionEnd":
        set_state("off")

    return 0


if __name__ == "__main__":
    sys.exit(main())
