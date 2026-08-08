"""py2app build for the Luminella menu bar app.

    python setup.py py2app

Prefer tools/build_app.sh, which stages resources, signs, and builds the dmg.
"""

import os

from setuptools import setup

ROOT = os.path.dirname(os.path.abspath(__file__))

APP = ["menubar/app.py"]
DATA_FILES = [os.path.join(ROOT, "build", "stage", "hook.py")]

OPTIONS = {
    "argv_emulation": False,
    "iconfile": os.path.join(ROOT, "build", "Luminella.icns"),
    "packages": ["rumps", "serial", "luminella", "Quartz", "ApplicationServices"],
    "includes": ["serial.tools", "serial.tools.list_ports"],
    "plist": {
        "CFBundleName": "Luminella",
        "CFBundleDisplayName": "Luminella",
        "CFBundleIdentifier": "com.miyashita.luminella",
        "CFBundleVersion": "1.2.2",
        "CFBundleShortVersionString": "1.2.2",
        "LSUIElement": True,  # menu bar only: no Dock icon, no menu bar app menu
        # Push-to-talk records from the microphone; macOS requires this string
        # before it will even prompt, and denies silently without it.
        "NSMicrophoneUsageDescription":
            "押して話す機能で、ボタンを押している間だけ音声を録音し、この Mac 上で文字に変換します。",
        # Pasting the transcript into the focused field drives System Events.
        "NSAppleEventsUsageDescription":
            "文字起こしした内容を、入力中のアプリに貼り付けるために使用します。",
        # The bundled interpreter is built against the macOS 26 SDK, so the
        # binaries will not load on anything older. Declaring 12.0 was simply
        # untrue.
        "LSMinimumSystemVersion": "26.0",
        "NSHumanReadableCopyright": "Luminella glow ring bridge for Claude Code",
    },
}

setup(
    name="Luminella",
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
