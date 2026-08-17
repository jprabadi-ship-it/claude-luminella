"""Clauminella menu bar app.

Hosts the serial daemon in background threads and exposes it through a status
item, so there is one process that owns the device, renders the ring, and
answers Claude Code's hooks over the unix socket.
"""

import json
import os
import subprocess
import sys
import threading
import time
import traceback

import rumps
from AppKit import NSApplication, NSRunningApplication, NSWorkspace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from luminella import config, daemon, hookinstall, icon, ptt

APP_NAME = "Clauminella"

# Fallback glyphs, used only if the drawn icons cannot be produced.
GLYPH = {
    "idle": "🔵",
    "busy": "🩵",
    "ask": "🟠",
    "notify": "🟣",
    "error": "🔴",
    "done": "🟢",
    "off": "⚫",
    "warmup": "🩷",
    "rec": "🔴",
    "stt": "🟣",
}

# Marks for the session list. Plain characters rather than the ring icons,
# which only exist as files sized for the status bar.
MARK = {
    "ask": "\N{LARGE ORANGE CIRCLE}", "notify": "\N{LARGE PURPLE CIRCLE}",
    "error": "\N{LARGE RED CIRCLE}", "done": "\N{LARGE GREEN CIRCLE}",
    "busy": "\N{LARGE BLUE CIRCLE}", "idle": "\N{MEDIUM WHITE CIRCLE}",
}

STATE_LABEL = {
    "idle": "待機中",
    "busy": "実行中",
    "ask": "許可待ち",
    "notify": "通知",
    "error": "エラー / 拒否",
    "done": "完了 / 許可",
    "off": "停止",
    "warmup": "マイク準備中",
    "rec": "録音中",
    "stt": "文字起こし中",
}


def guard(fn):
    """Log exceptions raised inside menu callbacks.

    A traceback from a rumps callback goes to stderr, which is nowhere at all
    inside an app bundle, so a broken menu item is indistinguishable from one
    that simply does nothing.
    """
    def wrapper(self, sender=None):
        daemon.log("menu: %s" % fn.__name__)
        try:
            return fn(self, sender)
        except Exception:
            daemon.log("menu: %s FAILED\n%s" % (fn.__name__, traceback.format_exc()))
            raise
    wrapper.__name__ = fn.__name__
    return wrapper


def front():
    """Bring the app forward so dialogs are not buried behind other windows.

    LSUIElement apps are not activated by a menu click, so an NSAlert or a
    rumps.Window opens behind the frontmost application and looks like nothing
    happened at all.
    """
    try:
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception:
        pass


def alert(*args, **kwargs):
    front()
    return rumps.alert(*args, **kwargs)


def resource(name):
    """Locate a file both in a py2app bundle and when run from source."""
    bundled = os.path.join(os.environ.get("RESOURCEPATH", ""), name)
    if os.environ.get("RESOURCEPATH") and os.path.exists(bundled):
        return bundled
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), name)


