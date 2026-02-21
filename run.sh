#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

RESTART_EXIT_CODE=42
CRASH_THRESHOLD=30
ROLLBACK_MARKER=".rollback_performed"

# Create default user data files if they don't exist
[ -f USER_PROFILE.md ] || cat > USER_PROFILE.md <<'EOF'
# User Profile

This is a currently blank user profile. It will be updated over time.
EOF

[ -f schedules.json ] || echo '[]' > schedules.json
[ -f schedule_history.json ] || echo '[]' > schedule_history.json

rollback_attempted=0

while true; do
    start_time=$(date +%s)
    code=0
    uv run python bot.py || code=$?

    # Normal restart requested — reset rollback flag and relaunch
    if [ $code -eq $RESTART_EXIT_CODE ]; then
        echo "Restart requested, relaunching..."
        rollback_attempted=0
        continue
    fi

    # Clean exit — respect it
    if [ $code -eq 0 ]; then
        echo "Clean exit, stopping."
        exit 0
    fi

    # Crash — check if it was a quick startup crash we can rollback
    elapsed=$(( $(date +%s) - start_time ))
    echo "Bot exited with code $code after ${elapsed}s."

    if [ $elapsed -ge $CRASH_THRESHOLD ]; then
        echo "Crash after ${elapsed}s (>= ${CRASH_THRESHOLD}s threshold). Not a startup crash. Stopping."
        exit $code
    fi

    echo "Quick crash detected (${elapsed}s < ${CRASH_THRESHOLD}s threshold)."

    if [ $rollback_attempted -eq 1 ]; then
        echo "Rollback already attempted. Stopping to prevent infinite loop."
        exit $code
    fi

    # Check if git is available and we're in a repo
    if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "Not a git repository. Cannot rollback. Stopping."
        exit $code
    fi

    # Check for uncommitted changes to roll back
    if git diff --quiet HEAD 2>/dev/null && git diff --cached --quiet 2>/dev/null; then
        echo "No uncommitted changes to roll back. Stopping."
        exit $code
    fi

    # Perform the rollback — stash bad changes and revert to last commit
    echo "Uncommitted changes detected. Stashing and rolling back..."
    stash_output=$(git stash push --include-untracked -m "auto-rollback: crash with exit code $code" 2>&1)
    echo "$stash_output"

    # Write rollback marker for bot.py to read on next startup
    stash_output_json=$(python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))" <<< "$stash_output")
    cat > "$ROLLBACK_MARKER" <<ROLLBACK_EOF
{
    "exit_code": $code,
    "uptime_seconds": $elapsed,
    "stash_output": $stash_output_json,
    "timestamp": "$(date -Iseconds)"
}
ROLLBACK_EOF

    echo "Rollback marker written. Relaunching with last committed code..."
    rollback_attempted=1
    continue
done
