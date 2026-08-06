"""Luminella menu bar app.

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
from AppKit import NSApplication

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from luminella import config, daemon, hookinstall, ptt

APP_NAME = "Luminella"

# Menu bar glyph per daemon state. Colour is carried by the emoji because a
# status item title cannot be tinted per-character.
GLYPH = {
    "idle": "🔵",
    "busy": "🩵",
    "ask": "🟠",
    "notify": "🟣",
    "error": "🔴",
    "done": "🟢",
    "off": "⚫",
    "rec": "🔴",
    "stt": "🟣",
}

STATE_LABEL = {
    "idle": "待機中",
    "busy": "実行中",
    "ask": "許可待ち",
    "notify": "通知",
    "error": "エラー / 拒否",
    "done": "完了 / 許可",
    "off": "停止",
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
        super().__init__(APP_NAME, title="⚫", quit_button=None)
        self.cfg = config.load()
        self.daemon = daemon.Daemon(self.cfg)
        self.stopping = False

        self.item_state = rumps.MenuItem("状態: 起動中…")
        self.item_device = rumps.MenuItem("デバイス: 確認中…")
        self.item_hooks = rumps.MenuItem("Claude Code フック: 確認中…")
        self.item_ptt = rumps.MenuItem("プッシュトゥトーク: 確認中…")
        self.item_sound = rumps.MenuItem("効果音", callback=self.toggle_sound)
        self.item_sound.state = 1 if self.cfg.get("sound") else 0

        self.mics = []
        threading.Thread(target=self._load_mics, daemon=True).start()

        self.daemon.on_state_change = self.play_state_sound

        self.daemon.ptt = ptt.PushToTalk(
            self.cfg, on_state=self._ptt_state, on_result=self._ptt_result, log=daemon.log
        )

        self.menu = [
            self.item_state,
            self.item_device,
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
            None,
            self.item_sound,
            None,
            rumps.MenuItem("設定ファイルを開く", callback=self.open_config),
            rumps.MenuItem("ログを開く", callback=self.open_log),
            rumps.MenuItem("デバイスに再接続", callback=self.reconnect),
            None,
            rumps.MenuItem("終了", callback=self.quit_app),
        ]

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
        self.title = GLYPH.get(state, "⚫")
        self.item_state.title = f"状態: {STATE_LABEL.get(state, state)}"

        connected = bool(self.daemon.ser) and self.daemon.running
        self.item_device.title = (
            "デバイス: 接続済み" if connected else "デバイス: 未接続（Core が掴んでいませんか）"
        )

        installed = hookinstall.is_installed()
        self.item_hooks.title = f"Claude Code フック: {'導入済み' if installed else '未導入'}"

        switch = self.cfg.get("ptt_switch")
        if not self.daemon.ptt.available():
            self.item_ptt.title = "プッシュトゥトーク: エンジン未検出"
        elif switch:
            self.item_ptt.title = f"プッシュトゥトーク: SW{switch} を長押し"
        else:
            self.item_ptt.title = "プッシュトゥトーク: 未設定"

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

    def _ptt_result(self, ok, text):
        if ok:
            preview = text if len(text) <= 90 else text[:90] + "…"
            self.daemon.set_state("done", revert_to="idle", after=1.2)
            rumps.notification("Luminella", "クリップボードにコピーしました", preview)
        else:
            self.daemon.set_state("error", revert_to="idle", after=1.5)
            rumps.notification("Luminella", "プッシュトゥトーク", text)

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
