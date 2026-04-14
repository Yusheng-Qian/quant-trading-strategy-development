#!/bin/bash

BIN_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$BIN_DIR/.." && pwd)"
PYTHON_BIN="$ROOT_DIR/.venv/bin/python"

if [ ! -x "$PYTHON_BIN" ]; then
    ALT_ROOT="$(cd "$ROOT_DIR/../9.0" 2>/dev/null && pwd)"
    if [ -n "$ALT_ROOT" ] && [ -x "$ALT_ROOT/.venv/bin/python" ]; then
        PYTHON_BIN="$ALT_ROOT/.venv/bin/python"
    fi
fi

if [ ! -x "$PYTHON_BIN" ]; then
    echo "未找到可用 Python 虚拟环境。"
    echo "请在 $ROOT_DIR/.venv 或 ../9.0/.venv 中准备依赖。"
    exit 1
fi

exec "$PYTHON_BIN" "$ROOT_DIR/main.py" "$@"

