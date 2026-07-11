#!/bin/bash
# push_health.sh — cron-friendly wrapper for push_health_dashboard()
# Pushes a system health snapshot to Canvas after each tick batch.
#
# Usage:
#   scripts/push_health.sh                          # push to default 'trading' board
#   scripts/push_health.sh --board main             # override board
#   scripts/push_health.sh --junit reports/junit.xml # include test pass rate
#   scripts/push_health.sh --board main --junit reports/junit.xml
#
# Designed for cron: uses absolute paths, explicit PATH, and writes
# output to a timestamped log so failures are traceable.

set -euo pipefail

REPO_DIR="${REPO_DIR:-$HOME/projects/paper-trading-rebuild}"
LOG_DIR="${LOG_DIR:-$REPO_DIR/logs}"
TIMESTAMP=$(date -u +%Y%m%d-%H%M)

cd "$REPO_DIR"

# Ensure log directory exists
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/push_health_${TIMESTAMP}.log"

# Explicit PATH for cron environments
export PATH="/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin:$PATH"

# ----- Parse args -----
BOARD="trading"
JUNIT_ARG=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --board)
            BOARD="$2"
            shift 2
            ;;
        --junit)
            JUNIT_ARG=(--junit "$2")
            shift 2
            ;;
        *)
            echo "Unknown arg: $1"
            echo "Usage: $0 [--board <name>] [--junit <path>]"
            exit 2
            ;;
    esac
done

# ----- Run -----
{
    echo "=== push_health.sh @ $TIMESTAMP ==="
    echo "Board: $BOARD"
    echo "JUnit: ${JUNIT_ARG[@]:-none}"
    echo ""

    python3 src/canvas_dashboard.py \
        --board "$BOARD" \
        "${JUNIT_ARG[@]}"

    echo ""
    echo "=== Done ==="
} 2>&1 | tee "$LOG_FILE"

# Keep only last 48 logs to avoid unbounded growth
ls -1t "$LOG_DIR"/push_health_*.log 2>/dev/null | tail -n +49 | xargs rm -f 2>/dev/null || true
