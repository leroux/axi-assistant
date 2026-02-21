#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

RESTART_EXIT_CODE=42

while true; do
    code=0
    uv run python bot.py || code=$?
    if [ $code -eq $RESTART_EXIT_CODE ]; then
        echo "Restart requested, relaunching..."
        continue
    fi
    echo "Bot exited with code $code, stopping."
    exit $code
done
