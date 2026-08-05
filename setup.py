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
    "packages": ["rumps", "serial", "luminella"],
    "includes": ["serial.tools", "serial.tools.list_ports"],
    "plist": {
        "CFBundleName": "Luminella",
        "CFBundleDisplayName": "Luminella",
        "CFBundleIdentifier": "com.miyashita.luminella",
        "CFBundleVersion": "1.0.0",
        "CFBundleShortVersionString": "1.0.0",
        "LSUIElement": True,  # menu bar only: no Dock icon, no menu bar app menu
        "LSMinimumSystemVersion": "12.0",
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
