#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

RESTART_EXIT_CODE=42

# Create default user data files if they don't exist
[ -f USER_PROFILE.md ] || cat > USER_PROFILE.md <<'EOF'
# User Profile

This is a currently blank user profile. It will be updated over time.
EOF

[ -f schedules.json ] || echo '[]' > schedules.json
[ -f schedule_history.json ] || echo '[]' > schedule_history.json

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
