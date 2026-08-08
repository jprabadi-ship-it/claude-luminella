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
from AppKit import NSApplication, NSRunningApplication

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
        self._probed_at = 0.0
        self._hooks_installed = False
        self._stt_available = False
        self.daemon = daemon.Daemon(self.cfg)
        self.stopping = False

        self.item_state = rumps.MenuItem("状態: 起動中…")
        # Fixed slots: a rumps menu is built once, so rows are re-titled
        # rather than added and removed as sessions come and go.
        self.session_slots = [rumps.MenuItem("セッション%d" % (i + 1)) for i in range(6)]
        self.item_device = rumps.MenuItem("デバイス: 確認中…")
        self.item_hooks = rumps.MenuItem("Claude Code フック: 確認中…")
        self.item_ptt = rumps.MenuItem("プッシュトゥトーク: 確認中…")
        self.item_sound = rumps.MenuItem("効果音", callback=self.toggle_sound)
        self.item_sound.state = 1 if self.cfg.get("sound") else 0
        self.item_paste = rumps.MenuItem("入力欄に直接入力", callback=self.toggle_paste)
        self.item_paste.state = 1 if self.cfg.get("paste_to_focused") else 0
        self.item_focus = rumps.MenuItem("許可待ちで前面に出す", callback=self.toggle_focus)
        self.item_focus.state = 1 if self.cfg.get("focus_on_ask") else 0

        self.mics = []
        threading.Thread(target=self._load_mics, daemon=True).start()

        self.daemon.on_state_change = self.play_state_sound
        self.daemon.on_ask = self.focus_asking_session
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
            rumps.MenuItem("許可ボタンを割り当て…", callback=self.assign_approve),
            rumps.MenuItem("拒否ボタンを割り当て…", callback=self.assign_deny),
            None,
            self.item_hooks,
            rumps.MenuItem("フックを導入", callback=self.install_hooks),
            rumps.MenuItem("フックを解除", callback=self.uninstall_hooks),
            None,
            self.item_ptt,
            rumps.MenuItem("押して話すボタンを割り当て…", callback=self.assign_ptt),
            rumps.MenuItem("プッシュトゥトークを無効化", callback=self.disable_ptt),
            rumps.MenuItem("マイクを選ぶ…", callback=self.choose_mic),
            self.item_paste,
            None,
            self.item_focus,
            self.item_sound,
            None,
            rumps.MenuItem("設定ファイルを開く", callback=self.open_config),
            rumps.MenuItem("ログを開く", callback=self.open_log),
            rumps.MenuItem("デバイスに再接続", callback=self.reconnect),
            None,
            rumps.MenuItem("終了", callback=self.quit_app),
        ]

        for slot in self.session_slots:
            slot._menuitem.setHidden_(True)

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

        rows = self.daemon.session_list()
        padded = rows + [None] * len(self.session_slots)
        for slot, row in zip(self.session_slots, padded):
            if row is None:
                slot._menuitem.setHidden_(True)
                continue
            slot._menuitem.setHidden_(False)
            label, session_state, _ = row
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

    def _show_icon(self, state):
        path = self.icons.get(state)
        if not path:
            self.title = GLYPH.get(state, "⚫")
            return
        try:
            self.icon = path
            # rumps loads the file at its pixel size; the image is drawn at 2x
            # so it has to be told the point size or it fills the whole bar.
            self._icon_nsimage.setSize_((icon.PT, icon.PT))
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

    # ---- button assignment ---------------------------------------------

    def _assign(self, key, label, colour):
        if not self.daemon.running:
            alert(APP_NAME, "デバイスに接続していません。")
            return
        previous = self.daemon.current()
        self.daemon.set_state(colour)

        def worker():
            switch = self.daemon.read_switch(20)
            self.daemon.set_state(previous)
            if switch is None:
                rumps.notification(APP_NAME, f"{label}の割り当て", "タイムアウトしました")
                return
            self.cfg[key] = switch
            self.daemon.cfg[key] = switch
            self._save_config({key: switch})
            rumps.notification(APP_NAME, f"{label}の割り当て", f"SW{switch} に設定しました")

        threading.Thread(target=worker, daemon=True).start()
        rumps.notification(
            APP_NAME, f"{label}にしたいボタンを押してください", "20秒以内 / リングの色が変わります"
        )

    @guard
    def assign_approve(self, _):
        self._assign("approve_switch", "許可ボタン", "done")

    @guard
    def assign_deny(self, _):
        self._assign("deny_switch", "拒否ボタン", "error")

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

    # ---- focus -----------------------------------------------------------

    def is_gui_app(self, pid):
        """Whether a pid belongs to a regular application, not a helper."""
        try:
            app = NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
        except Exception:
            return False
        return app is not None and app.activationPolicy() == 0

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

    @guard
    def toggle_focus(self, _):
        enabled = not self.cfg.get("focus_on_ask")
        self.cfg["focus_on_ask"] = enabled
        self.daemon.cfg["focus_on_ask"] = enabled
        self.item_focus.state = 1 if enabled else 0
        self._save_config({"focus_on_ask": enabled})

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

    @guard
    def toggle_paste(self, _):
        enabled = not self.cfg.get("paste_to_focused")
        if enabled:
            front()
            if not rumps.alert(
                APP_NAME,
                "文字起こしした内容を、入力中のアプリへ直接貼り付けます。\n\n"
                "そのためにキー操作（Cmd+V）を送るので、macOS の操作許可が必要です。\n"
                "初回に確認が出ます。\n\n"
                "オフのままなら権限は一切不要で、クリップボードにだけ入ります。",
                ok="有効にする", cancel="やめる",
            ):
                return
            if not ptt.accessibility_trusted(prompt=True):
                alert(
                    APP_NAME,
                    "システム設定 → プライバシーとセキュリティ → アクセシビリティ で\n"
                    "Luminella を許可してください。\n\n"
                    "許可するまでは、クリップボードにのみ入ります。",
                )
        self.cfg["paste_to_focused"] = enabled
        self.daemon.cfg["paste_to_focused"] = enabled
        self.item_paste.state = 1 if enabled else 0
        self._save_config({"paste_to_focused": enabled})

    @guard
    def toggle_sound(self, _):
        enabled = not self.cfg.get("sound")
        self.cfg["sound"] = enabled
        self.daemon.cfg["sound"] = enabled
        self.item_sound.state = 1 if enabled else 0
        self._save_config({"sound": enabled})
        if enabled:
            self.play_state_sound("done")

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
            rumps.notification(APP_NAME, title, preview)
        else:
            self.daemon.set_state("error", revert_to="idle", after=1.5)
            rumps.notification(APP_NAME, "プッシュトゥトーク", text)

    @guard
    def assign_ptt(self, _):
        if not self.daemon.ptt.available():
            alert(
                APP_NAME,
                "文字起こしエンジンが見つかりません。\n\n"
                "次のいずれかを入れてください:\n"
                "  pip install mlx-whisper   （高速・推奨）\n"
                "  pip install openai-whisper",
            )
            return

        def worker():
            switch, has_release = self.daemon.read_hold(20)
            self.daemon.set_state(previous)
            if switch is None:
                rumps.notification(APP_NAME, "押して話すボタン", "タイムアウトしました")
                return
            if not has_release:
                # At least one switch on the device reports the press only, and
                # push-to-talk stops on the release edge.
                rumps.notification(
                    APP_NAME, "SW%s は使えません" % switch,
                    "このボタンは「離した」信号を送りません。別のボタンを選んでください",
                )
                return
            self.cfg["ptt_switch"] = switch
            self.daemon.cfg["ptt_switch"] = switch
            self._save_config({"ptt_switch": switch})
            rumps.notification(APP_NAME, "押して話すボタン", "SW%s に設定しました" % switch)

        if not self.daemon.running:
            alert(APP_NAME, "デバイスに接続していません。")
            return
        previous = self.daemon.current()
        self.daemon.set_state("rec")
        threading.Thread(target=worker, daemon=True).start()
        rumps.notification(
            APP_NAME, "押して話すボタンを押して、離してください", "20秒以内 / リングがピンクに光ります"
        )

    @guard
    def disable_ptt(self, _):
        self.cfg["ptt_switch"] = None
        self.daemon.cfg["ptt_switch"] = None
        self._save_config({"ptt_switch": None})
        rumps.notification(APP_NAME, "プッシュトゥトーク", "無効にしました")

    @guard
    def choose_mic(self, _):
        devices = self.mics or ptt.list_input_devices()
        self.mics = devices
        if not devices:
            alert(APP_NAME, "マイクが見つかりませんでした。")
            return
        current = int(self.cfg.get("mic_index", 0))
        listing = "\n".join(
            "%s%d: %s" % ("→ " if i == current else "   ", i, n) for i, n in devices
        )
        window = rumps.Window(
            title=APP_NAME,
            message="使うマイクの番号を入力してください。\n\n" + listing,
            default_text=str(current),
            ok="設定",
            cancel="キャンセル",
            dimensions=(80, 22),
        )
        front()
        response = window.run()
        if not response.clicked:
            return
        try:
            index = int(response.text.strip())
        except ValueError:
            alert(APP_NAME, "番号を入力してください。")
            return
        if index not in [i for i, _ in devices]:
            alert(APP_NAME, "その番号のマイクはありません。")
            return
        self.cfg["mic_index"] = index
        self.daemon.cfg["mic_index"] = index
        self._save_config({"mic_index": index})
        name = dict(devices)[index]
        rumps.notification(APP_NAME, "マイク", "%d: %s に設定しました" % (index, name))

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
        rumps.notification(APP_NAME, "再接続", "デバイスに接続し直しています…")

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
