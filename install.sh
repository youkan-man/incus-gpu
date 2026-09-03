#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PREFIX=${PREFIX:-/usr/local}
DESTDIR=${DESTDIR:-}
TARGET="$DESTDIR$PREFIX/sbin/incus-gpu"

if [[ ! -f "$ROOT_DIR/incus-gpu" ]]; then
    echo "incus-gpu が見つかりません: $ROOT_DIR/incus-gpu" >&2
    exit 1
fi

if [[ $EUID -ne 0 && -z "$DESTDIR" ]]; then
    echo "インストールにはroot権限が必要です。sudo ./install.sh を実行してください。" >&2
    exit 1
fi

install -D -m 0755 "$ROOT_DIR/incus-gpu" "$TARGET"
echo "Installed: $TARGET"
echo "Run: incus-gpu --help"
