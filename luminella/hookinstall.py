"""Install / remove the Luminella hooks in ~/.claude/settings.json.

Edits are additive and reversible: Luminella entries are appended to whatever
hook arrays already exist and are removed by matching on the installed script
path, so other tools' hooks are never touched. A timestamped backup is written
before the first change.
"""

import json
import os
import shutil
import time

from luminella import config

SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".claude", "settings.json")
HOOK_PATH = os.path.join(config.STATE_DIR, "hook.py")
HOOK_COMMAND = f"/usr/bin/python3 {HOOK_PATH}"

EVENTS = [
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PermissionRequest",
    "Notification",
    "Stop",
    "SessionEnd",
]


def install_hook_script(source):
    """Copy the stdlib-only hook out of the app bundle into ~/.claude/luminella."""
    os.makedirs(config.STATE_DIR, exist_ok=True)
    if os.path.abspath(source) != os.path.abspath(HOOK_PATH):
        shutil.copyfile(source, HOOK_PATH)
    os.chmod(HOOK_PATH, 0o755)
    return HOOK_PATH


def _load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_settings(data):
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _backup():
    if not os.path.exists(SETTINGS_PATH):
        return None
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = f"{SETTINGS_PATH}.bak-luminella-{stamp}"
    shutil.copyfile(SETTINGS_PATH, dst)
    return dst


def is_installed():
    settings = _load_settings()
    hooks = settings.get("hooks", {})
    for event in EVENTS:
        for group in hooks.get(event, []):
            for entry in group.get("hooks", []):
                if HOOK_PATH in entry.get("command", ""):
                    return True
    return False


def install():
    backup = _backup()
    settings = _load_settings()
    hooks = settings.setdefault("hooks", {})

    for event in EVENTS:
        groups = hooks.setdefault(event, [])
        # Reuse a catch-all group if one exists, otherwise add our own.
        target = next((g for g in groups if g.get("matcher", "") == ""), None)
        if target is None:
            target = {"matcher": "", "hooks": []}
            groups.append(target)
        entries = target.setdefault("hooks", [])
        if not any(HOOK_PATH in e.get("command", "") for e in entries):
            entries.append({"type": "command", "command": HOOK_COMMAND})

    _save_settings(settings)
    return backup


def uninstall():
    backup = _backup()
    settings = _load_settings()
    hooks = settings.get("hooks", {})

    for event in list(hooks.keys()):
        groups = hooks.get(event, [])
        for group in groups:
            group["hooks"] = [
                e for e in group.get("hooks", []) if HOOK_PATH not in e.get("command", "")
            ]
        # Drop groups we emptied, and events left with no groups.
        hooks[event] = [g for g in groups if g.get("hooks")]
        if not hooks[event]:
            del hooks[event]

    _save_settings(settings)
    return backup
