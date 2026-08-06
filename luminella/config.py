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
        # push-to-talk
        "rec":     {"color": [255, 0, 90],   "mode": "breathe"},
        "stt":     {"color": [120, 90, 255], "mode": "blink"},
    },
    # Sound on state change. Names are macOS system sounds
    # (/System/Library/Sounds); null means silent. States that fire constantly
    # -- busy on every tool call, idle on every return -- are silent by
    # default, since a chime per tool call is unusable.
    "sound": True,
    "sounds": {
        "ask":    "Submarine",
        "done":   "Glass",
        "error":  "Basso",
        "notify": "Ping",
        "rec":    "Pop",
        "stt":    "Tink",
        "busy":   None,
        "idle":   None,
        "off":    None,
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

    # ---- push-to-talk ----
    # Hold this switch to record; release to transcribe to the clipboard.
    # null disables the feature entirely.
    "ptt_switch": None,
    # "stick" holds while the stick is deflected, "switch" while a button is
    # held. The stick reports position continuously, so a lost frame corrects
    # itself; a switch's release edge, once dropped, is gone.
    "ptt_mode": "stick",
    "ptt_stick_on": 45,
    "ptt_stick_off": 20,
    # AVFoundation audio input index (see the menu for the device list).
    "mic_index": 0,
    "stt_language": "ja",
    # Trimming silence keeps whisper from inventing text to fill it, but the
    # filter can cut speech too; off until it is tuned.
    "trim_silence": False,
    # Absolute path to the transcriber. Required when running from an app
    # bundle, where the project's .venv cannot be discovered.
    "stt_path": None,
    # mlx-whisper model (GPU, ~1s for 5s of speech on Apple Silicon).
    "stt_model": "mlx-community/whisper-large-v3-turbo",
    # Fallback when only the reference whisper CLI is installed.
    "stt_model_cli": "base",
}


def load():
    cfg = json.loads(json.dumps(DEFAULTS))  # deep copy
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user = json.load(f)
    except (OSError, ValueError):
        return cfg
    for key, value in user.items():
        if key in ("states", "sounds") and isinstance(value, dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    return cfg