class LuminellaApp(rumps.App):
    def __init__(self):
        super().__init__(APP_NAME, title="", quit_button=None)
        self.cfg = config.load()
        try:
            self.icons = icon.render_states(self.cfg["states"])
        except Exception:
            daemon.log("icon rendering failed\n%s" % traceback.format_exc())
            self.icons = {}
        self.shown_state = None
        self._logged_icon = False
        # Notifications are posted from the rumps timer, which runs on the
        # main thread. Everything that wants to notify -- the daemon's socket
        # handler, the push-to-talk worker -- runs on other threads, and
        # AppKit quietly does nothing when called from those.
        self._pending_notes = []
        self._notes_lock = threading.Lock()
        self._notification_centre = None
        self._prepare_notifications()
        self._probed_at = 0.0
        self._hooks_installed = False
        self._stt_available = False
        self.daemon = daemon.Daemon(self.cfg)
        self.stopping = False

        self.item_state = rumps.MenuItem("状態: 起動中…")
        # Fixed slots: a rumps menu is built once, so rows are re-titled
        # rather than added and removed as sessions come and go.
        self.session_slots = [
            rumps.MenuItem("セッション%d" % (i + 1), callback=self.reset_session)
            for i in range(6)
        ]
        # session_id behind each visible slot, refreshed with the titles, so a
        # click knows which session it is aimed at.
        self.slot_sids = [None] * 6
        self.item_device = rumps.MenuItem("デバイス: 確認中…")
        self.item_hooks = rumps.MenuItem("Claude Code フック: 確認中…")
        self.item_ptt = rumps.MenuItem("プッシュトゥトーク: 確認中…")
        self.settings = None

        self.mics = []
        threading.Thread(target=self._load_mics, daemon=True).start()

        self.daemon.on_state_change = self.play_state_sound
        self.daemon.on_ask = self.on_ask
        self.daemon.on_notify = self.on_notify
        self.daemon.on_resolve_pid = self.is_gui_app

        self.daemon.ptt = ptt.PushToTalk(
            self.cfg, on_state=self._ptt_state, on_result=self._ptt_result, log=daemon.log
        )

        self.menu = [
            self.item_state,
            self.item_device,
            None,
        ] + self.session_slots + [
            None,
            self.item_ptt,
            self.item_hooks,
            None,
            rumps.MenuItem("設定…", callback=self.open_settings),
            rumps.MenuItem("フックを導入", callback=self.install_hooks),
            rumps.MenuItem("フックを解除", callback=self.uninstall_hooks),
            None,
            rumps.MenuItem("設定ファイルを開く", callback=self.open_config),
            rumps.MenuItem("ログを開く", callback=self.open_log),
            rumps.MenuItem("デバイスに再接続", callback=self.reconnect),
            None,
            rumps.MenuItem("終了", callback=self.quit_app),
        ]

        for slot in self.session_slots:
            slot._menuitem.setHidden_(True)

        self._watch_display_sleep()

        # Keep the deployed hook in step with the one in this bundle, so an
        # update to the hook takes effect on launch rather than waiting for
        # someone to click 導入 again.
        try:
            if hookinstall.refresh_hook_script(resource("hook.py")):
                daemon.log("hook script updated from the bundle")
        except Exception:
            daemon.log("hook refresh failed\n%s" % traceback.format_exc())

        threading.Thread(target=self._run_daemon, daemon=True).start()

    # ---- daemon lifecycle ----------------------------------------------

    def _run_daemon(self):
        """Supervisor: keep the daemon alive across unplug/replug.

        serve() returns whenever the daemon stops -- either because a write
        failed (device pulled) or because "再接続" was clicked -- so the loop
        simply reconnects and starts over.
        """
        while not self.stopping:
            self.daemon.running = True
            if not self.daemon.connect():
                self.daemon.running = False
                time.sleep(3)
                continue
            threading.Thread(target=self.daemon.reader_loop, daemon=True).start()
            threading.Thread(target=self.daemon.animate_loop, daemon=True).start()
            self.daemon.serve()
            time.sleep(1)

    @rumps.timer(0.4)
    def refresh(self, _):
        state = self.daemon.current() if self.daemon.running else "off"
        if state != self.shown_state:
            self.shown_state = state
            self._show_icon(state)
        self.item_state.title = f"状態: {STATE_LABEL.get(state, state)}"

        connected = bool(self.daemon.ser) and self.daemon.running
        self.item_device.title = (
            "デバイス: 接続済み" if connected else "デバイス: 未接続（Core が掴んでいませんか）"
        )

        self._drain_notes()

        rows = self.daemon.session_list()
        padded = rows + [None] * len(self.session_slots)
        for i, (slot, row) in enumerate(zip(self.session_slots, padded)):
            if row is None:
                slot._menuitem.setHidden_(True)
                self.slot_sids[i] = None
                continue
            slot._menuitem.setHidden_(False)
            label, session_state, sid = row
            self.slot_sids[i] = sid
            slot.title = "%s %s \u2014 %s" % (
                MARK.get(session_state, "\N{MEDIUM WHITE CIRCLE}"), label,
                STATE_LABEL.get(session_state, session_state))

        # These read settings.json and probe the filesystem, so they are polled
        # every few seconds rather than on every 0.4s tick.
        now = time.time()
        if now - self._probed_at > 4.0:
            self._probed_at = now
            self._hooks_installed = hookinstall.is_installed()
            self._stt_available = self.daemon.ptt.available()
        self.item_hooks.title = (
            f"Claude Code フック: {'導入済み' if self._hooks_installed else '未導入'}"
        )

        # Say how it is actually triggered. This line went on naming a switch
        # long after the stick took the job over, which read as a bug in the
        # app when it was only a stale label.
        if not self._stt_available:
            self.item_ptt.title = "プッシュトゥトーク: エンジン未検出"
        elif self.cfg.get("ptt_mode") == "stick":
            held = [d for d, a in (self.cfg.get("stick_actions") or {}).items()
                    if (a or {}).get("type") == "ptt"]
            where = {"down": "手前", "up": "奥", "left": "左", "right": "右"}
            if len(held) >= 4:
                self.item_ptt.title = "プッシュトゥトーク: スティックを倒す（どの向きでも）"
            elif held:
                self.item_ptt.title = "プッシュトゥトーク: スティックを%sに倒す" % where.get(held[0], held[0])
            else:
                self.item_ptt.title = "プッシュトゥトーク: 未割り当て"
        elif self.cfg.get("ptt_switch"):
            self.item_ptt.title = "プッシュトゥトーク: SW%s を長押し" % self.cfg["ptt_switch"]
        else:
            self.item_ptt.title = "プッシュトゥトーク: 未設定"

    def notify(self, title, message=""):
        with self._notes_lock:
            self._pending_notes.append((title, message))

    def _prepare_notifications(self):
        """Ask once for permission to post banners.

        rumps posts through NSUserNotification, which current macOS accepts
        and silently discards -- no banner, no error, and the app still
        appears in System Settings, so everything looks configured.
        UserNotifications is the API that still delivers.
        """
        try:
            from UserNotifications import (
                UNAuthorizationOptionAlert,
                UNAuthorizationOptionSound,
                UNUserNotificationCenter,
            )

            centre = UNUserNotificationCenter.currentNotificationCenter()
            centre.requestAuthorizationWithOptions_completionHandler_(
                UNAuthorizationOptionAlert | UNAuthorizationOptionSound,
                lambda granted, error: daemon.log(
                    "notifications: granted=%s error=%s" % (granted, error)),
            )
            self._notification_centre = centre
            daemon.log("notifications: using UserNotifications")
        except Exception:
            self._notification_centre = None
            daemon.log("notifications: unavailable\n%s" % traceback.format_exc())

    def _post_note(self, title, message):
        if self._notification_centre is not None:
            try:
                import uuid

                from UserNotifications import (
                    UNMutableNotificationContent,
                    UNNotificationRequest,
                )

                content = UNMutableNotificationContent.alloc().init()
                content.setTitle_(title)
                if message:
                    content.setBody_(message)
                request = UNNotificationRequest.requestWithIdentifier_content_trigger_(
                    str(uuid.uuid4()), content, None
                )
                self._notification_centre.addNotificationRequest_withCompletionHandler_(
                    request, None
                )
                return
            except Exception as exc:
                daemon.log("notification failed: %r (%s)" % (exc, title))
        try:
            rumps.notification(APP_NAME, title, message)
        except Exception as exc:
            daemon.log("fallback notification failed: %r (%s)" % (exc, title))

    def _drain_notes(self):
        with self._notes_lock:
            pending, self._pending_notes = self._pending_notes, []
        for title, message in pending:
            self._post_note(title, message)

    def _show_icon(self, state):
        path = self.icons.get(state)
        if not path:
            self.title = GLYPH.get(state, "⚫")
            return
        try:
            self.icon = path
            # rumps loads the file at its pixel size; the image is drawn at 2x
            # so it has to be told the point size or it fills the whole bar.
            self._icon_nsimage.setSize_((icon.PT_W, icon.PT))
            self._nsapp.setStatusBarIcon()
            # rumps writes the app name into the status item whenever both the
            # title and the image are empty, which is the case for the moment
            # before the first icon is drawn -- and it never takes the name
            # back out. Clear it so the ring stands alone.
            self._nsapp.nsstatusitem.setTitle_("")
            if not self._logged_icon:
                self._logged_icon = True
                item = self._nsapp.nsstatusitem
                image = item.image()
                daemon.log("statusitem: image=%s menu=%s items=%s" % (
                    tuple(image.size()) if image else None,
                    item.menu() is not None,
                    item.menu().numberOfItems() if item.menu() else 0))
        except Exception:
            daemon.log("icon update failed\n%s" % traceback.format_exc())
            self.title = GLYPH.get(state, "⚫")

    @guard
    def reset_session(self, sender):
        """Put a clicked session row back to 待機中.

        A session can leave the ring stuck on 許可待ち when its prompt was
        answered in a way no hook reports. The state is only a display, so
        letting the person overrule it is safe: the next hook event from that
        session sets it right again either way.
        """
        try:
            index = self.session_slots.index(sender)
        except ValueError:
            return
        sid = self.slot_sids[index]
        if not sid:
            return
        label = sender.title.split(" ", 1)[-1].split(" — ")[0]
        self.daemon.set_session_state(sid, "idle")
        self.notify("表示をリセット", "%s を待機中に戻しました" % label)

    # ---- settings window -------------------------------------------------

    @guard
    def open_settings(self, _):
        from luminella import settingsui
        if self.settings is None:
            self.settings = settingsui.SettingsController.alloc().initWithApp_(self)
        self.settings.show()

    def _save_config(self, updates):
        os.makedirs(config.STATE_DIR, exist_ok=True)
        try:
            with open(config.CONFIG_PATH, encoding="utf-8") as f:
                stored = json.load(f)
        except (OSError, ValueError):
            stored = {}
        stored.update(updates)
        with open(config.CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(stored, f, indent=2, ensure_ascii=False)
            f.write("\n")

    # ---- display sleep ---------------------------------------------------

    def _watch_display_sleep(self):
        """Follow the screens, so the ring is not the only thing lit at night.

        Notifications rather than polling: the two events say exactly when the
        displays go and come back, and CGDisplayIsAsleep covers the case where
        the app starts up while they are already off.
        """
        try:
            from Quartz import CGDisplayIsAsleep, CGMainDisplayID

            self.daemon.display_off = bool(CGDisplayIsAsleep(CGMainDisplayID()))
        except Exception:
            pass
        try:
            centre = NSWorkspace.sharedWorkspace().notificationCenter()
            centre.addObserver_selector_name_object_(
                self, "screensDidSleep:", "NSWorkspaceScreensDidSleepNotification", None)
            centre.addObserver_selector_name_object_(
                self, "screensDidWake:", "NSWorkspaceScreensDidWakeNotification", None)
        except Exception:
            daemon.log("display sleep watch failed\n%s" % traceback.format_exc())

    def screensDidSleep_(self, note):
        daemon.log("display: asleep")
        self.daemon.display_off = True

    def screensDidWake_(self, note):
        daemon.log("display: awake")
        self.daemon.display_off = False
        self.daemon.last_rgb = None  # force a redraw at the current state

    # ---- focus -----------------------------------------------------------

    def is_gui_app(self, pid):
        """Whether a pid belongs to a regular application, not a helper."""
        try:
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
        except Exception:
            return False
        return app is not None and app.activationPolicy() == 0

    @staticmethod
    def _stamp(label):
        """Trailing detail that makes each notification's text unique.

        Tools that watch Notification Center drop a banner whose text repeats
        one they have already seen, so a second identical wait would never
        reach them. The time is what varies; the label is there to be useful.
        """
        return "（%s / %s）" % (time.strftime("%H:%M:%S"), label)

    @staticmethod
    def _label_of(req):
        return os.path.basename(req.get("cwd") or "") or "セッション"

    def on_ask(self, req):
        """Called when a session asks for approval."""
        pids = req.get("pids") or []
        if self.cfg.get("notify_on_ask"):
            label = self._label_of(req)
            tool = req.get("tool") or ""
            body = "%s の実行を待っています" % tool if tool else "操作の許可を待っています"
            self.notify("許可待ち: %s" % label, body + self._stamp(label))
        self.focus_asking_session(pids)

    def on_notify(self, req):
        """Called when a session is waiting on the person, not on a decision.

        Deliberately does not raise the window: this fires far more often than
        an approval request, and pulling focus every time would be worse than
        missing one.
        """
        if not self.cfg.get("notify_on_idle"):
            return
        label = self._label_of(req)
        self.notify("入力待ち: %s" % label, "入力を待っています" + self._stamp(label))

    def focus_asking_session(self, pids):
        """Raise the application whose session is waiting for approval.

        The hook sends its ancestry; the first entry that is a regular GUI
        application is the terminal or app hosting that session. Without this
        the ring says something is waiting but not where.
        """
        if not self.cfg.get("focus_on_ask") or not pids:
            return
        for pid in pids:
            try:
                app = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
            except Exception:
                continue
            if app is None or app.activationPolicy() != 0:
                continue
            daemon.log("focus: raising %s (pid %s)" % (app.localizedName(), pid))
            app.activateWithOptions_(1 << 1)  # NSApplicationActivateIgnoringOtherApps
            return
        daemon.log("focus: no GUI app found in %r" % (pids[:6],))

    # ---- sound -----------------------------------------------------------

    SOUND_DIR = "/System/Library/Sounds"

    def play_state_sound(self, state):
        """Play the sound mapped to a state, if any.

        This shells out to afplay rather than using NSSound: inside the app
        bundle NSSound reported success and produced nothing audible, while
        afplay is verifiably heard. Popen without wait keeps the animation
        loop from stalling on playback.
        """
        if not self.cfg.get("sound"):
            return
        name = (self.cfg.get("sounds") or {}).get(state)
        if not name:
            return
        path = os.path.join(self.SOUND_DIR, name + ".aiff")
        if not os.path.exists(path):
            daemon.log("sound %r not found at %s" % (name, path))
            return
        def worker():
            try:
                r = subprocess.run(
                    ["/usr/bin/afplay", path],
                    capture_output=True, timeout=30, env=ptt.clean_env(),
                    encoding="utf-8", errors="replace",
                )
                daemon.log("sound %s -> %s rc=%s err=%s" % (
                    state, name, r.returncode, (r.stderr or "").strip()[:200]))
            except Exception as exc:
                daemon.log("sound %s failed: %r" % (state, exc))

        threading.Thread(target=worker, daemon=True).start()

    # ---- push-to-talk ---------------------------------------------------

    def _load_mics(self):
        self.mics = ptt.list_input_devices()

    def _ptt_state(self, state):
        if state == "idle":
            self.daemon.set_state("idle")
        else:
            self.daemon.set_state(state)

    def _ptt_result(self, ok, text, pasted=False):
        if ok:
            preview = text if len(text) <= 90 else text[:90] + "…"
            self.daemon.set_state("done", revert_to="idle", after=1.2)
            title = "入力しました" if pasted else "クリップボードにコピーしました"
            self.notify(title, preview)
        else:
            self.daemon.set_state("error", revert_to="idle", after=1.5)
            self.notify("プッシュトゥトーク", text)

    # ---- hooks ----------------------------------------------------------

    @guard
    def install_hooks(self, _):
        try:
            hookinstall.install_hook_script(resource("hook.py"))
            backup = hookinstall.install()
        except Exception as exc:
            alert(APP_NAME, f"フックの導入に失敗しました:\n{exc}")
            return
        note = f"\n\nバックアップ: {os.path.basename(backup)}" if backup else ""
        alert(
            APP_NAME,
            "Claude Code フックを導入しました。\n"
            "既に開いているセッションには次回起動から反映されます。" + note,
        )

    @guard
    def uninstall_hooks(self, _):
        try:
            backup = hookinstall.uninstall()
        except Exception as exc:
            alert(APP_NAME, f"フックの解除に失敗しました:\n{exc}")
            return
        note = f"\n\nバックアップ: {os.path.basename(backup)}" if backup else ""
        alert(APP_NAME, "Claude Code フックを解除しました。" + note)

    # ---- misc -----------------------------------------------------------

    @guard
    def open_config(self, _):
        os.makedirs(config.STATE_DIR, exist_ok=True)
        if not os.path.exists(config.CONFIG_PATH):
            self._save_config({})
        subprocess.run(["/usr/bin/open", "-t", config.CONFIG_PATH], check=False)

    @guard
    def open_log(self, _):
        if os.path.exists(config.LOG_PATH):
            subprocess.run(["/usr/bin/open", "-t", config.LOG_PATH], check=False)
        else:
            alert(APP_NAME, "ログはまだありません。")

    @guard
    def reconnect(self, _):
        # Stopping the daemon is enough; the supervisor loop reconnects.
        self.daemon.running = False
        try:
            if self.daemon.ser:
                self.daemon.ser.close()
        except Exception:
            pass
        self.notify("再接続", "デバイスに接続し直しています…")

    @guard
    def quit_app(self, _):
        self.stopping = True
        self.daemon.running = False
        time.sleep(0.3)
        rumps.quit_application()


def main():
    os.makedirs(config.STATE_DIR, exist_ok=True)
    LuminellaApp().run()


if __name__ == "__main__":
    main()
