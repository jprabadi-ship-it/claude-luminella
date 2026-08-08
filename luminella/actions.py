"""Things the device can make the computer do, beyond approving a tool call.

Every action ends up as a keystroke aimed at whichever application owns the
Claude Code session, so they share the focus handling and the accessibility
check that push-to-talk already needed.
"""

import time

from luminella import ptt

# Virtual key codes (Carbon HIToolbox names, values are stable).
KEY_ESCAPE = 53
KEY_RETURN = 36
KEY_V = 9


def send_key(keycode, command=False):
    """Post one key down/up pair. Returns (ok, message)."""
    if not ptt.accessibility_trusted():
        return False, "アクセシビリティの許可がありません"
    try:
        from Quartz import (
            CGEventCreateKeyboardEvent,
            CGEventPost,
            CGEventSetFlags,
            kCGEventFlagMaskCommand,
            kCGHIDEventTap,
        )
    except ImportError as exc:
        return False, "Quartz を読み込めません: %s" % exc

    try:
        for pressed in (True, False):
            event = CGEventCreateKeyboardEvent(None, keycode, pressed)
            if command:
                CGEventSetFlags(event, kCGEventFlagMaskCommand)
            else:
                CGEventSetFlags(event, 0)
            CGEventPost(kCGHIDEventTap, event)
            time.sleep(0.02)
    except Exception as exc:
        return False, repr(exc)
    return True, ""


def activate(pid):
    """Bring an application forward so the keystroke reaches it."""
    if not pid:
        return False
    try:
        from AppKit import NSRunningApplication

        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
        if app is None or app.activationPolicy() != 0:
            return False
        app.activateWithOptions_(1 << 1)
        time.sleep(0.15)
        return True
    except Exception:
        return False


def interrupt(session_pid=None):
    """Stop whatever Claude Code is doing.

    Escape is what the on-screen interface listens for, so the session's
    window has to be frontmost first -- otherwise the key lands wherever the
    user happens to be looking.
    """
    activate(session_pid)
    return send_key(KEY_ESCAPE)


def send_prompt(text, submit=True, session_pid=None):
    """Type a canned prompt into the session, optionally submitting it."""
    if not text:
        return False, "本文が空です"
    activate(session_pid)
    ready, note = ptt.ensure_text_focus()
    if not ready:
        return False, note or "入力欄が見つかりません"
    ptt.set_clipboard(text)
    ok, err = send_key(KEY_V, command=True)
    if not ok:
        return False, err
    if submit:
        time.sleep(0.12)
        return send_key(KEY_RETURN)
    return True, ""


def perform(action, session_pid=None, log=print):
    """Run one configured action. Returns (ok, message)."""
    kind = (action or {}).get("type")
    if kind == "interrupt":
        ok, err = interrupt(session_pid)
        log("action: interrupt ok=%s %s" % (ok, err))
        return ok, err
    if kind == "prompt":
        ok, err = send_prompt(
            action.get("text", ""), action.get("submit", True), session_pid
        )
        log("action: prompt %r ok=%s %s" % (action.get("text", "")[:20], ok, err))
        return ok, err
    if kind in (None, "ptt"):
        return True, ""
    log("action: unknown type %r" % kind)
    return False, "不明な動作: %s" % kind
