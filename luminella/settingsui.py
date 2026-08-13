"""Settings window for the Clauminella menu bar app.

The menu grew past thirty items; toggles and button assignment live here now.
Everything applies immediately -- there is no OK button to forget to press.

AppKit is driven directly rather than through rumps, which only knows about
menus. All methods must run on the main thread; the only background work is
waiting for a physical button press, which hops back via
performSelectorOnMainThread.
"""

import os
import subprocess
import threading

import objc
from AppKit import (
    NSAlert,
    NSApplication,
    NSAttributedString,
    NSBezierPath,
    NSButton,
    NSButtonTypeSwitch,
    NSColor,
    NSControlStateValueOff,
    NSControlStateValueOn,
    NSFont,
    NSFontAttributeName,
    NSForegroundColorAttributeName,
    NSMakeRect,
    NSObject,
    NSPopUpButton,
    NSTabView,
    NSTabViewItem,
    NSTextField,
    NSView,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskTitled,
)

# Row roles, in menu order. 許可/拒否 are exclusive (one switch each);
# 中断/定型プロンプト may repeat across switches.
ROLE_NONE = "なし"
ROLE_APPROVE = "許可"
ROLE_DENY = "拒否"
ROLE_INTERRUPT = "中断"
ROLE_PROMPT = "定型プロンプト"
ROLES = [ROLE_NONE, ROLE_APPROVE, ROLE_DENY, ROLE_INTERRUPT, ROLE_PROMPT]

SWITCHES = [str(i) for i in range(1, 9)]

WIN_W, WIN_H = 600, 710
PAD = 18
ROW_H = 26
GAP = 8

# States that can chime, with the words the rest of the app uses for them.
SOUND_STATES = [
    ("ask", "許可待ち"),
    ("notify", "入力待ち・通知"),
    ("done", "完了 / 許可"),
    ("error", "エラー / 拒否"),
    ("busy", "実行開始"),
    ("idle", "待機に戻ったとき"),
    ("rec", "録音開始"),
    ("stt", "文字起こし開始"),
    ("warmup", "マイク準備"),
]
SOUND_DIR = "/System/Library/Sounds"
SOUND_NONE = "なし"

# Diagram colours per role. System colours so they follow light/dark mode.
ROLE_COLOUR = {
    ROLE_APPROVE: "systemGreenColor",
    ROLE_DENY: "systemRedColor",
    ROLE_INTERRUPT: "systemOrangeColor",
    ROLE_PROMPT: "systemPurpleColor",
}


def _centred_text(text, cx, y, size, colour, bold=False):
    font = NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size)
    attributed = NSAttributedString.alloc().initWithString_attributes_(
        text, {NSFontAttributeName: font, NSForegroundColorAttributeName: colour})
    attributed.drawAtPoint_((cx - attributed.size().width / 2.0, y))


