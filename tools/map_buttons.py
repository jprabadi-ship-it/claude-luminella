#!/usr/bin/env python3
"""Identify which physical switch is which, and write the approve/deny config.

Press the button you want for APPROVE, then the one for DENY. The numbers are
saved to ~/.claude/luminella/config.json.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from luminella import client, config


def capture(label, colour):
    client.request({"cmd": "state", "state": colour}, timeout=2)
    print(f"\n>>> {label} にしたいボタンを押してください (20秒)", flush=True)
    reply = client.request({"cmd": "readsw", "timeout": 20}, timeout=30)
    switch = (reply or {}).get("switch")
    if switch is None:
        print("    タイムアウト（押されませんでした）", flush=True)
    else:
        print(f"    -> SW{switch}", flush=True)
    return switch


def main():
    if not client.ensure_running():
        print("デーモンに接続できません。Orbital2 Core が起動していないか確認してください。")
        return 1

    approve = capture("許可 (approve)", "done")
    deny = capture("拒否 (deny)", "error")
    client.set_state("idle")

    if approve is None or deny is None:
        print("\n両方押されなかったので保存しません。")
        return 1
    if approve == deny:
        print("\n同じボタンなので保存しません。別々のボタンを選んでください。")
        return 1

    os.makedirs(config.STATE_DIR, exist_ok=True)
    try:
        with open(config.CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    cfg["approve_switch"] = approve
    cfg["deny_switch"] = deny
    with open(config.CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")

    print(f"\n保存しました: 許可=SW{approve} / 拒否=SW{deny}")
    print(f"  {config.CONFIG_PATH}")
    print("\n設定を反映するにはデーモンを再起動してください:")
    print("  tools/restart.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
