# claude-luminella

BRAIN MAGIC **Luminella** を Claude Code の物理ステータスランプ兼承認ボタンにする。

- グロウリングの色で Claude Code の状態が分かる
- 許可プロンプトが出たら**リングが橙に点滅**し、**物理ボタンで許可/拒否**できる

Luminella Core / Orbital2 Core を介さず、シリアルポートを直接叩いている。

## プロトコル

公開仕様がないため、設定ソフトの通信処理を調べて把握し、実機で検証した。
以下は Luminella での実測にもとづく。

```
ポート   /dev/cu.SLAB_USBtoUART   (VID 3525 / PID 0002 = LightModel = Luminella)
通信     9600 baud / 8bit / パリティ EVEN / ストップ 1
符号化   論理1バイトを <byte> 0x00 の2バイトで送る。フレーム終端は ';'

送信
  O   4f 00 00 00 3b 00              ハンドシェイク -> "OK;" が返る
  T   54 00 R 00 G 00 B 00 3b 00     グロウリング色
  R   52 00 00 00 3b 00              スティック中心位置リセット
  M   4d 00 P 00 3b 00               振動 (Orbital2 のみ。Luminella には非搭載)

受信 (';' 終端、2バイトのタグ + 固定長ペイロード)
  OK  0 byte           ハンドシェイク応答
  JS  4 byte           'X' <x> 'Y' <y>   スティック位置 (0x80 が中心)
  RE  2 byte           ロータリーエンコーダ (Orbital2 のみ)
  SW  3 byte           ASCII "<n>=<0|1>"  スイッチ n の押下(1)/解放(0)
  RC  3 byte           フラットリング
```

パリティ EVEN が肝。none で開くと**一切応答しない**。

## 導入

`dist/Luminella.dmg` を開き、`Luminella.app` を Applications にドラッグする。
起動するとメニューバーに常駐する（Dock には出ない）。

メニューから **「フックを導入」** を選ぶと、`~/.claude/settings.json` に
Luminella のフックが追記される。既存のフックには触らず追記のみで、
実行前にタイムスタンプ付きバックアップが作られる。**「フックを解除」** で元に戻る。

`~/.claude/luminella/hook.py` は**標準ライブラリだけ**で書かれていて
`/usr/bin/python3` で動く。アプリを消しても Claude Code は壊れない
（ソケットに繋がらなければ何も出力せず通常フローに落ちる）。

### メニュー

| 項目 | 内容 |
|---|---|
| 状態 / デバイス | 現在の状態と接続状況 |
| 許可・拒否ボタンを割り当て | リングが光っている間に押したボタンを記録 |
| フックを導入 / 解除 | `~/.claude/settings.json` の編集 |
| 設定ファイル・ログを開く | |
| デバイスに再接続 | Core を終了させた後などに |

アプリ未起動でも、Claude Code の `SessionStart` フックが
バンドル ID (`com.miyashita.luminella`) 経由で自動起動する。
そのため「ログイン時に起動」は用意していない。

## 構成

```
menubar/app.py          メニューバーアプリ本体（rumps）。デーモンを内包し監督する
luminella/protocol.py   フレーム符号化・パース・ハンドシェイク
luminella/config.py     既定値と ~/.claude/luminella/config.json の読み込み
luminella/daemon.py     シリアル保持 + unix socket サーバ + LED描画
luminella/hookinstall.py settings.json への追記・除去
luminella/client.py     デーモンへの薄いクライアント（CLI/検証用）
hooks/luminella_hook.py Claude Code フックの入口。標準ライブラリのみ
setup.py                py2app 設定
tools/build_app.sh      .app と .dmg のビルド
tools/make_icon.py      アイコン生成（Quartz で描画）
tools/map_buttons.py    ボタン割り当て（CLI版）
tools/learn_buttons.py  生フレームのダンプ（解析用）
tools/restart.sh        デーモン再起動（CLI版）
```

