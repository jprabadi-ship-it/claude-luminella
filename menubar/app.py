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

import rumps

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from luminella import config, daemon, hookinstall

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
}

STATE_LABEL = {
    "idle": "待機中",
    "busy": "実行中",
    "ask": "許可待ち",
    "notify": "通知",
    "error": "エラー / 拒否",
    "done": "完了 / 許可",
    "off": "停止",
}


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

    # ---- button assignment ---------------------------------------------

    def _assign(self, key, label, colour):
        if not self.daemon.running:
            rumps.alert(APP_NAME, "デバイスに接続していません。")
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

    def assign_approve(self, _):
        self._assign("approve_switch", "許可ボタン", "done")

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

    # ---- hooks ----------------------------------------------------------

    def install_hooks(self, _):
        try:
            hookinstall.install_hook_script(resource("hook.py"))
            backup = hookinstall.install()
        except Exception as exc:
            rumps.alert(APP_NAME, f"フックの導入に失敗しました:\n{exc}")
            return
        note = f"\n\nバックアップ: {os.path.basename(backup)}" if backup else ""
        rumps.alert(
            APP_NAME,
            "Claude Code フックを導入しました。\n"
            "既に開いているセッションには次回起動から反映されます。" + note,
        )

    def uninstall_hooks(self, _):
        try:
            backup = hookinstall.uninstall()
        except Exception as exc:
            rumps.alert(APP_NAME, f"フックの解除に失敗しました:\n{exc}")
            return
        note = f"\n\nバックアップ: {os.path.basename(backup)}" if backup else ""
        rumps.alert(APP_NAME, "Claude Code フックを解除しました。" + note)

    # ---- misc -----------------------------------------------------------

    def open_config(self, _):
        os.makedirs(config.STATE_DIR, exist_ok=True)
        if not os.path.exists(config.CONFIG_PATH):
            self._save_config({})
        subprocess.run(["/usr/bin/open", "-t", config.CONFIG_PATH], check=False)

    def open_log(self, _):
        if os.path.exists(config.LOG_PATH):
            subprocess.run(["/usr/bin/open", "-t", config.LOG_PATH], check=False)
        else:
            rumps.alert(APP_NAME, "ログはまだありません。")

    def reconnect(self, _):
        # Stopping the daemon is enough; the supervisor loop reconnects.
        self.daemon.running = False
        try:
            if self.daemon.ser:
                self.daemon.ser.close()
        except Exception:
            pass
        rumps.notification(APP_NAME, "再接続", "デバイスに接続し直しています…")

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
