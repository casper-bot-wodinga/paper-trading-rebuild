#!/usr/bin/env bash
# ============================================================================
# run_learning_loop.sh — Nightly cron wrapper for the closed learning loop.
#
# Runs python3 -m src.learning_loop --agent <trader-id> for each trader.
# Uses flock to prevent overlapping runs. Errors are logged per-trader
# without failing the whole batch.
#
# Card: 32b861ee-9d2a-4f52-b9a5-f81d80290356
#
# Usage:
#   ./scripts/run_learning_loop.sh            # run for all traders
#   ./scripts/run_learning_loop.sh kairos     # run for a single trader
# ============================================================================

set -o pipefail

# ── Paths ────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LOCK_FILE="${PROJECT_DIR}/state/learning_loop.lock"
LOG_FILE="${PROJECT_DIR}/state/learning_loop.log"
STATE_DIR="${PROJECT_DIR}/state"

# Ensure state directory exists
mkdir -p "$STATE_DIR"

# ── Logging helpers ──────────────────────────────────────────────────────────
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"
}

log_error() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ERROR: $*" | tee -a "$LOG_FILE" >&2
}

# ── Flock guard ──────────────────────────────────────────────────────────────
exec {LOCK_FD}>"$LOCK_FILE"

if ! flock -n "$LOCK_FD"; then
    log "Another learning_loop instance is already running (lock: $LOCK_FILE). Exiting."
    exit 0
fi

# Cleanup lock on exit
cleanup() {
    flock -u "$LOCK_FD" 2>/dev/null || true
}
trap cleanup EXIT

# ── Determine traders to process ─────────────────────────────────────────────
if [ $# -gt 0 ]; then
    # Specific trader(s) passed on command line
    TRADERS=("$@")
else
    # Default: all three paper traders
    TRADERS=("kairos" "aldridge" "stonks")
fi

# ── Run learning loop for each trader ────────────────────────────────────────
log "=== Learning loop started for traders: ${TRADERS[*]} ==="

SUCCESS_COUNT=0
FAIL_COUNT=0
FAILED_TRADERS=()

for trader_id in "${TRADERS[@]}"; do
    log "--- Running learning loop for trader: ${trader_id} ---"

    START_TS=$(date +%s)

    if python3 -m src.learning_loop --agent "$trader_id" >> "$LOG_FILE" 2>&1; then
        ELAPSED=$(( $(date +%s) - START_TS ))
        log "✓ ${trader_id}: completed successfully (${ELAPSED}s)"
        SUCCESS_COUNT=$((SUCCESS_COUNT + 1))
    else
        EXIT_CODE=$?
        ELAPSED=$(( $(date +%s) - START_TS ))
        log_error "✗ ${trader_id}: FAILED with exit code ${EXIT_CODE} (${ELAPSED}s)"
        FAIL_COUNT=$((FAIL_COUNT + 1))
        FAILED_TRADERS+=("$trader_id")
    fi
done

# ── Summary ──────────────────────────────────────────────────────────────────
log "=== Learning loop finished ==="
log "Results: ${SUCCESS_COUNT} succeeded, ${FAIL_COUNT} failed"

if [ ${#FAILED_TRADERS[@]} -gt 0 ]; then
    log_error "Failed traders: ${FAILED_TRADERS[*]}"
    exit 1
fi

log "All traders completed successfully."
exit 0