デーモンを挟んでいるのは、ポートを有用に保持できるプロセスが1つだけであることと、
フックが高頻度で発火するのに毎回 open + handshake のコストは払えないため。
アニメーション描画とスイッチ読み取りもデーモンが担当する。
メニューバーアプリはこのデーモンを監督し、デバイスの抜き差しで自動復帰する。

## ビルド

```sh
tools/build_app.sh                                  # ad-hoc 署名（既定）
SIGN_ID="Developer ID Application: ..." tools/build_app.sh
```

既定は ad-hoc 署名。Apple Development 証明書で署名しても、
受け取る側の手間は ad-hoc と変わらない（どちらも初回に手動で開く必要がある）のに
証明書の期限と失効リスクを負うだけなので使わない。
そのまま開けるようにするには Developer ID + notarization が要る。

配布した相手がやること（macOS 15 以降は右クリック→開くが効かない）:

**システム設定 → プライバシーとセキュリティ → 「このまま開く」**

またはコマンドで:

```sh
xattr -d com.apple.quarantine /Applications/Luminella.app
```

`codesign --deep` は**使えない**。バンドル内の `.so` と同梱 python の
Team ID が食い違い、dyld が
`different Team IDs` で読み込みを拒否してアプリが起動しなくなる。
`build_app.sh` は Mach-O を内側から順に同一 ID で署名し、最後にバンドルを封じている。

## 状態と色

| Claude Code | 状態名 | 色 | 表現 |
|---|---|---|---|
| 待機 | `idle` | 暗い青 | 点灯 |
| 実行中 | `busy` | シアン | 呼吸 |
| **許可待ち** | `ask` | **橙** | **点滅** |
| 通知 | `notify` | 紫 | 点滅 |
| エラー / 拒否 | `error` | 赤 | 点灯 |
| 完了 / 許可 | `done` | 緑 | 点灯 |

色・モードは `~/.claude/luminella/config.json` の `states` で上書きできる。

## 承認フロー

`PermissionRequest` フックに配線している。このイベントは**実際に許可が必要になった時だけ**発火する
（`PreToolUse` と違い、全ツール呼び出しを止めない）。

1. 許可が必要になる → リングが橙に点滅
2. **許可ボタン** → `{"decision":{"behavior":"allow"}}` を返す（緑が一瞬光る）
3. **拒否ボタン** → `{"decision":{"behavior":"deny"}}` を返す（赤が一瞬光る）
4. `ask_timeout` 秒（既定 30）無操作 → **何も出力せず終了**し、通常の画面プロンプトに委ねる

デーモンやデバイスが落ちていても、全経路が「何も出力せず exit 0」に倒れる。
リングはあくまで表示と入力補助であり、**フェイルオープンする関門にはしていない**。

## 設定

`~/.claude/luminella/config.json`

```json
{
  "approve_switch": "7",
  "deny_switch": "5",
  "gated_tools": [],
  "ask_timeout": 30
}
```

- `approve_switch` / `deny_switch` — `tools/map_buttons.py` で対話的に設定できる
- `gated_tools` — ここに入れたツールは `PreToolUse` の段階で必ずボタン待ちになる。
  `PermissionRequest` 経由で足りるので通常は空でよい
- 変更後は `tools/restart.sh`

## 注意

- **Luminella Core / Orbital2 Core とは同時に使えない。** 同じシリアルポートを取り合う。
  デーモンが `handshake no reply` を吐く時はまずこれを疑う
- `PermissionRequest` には `claude-remote-approver` も同居している。
  両方が判断を返し得るため、遠隔承認と物理ボタンを同時に使う場合は挙動を確認すること
- デバイスを抜くとデーモンは書き込み失敗を検知して自己終了する。
  次のフック発火時に自動で起動し直す

## ログ

```
~/.claude/luminella/daemon.log
```

## ライセンス

GPLv3。詳細は [LICENSE](LICENSE) を参照。

Claude Luminella は株式会社ブレインマジックおよび Anthropic とは関係のない
独立した製品。Luminella、Orbital2 は株式会社ブレインマジックの商標、
Claude、Claude Code は Anthropic PBC の商標であり、
対応する製品を示す目的でのみ使用している。
