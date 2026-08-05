"""Configuration for the Luminella / Claude Code bridge.

Defaults live here; ~/.claude/luminella/config.json overrides them key by key.
"""

import json
import os

HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".claude", "luminella")
SOCKET_PATH = os.path.join(STATE_DIR, "daemon.sock")
LOG_PATH = os.path.join(STATE_DIR, "daemon.log")
CONFIG_PATH = os.path.join(STATE_DIR, "config.json")

# mode is one of: solid, breathe, blink
DEFAULTS = {
    "port": "/dev/cu.SLAB_USBtoUART",
    "states": {
        "idle":    {"color": [0, 20, 60],    "mode": "solid"},
        "busy":    {"color": [0, 160, 200],  "mode": "breathe"},
        "ask":     {"color": [255, 140, 0],  "mode": "blink"},
        "error":   {"color": [220, 0, 0],    "mode": "solid"},
        "done":    {"color": [0, 200, 60],   "mode": "solid"},
        "notify":  {"color": [180, 0, 255],  "mode": "blink"},
        "off":     {"color": [0, 0, 0],      "mode": "solid"},
    },
    # Which switch approves / rejects a gated tool call.
    "approve_switch": "1",
    "deny_switch": "5",
    # Tools whose PreToolUse is gated on a physical button press.
    # Anything not listed here goes through Claude Code's normal permission flow.
    "gated_tools": ["Bash", "Write", "Edit", "NotebookEdit"],
    # Seconds to wait for a button before falling back to the on-screen prompt.
    "ask_timeout": 30.0,
    # After "done", return to idle this many seconds later.
    "done_hold": 2.0,
    "fps": 15,
}


def load():
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user = json.load(f)
    except (OSError, ValueError):
        return cfg
    for key, value in user.items():
        if key == "states" and isinstance(value, dict):
            cfg["states"].update(value)
        else:
            cfg[key] = value
    return cfg