class ButtonDiagram(NSView):
    """Top view of the device: stick in the middle, all eight switches on the
    ring, each labelled with whatever it is currently assigned to.

    SW1 (left), SW5 (right) and SW7 (front) were measured with the user; the
    rest are placed by the only spacing consistent with those three -- 45
    degrees apart, numbered clockwise from the left through the back.
    """

    # angle = 180 - 45*(n-1): SW1 left, SW3 back, SW5 right, SW7 front.
    RX, RY = 100.0, 84.0

    def initWithFrame_(self, frame):
        self = objc.super(ButtonDiagram, self).initWithFrame_(frame)
        if self is None:
            return None
        self.roles = {}
        return self

    def setRoles_(self, roles):
        self.roles = roles or {}
        self.setNeedsDisplay_(True)

    @objc.python_method
    def _button(self, cx, cy, sw):
        import math
        n = int(sw)
        angle = math.radians(180.0 - 45.0 * (n - 1))
        bx = cx + self.RX * math.cos(angle)
        by = cy + self.RY * math.sin(angle)

        role = self.roles.get(sw) or ROLE_NONE
        name = ROLE_COLOUR.get(role)
        colour = getattr(NSColor, name)() if name else NSColor.tertiaryLabelColor()
        colour.colorWithAlphaComponent_(0.18).set()
        circle = NSBezierPath.bezierPathWithOvalInRect_(((bx - 14, by - 14), (28, 28)))
        circle.fill()
        colour.set()
        circle.setLineWidth_(2.0)
        circle.stroke()
        _centred_text("SW" + sw, bx, by - 5, 9, colour)
        if role != ROLE_NONE:
            lx = cx + (self.RX + 36) * math.cos(angle)
            ly = cy + (self.RY + 26) * math.sin(angle) - 6
            _centred_text(role, lx, ly, 11, NSColor.labelColor())

    def drawRect_(self, rect):
        w = self.bounds().size.width
        cx, cy = w / 2.0, 128.0

        NSColor.tertiaryLabelColor().set()
        ring = NSBezierPath.bezierPathWithOvalInRect_(
            ((cx - self.RX, cy - self.RY), (self.RX * 2, self.RY * 2)))
        ring.setLineWidth_(2.0)
        ring.stroke()
        _centred_text("↑ USB（奥）", cx + self.RX + 40, cy + self.RY - 10, 10,
                      NSColor.secondaryLabelColor())

        stick = NSBezierPath.bezierPathWithOvalInRect_(((cx - 38, cy - 38), (76, 76)))
        NSColor.secondaryLabelColor().set()
        stick.setLineWidth_(2.0)
        stick.stroke()
        _centred_text("スティック", cx, cy + 2, 12, NSColor.labelColor())
        _centred_text("倒すと録音", cx, cy - 16, 10, NSColor.secondaryLabelColor())

        for n in range(1, 9):
            self._button(cx, cy, str(n))


def _label(text, frame, small=False, bold=False):
    field = NSTextField.alloc().initWithFrame_(frame)
    field.setStringValue_(text)
    field.setBezeled_(False)
    field.setDrawsBackground_(False)
    field.setEditable_(False)
    field.setSelectable_(False)
    size = 11 if small else 13
    font = NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size)
    field.setFont_(font)
    return field


def _checkbox(title, frame, on, target, action):
    box = NSButton.alloc().initWithFrame_(frame)
    box.setButtonType_(NSButtonTypeSwitch)
    box.setTitle_(title)
    box.setState_(NSControlStateValueOn if on else NSControlStateValueOff)
    box.setTarget_(target)
    box.setAction_(action)
    return box


