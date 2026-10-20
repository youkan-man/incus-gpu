#!/usr/bin/env bash
set -Eeuo pipefail

PREFIX=${PREFIX:-/usr/local}
DESTDIR=${DESTDIR:-}
TARGET="$DESTDIR$PREFIX/sbin/incus-gpu"

if [[ $EUID -ne 0 && -z "$DESTDIR" ]]; then
    echo "アンインストールにはroot権限が必要です。sudo ./uninstall.sh を実行してください。" >&2
    exit 1
fi

rm -f "$TARGET" "$DESTDIR$PREFIX/libexec/incus-gpu/runtime.py"
echo "Removed: $TARGET"
echo "管理したGRUB設定は保持します。解除はアンインストール前に prepare-host --undo を実行してください。"
