#!/usr/bin/env bash
set -Eeuo pipefail

PREFIX=${PREFIX:-/usr/local}
DESTDIR=${DESTDIR:-}
TARGET="$DESTDIR$PREFIX/sbin/incus-gpu"

if [[ $EUID -ne 0 && -z "$DESTDIR" ]]; then
    echo "アンインストールにはroot権限が必要です。sudo ./uninstall.sh を実行してください。" >&2
    exit 1
fi

rm -f "$TARGET"
echo "Removed: $TARGET"
echo "prepare-hostで作成した起動設定も消す場合: sudo incus-gpu prepare-host --undo"