class SettingsController(NSObject):
    """Owns the window and writes every change straight into the config."""

    def initWithApp_(self, app):
        self = objc.super(SettingsController, self).init()
        if self is None:
            return None
        self.app = app  # LuminellaApp: cfg, daemon, _save_config, notify
        self.window = None
        self.row_popup = {}    # switch number -> role NSPopUpButton
        self.row_text = {}     # switch number -> prompt NSTextField
        self.row_submit = {}   # switch number -> submit NSButton
        self.row_mark = {}     # switch number -> "←" marker label
        self.mic_popup = None
        self.mic_indices = []
        self.diagram = None
        self.sound_popup = {}  # state name -> NSPopUpButton
        self.sound_names = []
        self.check = {}        # config key -> NSButton
        self._loading = False
        return self

    # ---- window ---------------------------------------------------------

    def show(self):
        if self.window is None:
            self._build()
        self._load()
        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
        self.window.makeKeyAndOrderFront_(None)

    def _build(self):
        style = NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, WIN_W, WIN_H), style, 2, False)
        self.window.setTitle_("Clauminella 設定")
        self.window.setReleasedWhenClosed_(False)
        self.window.center()

        tabs = NSTabView.alloc().initWithFrame_(
            NSMakeRect(PAD, PAD, WIN_W - 2 * PAD, WIN_H - 2 * PAD))
        general = NSTabViewItem.alloc().initWithIdentifier_("general")
        general.setLabel_("一般")
        general.setView_(self._build_general())
        buttons = NSTabViewItem.alloc().initWithIdentifier_("buttons")
        buttons.setLabel_("ボタン")
        buttons.setView_(self._build_buttons())
        sounds = NSTabViewItem.alloc().initWithIdentifier_("sounds")
        sounds.setLabel_("音")
        sounds.setView_(self._build_sounds())
        tabs.addTabViewItem_(general)
        tabs.addTabViewItem_(buttons)
        tabs.addTabViewItem_(sounds)
        self.window.contentView().addSubview_(tabs)

    def _build_general(self):
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WIN_W - 2 * PAD, WIN_H - 2 * PAD))
        height = view.frame().size.height
        y = height - 40

        toggles = [
            ("notify_on_ask", "許可待ちを通知"),
            ("notify_on_idle", "入力待ちを通知"),
            ("focus_on_ask", "許可待ちでそのセッションを前面に出す"),
            ("off_when_display_sleeps", "画面が消えたらリングを消灯"),
            ("paste_to_focused", "文字起こしを入力欄に直接入力（アクセシビリティ権限が必要）"),
        ]
        for key, title in toggles:
            box = _checkbox(title, NSMakeRect(PAD, y, 480, ROW_H),
                            bool(self.app.cfg.get(key)), self, "toggleChanged:")
            box.setIdentifier_(key)
            self.check[key] = box
            view.addSubview_(box)
            y -= ROW_H + GAP

        y -= 8
        view.addSubview_(_label("マイク:", NSMakeRect(PAD, y + 2, 60, 20)))
        self.mic_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(PAD + 64, y, 340, 26), False)
        self.mic_popup.setTarget_(self)
        self.mic_popup.setAction_("micChanged:")
        view.addSubview_(self.mic_popup)
        return view

    def _build_buttons(self):
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WIN_W - 2 * PAD, WIN_H - 2 * PAD))
        width = view.frame().size.width
        height = view.frame().size.height
        y = height - 272

        self.diagram = ButtonDiagram.alloc().initWithFrame_(
            NSMakeRect((width - 380) / 2.0, y, 380, 265))
        view.addSubview_(self.diagram)
        y -= 32

        pick = NSButton.alloc().initWithFrame_(NSMakeRect(PAD, y, 220, ROW_H))
        pick.setTitle_("本体のボタンを押して行を探す…")
        pick.setBezelStyle_(1)
        pick.setTarget_(self)
        pick.setAction_("pickSwitch:")
        view.addSubview_(pick)
        y -= ROW_H + 10

        # Header
        view.addSubview_(_label("ボタン", NSMakeRect(PAD, y, 60, 18), small=True, bold=True))
        view.addSubview_(_label("役割", NSMakeRect(PAD + 66, y, 130, 18), small=True, bold=True))
        view.addSubview_(_label("定型プロンプトの文面", NSMakeRect(PAD + 206, y, 200, 18),
                                small=True, bold=True))
        view.addSubview_(_label("即送信", NSMakeRect(PAD + 428, y, 60, 18), small=True, bold=True))
        y -= 24

        for sw in SWITCHES:
            mark = _label("", NSMakeRect(2, y + 3, 14, 18), bold=True)
            self.row_mark[sw] = mark
            view.addSubview_(mark)
            view.addSubview_(_label("SW" + sw, NSMakeRect(PAD, y + 3, 46, 18)))

            popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
                NSMakeRect(PAD + 62, y, 138, ROW_H), False)
            popup.addItemsWithTitles_(ROLES)
            popup.setIdentifier_(sw)
            popup.setTarget_(self)
            popup.setAction_("roleChanged:")
            self.row_popup[sw] = popup
            view.addSubview_(popup)

            text = NSTextField.alloc().initWithFrame_(NSMakeRect(PAD + 206, y + 1, 214, 24))
            text.setIdentifier_(sw)
            text.setDelegate_(self)
            self.row_text[sw] = text
            view.addSubview_(text)

            submit = _checkbox("", NSMakeRect(PAD + 440, y + 3, 30, 20), True,
                               self, "rowChanged:")
            submit.setIdentifier_(sw)
            self.row_submit[sw] = submit
            view.addSubview_(submit)
            y -= ROW_H + 4

        view.addSubview_(_label(
            "定型プロンプトは押した瞬間にそのセッションへ送られます。\n"
            "SW8 は「離した」信号を送らないため、単発の役割には使えますが長押し系には不向きです。",
            NSMakeRect(PAD, y - 34, width - 2 * PAD, 34), small=True))
        return view

    def _build_sounds(self):
        view = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, WIN_W - 2 * PAD, WIN_H - 2 * PAD))
        height = view.frame().size.height
        y = height - 40

        box = _checkbox("効果音を鳴らす", NSMakeRect(PAD, y, 300, ROW_H),
                        bool(self.app.cfg.get("sound")), self, "toggleChanged:")
        box.setIdentifier_("sound")
        self.check["sound"] = box
        view.addSubview_(box)
        y -= ROW_H + 14

        try:
            self.sound_names = sorted(
                name[:-5] for name in os.listdir(SOUND_DIR) if name.endswith(".aiff"))
        except OSError:
            self.sound_names = []

        for state, label in SOUND_STATES:
            view.addSubview_(_label(label, NSMakeRect(PAD, y + 4, 170, 20)))
            popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
                NSMakeRect(PAD + 176, y, 180, ROW_H), False)
            popup.addItemWithTitle_(SOUND_NONE)
            for name in self.sound_names:
                popup.addItemWithTitle_(name)
            popup.setIdentifier_(state)
            popup.setTarget_(self)
            popup.setAction_("soundChanged:")
            self.sound_popup[state] = popup
            view.addSubview_(popup)
            y -= ROW_H + GAP

        view.addSubview_(_label(
            "選ぶとその場で試聴できます。実行開始・待機は毎ターン鳴るので、既定では「なし」です。",
            NSMakeRect(PAD, y - 16, WIN_W - 4 * PAD, 20), small=True))
        return view

    def soundChanged_(self, sender):
        if self._loading:
            return
        state = str(sender.identifier())
        title = sender.titleOfSelectedItem()
        name = None if title == SOUND_NONE else title
        sounds = dict(self.app.cfg.get("sounds") or {})
        sounds[state] = name
        self.app.cfg["sounds"] = sounds
        self.app.daemon.cfg["sounds"] = sounds
        self.app._save_config({"sounds": sounds})
        if name:
            path = os.path.join(SOUND_DIR, name + ".aiff")
            subprocess.Popen(["/usr/bin/afplay", path])

    # ---- state <-> widgets ----------------------------------------------

    def _load(self):
        """Fill every control from the config, without firing save-backs."""
        self._loading = True
        try:
            cfg = self.app.cfg
            for key, box in self.check.items():
                box.setState_(NSControlStateValueOn if cfg.get(key)
                              else NSControlStateValueOff)

            approve = str(cfg.get("approve_switch") or "")
            deny = str(cfg.get("deny_switch") or "")
            actions = cfg.get("switch_actions") or {}
            for sw in SWITCHES:
                role = ROLE_NONE
                text, submit = "", True
                if sw == approve:
                    role = ROLE_APPROVE
                elif sw == deny:
                    role = ROLE_DENY
                else:
                    bound = actions.get(sw) or {}
                    if bound.get("type") == "interrupt":
                        role = ROLE_INTERRUPT
                    elif bound.get("type") == "prompt":
                        role = ROLE_PROMPT
                        text = bound.get("text") or ""
                        submit = bool(bound.get("submit", True))
                self.row_popup[sw].selectItemWithTitle_(role)
                self.row_text[sw].setStringValue_(text)
                self.row_submit[sw].setState_(
                    NSControlStateValueOn if submit else NSControlStateValueOff)
                self.row_mark[sw].setStringValue_("")
                self._sync_row_enabled(sw)

            sounds = cfg.get("sounds") or {}
            for state, popup in self.sound_popup.items():
                popup.selectItemWithTitle_(sounds.get(state) or SOUND_NONE)

            self._load_mics()
            self._update_diagram()
        finally:
            self._loading = False

    def _load_mics(self):
        from luminella import ptt
        devices = self.app.mics or ptt.list_input_devices()
        self.app.mics = devices
        self.mic_popup.removeAllItems()
        self.mic_indices = [i for i, _ in devices]
        for i, name in devices:
            self.mic_popup.addItemWithTitle_("%d: %s" % (i, name))
        current = int(self.app.cfg.get("mic_index", 0))
        if current in self.mic_indices:
            self.mic_popup.selectItemAtIndex_(self.mic_indices.index(current))

    @objc.python_method
    def _update_diagram(self):
        if self.diagram is None:
            return
        roles = {}
        for sw in SWITCHES:
            role = self.row_popup[sw].titleOfSelectedItem()
            if role and role != ROLE_NONE:
                roles[sw] = role
        self.diagram.setRoles_(roles)

    @objc.python_method
    def _sync_row_enabled(self, sw):
        is_prompt = self.row_popup[sw].titleOfSelectedItem() == ROLE_PROMPT
        self.row_text[sw].setEnabled_(is_prompt)
        self.row_submit[sw].setEnabled_(is_prompt)

    @objc.python_method
    def _save(self, updates):
        self.app.cfg.update(updates)
        self.app.daemon.cfg.update(updates)
        self.app._save_config(updates)

    # ---- actions --------------------------------------------------------

    def toggleChanged_(self, sender):
        if self._loading:
            return
        key = str(sender.identifier())
        enabled = sender.state() == NSControlStateValueOn
        if key == "paste_to_focused" and enabled:
            from luminella import ptt
            if not ptt.accessibility_trusted(prompt=True):
                alert = NSAlert.alloc().init()
                alert.setMessageText_("アクセシビリティの許可が必要です")
                alert.setInformativeText_(
                    "システム設定 → プライバシーとセキュリティ → アクセシビリティで\n"
                    "Clauminella を許可してください。許可するまではクリップボードにのみ入ります。")
                alert.runModal()
        self._save({key: enabled})
        if key == "sound" and enabled:
            self.app.play_state_sound("done")

    def micChanged_(self, sender):
        if self._loading:
            return
        row = sender.indexOfSelectedItem()
        if 0 <= row < len(self.mic_indices):
            self._save({"mic_index": self.mic_indices[row]})

    def roleChanged_(self, sender):
        if self._loading:
            return
        sw = str(sender.identifier())
        role = sender.titleOfSelectedItem()
        # 許可 and 拒否 belong to exactly one switch: selecting one here takes
        # it away from whichever row held it.
        if role in (ROLE_APPROVE, ROLE_DENY):
            for other, popup in self.row_popup.items():
                if other != sw and popup.titleOfSelectedItem() == role:
                    popup.selectItemWithTitle_(ROLE_NONE)
                    self._sync_row_enabled(other)
        self._sync_row_enabled(sw)
        self._save_switches()

    def rowChanged_(self, sender):
        if self._loading:
            return
        self._save_switches()

    def controlTextDidChange_(self, note):
        if self._loading:
            return
        self._save_switches()

    def _save_switches(self):
        approve, deny = "", ""
        actions = {}
        for sw in SWITCHES:
            role = self.row_popup[sw].titleOfSelectedItem()
            if role == ROLE_APPROVE:
                approve = sw
            elif role == ROLE_DENY:
                deny = sw
            elif role == ROLE_INTERRUPT:
                actions[sw] = {"type": "interrupt"}
            elif role == ROLE_PROMPT:
                actions[sw] = {
                    "type": "prompt",
                    "text": str(self.row_text[sw].stringValue()),
                    "submit": self.row_submit[sw].state() == NSControlStateValueOn,
                }
        # switch_actions is replaced wholesale, not merged: a row set back to
        # なし must actually go away. daemon.cfg is swapped the same way.
        self.app.cfg["switch_actions"] = actions
        self.app.daemon.cfg["switch_actions"] = actions
        self.app.cfg["approve_switch"] = approve
        self.app.cfg["deny_switch"] = deny
        self.app.daemon.cfg["approve_switch"] = approve
        self.app.daemon.cfg["deny_switch"] = deny
        self.app._save_config({
            "approve_switch": approve,
            "deny_switch": deny,
            "switch_actions": actions,
        })
        self._update_diagram()

    def pickSwitch_(self, sender):
        """Light the ring, wait for a press, and point at that row."""
        daemon = self.app.daemon
        if not daemon.running:
            return
        for mark in self.row_mark.values():
            mark.setStringValue_("")
        previous = daemon.current()
        daemon.set_state("notify")

        def worker():
            switch = daemon.read_switch(20)
            daemon.set_state(previous)
            if switch is not None:
                self.performSelectorOnMainThread_withObject_waitUntilDone_(
                    "markSwitch:", str(switch), False)

        threading.Thread(target=worker, daemon=True).start()

    def markSwitch_(self, sw):
        sw = str(sw)
        if sw in self.row_mark:
            self.row_mark[sw].setStringValue_("→")
