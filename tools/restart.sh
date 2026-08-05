#!/bin/sh
# Restart the Luminella daemon (picks up config.json changes).
set -e
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

"$ROOT/.venv/bin/python" -c "from luminella import client; client.request({'cmd':'quit'}, timeout=2)" 2>/dev/null || true
pkill -f "luminella.daemon" 2>/dev/null || true
sleep 1

cd "$ROOT"
nohup "$ROOT/.venv/bin/python" -m luminella.daemon >/dev/null 2>&1 &
sleep 2

"$ROOT/.venv/bin/python" -c "
from luminella import client
r = client.request({'cmd':'ping'}, timeout=2)
print('daemon:', 'up' if r else 'DOWN', r or '')
"
