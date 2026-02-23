#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

RESTART_EXIT_CODE=42
CRASH_THRESHOLD=60
ROLLBACK_MARKER=".rollback_performed"
CRASH_ANALYSIS_MARKER=".crash_analysis"
LOG_FILE=".bot_output.log"
MAX_RUNTIME_CRASHES=3

# Create default user data files if they don't exist
[ -f USER_PROFILE.md ] || cat > USER_PROFILE.md <<'EOF'
# User Profile

This is a currently blank user profile. It will be updated over time.
EOF

[ -f schedules.json ] || echo '[]' > schedules.json
[ -f schedule_history.json ] || echo '[]' > schedule_history.json

rollback_attempted=0
runtime_crash_count=0

while true; do
    start_time=$(date +%s)

    # Record the commit hash before launch so we can revert committed changes
    pre_launch_commit=$(git rev-parse HEAD 2>/dev/null || echo "")

    set +o pipefail
    uv run python bot.py 2>&1 | tee -a "$LOG_FILE"
    code=${PIPESTATUS[0]}
    set -o pipefail

    # Normal restart requested — reset rollback flag and relaunch
    if [ $code -eq $RESTART_EXIT_CODE ]; then
        echo "Restart requested, relaunching..."
        rollback_attempted=0
        runtime_crash_count=0
        pre_launch_commit=$(git rev-parse HEAD 2>/dev/null || echo "")
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
        runtime_crash_count=$((runtime_crash_count + 1))
        echo "Runtime crash detected (${elapsed}s >= ${CRASH_THRESHOLD}s threshold). Consecutive count: $runtime_crash_count/$MAX_RUNTIME_CRASHES."

        if [ $runtime_crash_count -ge $MAX_RUNTIME_CRASHES ]; then
            echo "Max consecutive runtime crashes ($MAX_RUNTIME_CRASHES) reached. Stopping."
            exit $code
        fi

        # Save last 200 lines of the log for crash analysis
        crash_log_snapshot=".crash_log_snapshot"
        tail -n 200 "$LOG_FILE" > "$crash_log_snapshot" 2>/dev/null || true

        # Write crash analysis marker for bot.py to read on next startup
        crash_log_content=$(python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))" < "$crash_log_snapshot" 2>/dev/null || echo '""')
        cat > "$CRASH_ANALYSIS_MARKER" <<CRASH_EOF
{
    "exit_code": $code,
    "uptime_seconds": $elapsed,
    "timestamp": "$(date -Iseconds)",
    "crash_log": $crash_log_content
}
CRASH_EOF
        rm -f "$crash_log_snapshot"

        echo "Crash analysis marker written. Relaunching for runtime crash recovery..."
        rollback_attempted=0
        continue
    fi

    echo "Quick crash detected (${elapsed}s < ${CRASH_THRESHOLD}s threshold)."

    # Save last 200 lines of the log for crash analysis
    crash_log_snapshot=".crash_log_snapshot"
    tail -n 200 "$LOG_FILE" > "$crash_log_snapshot" 2>/dev/null || true

    if [ $rollback_attempted -eq 1 ]; then
        echo "Rollback already attempted. Stopping to prevent infinite loop."
        exit $code
    fi

    # Check if git is available and we're in a repo
    if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        echo "Not a git repository. Cannot rollback. Stopping."
        exit $code
    fi

    current_commit=$(git rev-parse HEAD 2>/dev/null || echo "")
    has_uncommitted=0
    git diff --quiet HEAD 2>/dev/null && git diff --cached --quiet 2>/dev/null || has_uncommitted=1

    # Nothing to roll back — no new commits and no uncommitted changes
    if [ "$current_commit" = "$pre_launch_commit" ] && [ $has_uncommitted -eq 0 ]; then
        echo "No changes (committed or uncommitted) to roll back. Stopping."
        exit $code
    fi

    rollback_details=""

    # Stash uncommitted changes first (if any)
    if [ $has_uncommitted -eq 1 ]; then
        echo "Stashing uncommitted changes..."
        stash_output=$(git stash push --include-untracked -m "auto-rollback: crash with exit code $code" 2>&1)
        echo "$stash_output"
        rollback_details="uncommitted changes stashed"
    fi

    # Revert committed changes if HEAD moved since pre-launch
    if [ -n "$pre_launch_commit" ] && [ "$current_commit" != "$pre_launch_commit" ]; then
        new_commits=$(git rev-list --count "$pre_launch_commit".."$current_commit" 2>/dev/null || echo "?")
        echo "HEAD moved from ${pre_launch_commit:0:7} to ${current_commit:0:7} ($new_commits new commit(s)). Resetting..."
        git reset --hard "$pre_launch_commit"
        if [ -n "$rollback_details" ]; then
            rollback_details="$rollback_details + $new_commits commit(s) reverted"
        else
            rollback_details="$new_commits commit(s) reverted"
        fi
    fi

    # Write rollback marker for bot.py to read on next startup
    stash_output_json=$(python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))" <<< "${stash_output:-}")
    rollback_details_json=$(python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))" <<< "$rollback_details")
    crash_log_content=$(python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))" < "$crash_log_snapshot" 2>/dev/null || echo '""')
    cat > "$ROLLBACK_MARKER" <<ROLLBACK_EOF
{
    "exit_code": $code,
    "uptime_seconds": $elapsed,
    "stash_output": $stash_output_json,
    "rollback_details": $rollback_details_json,
    "pre_launch_commit": "$pre_launch_commit",
    "crashed_commit": "$current_commit",
    "timestamp": "$(date -Iseconds)",
    "crash_log": $crash_log_content
}
ROLLBACK_EOF
    rm -f "$crash_log_snapshot"

    echo "Rollback marker written. Relaunching with pre-launch code (${pre_launch_commit:0:7})..."
    rollback_attempted=1
    continue
done
