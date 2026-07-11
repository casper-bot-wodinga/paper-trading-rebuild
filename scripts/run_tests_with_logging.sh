#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# run_tests_with_logging — run all tests, persist JUnit XML, push Canvas card
# ──────────────────────────────────────────────────────────────────────────────
# Usage:
#   ./scripts/run_tests_with_logging.sh                           # full suite
#   ./scripts/run_tests_with_logging.sh --coverage                 # with coverage
#   ./scripts/run_tests_with_logging.sh -m smoke                   # smoke tests only
#   ./scripts/run_tests_with_logging.sh tests/test_trader.py       # single file
#
# Output:
#   logs/test-results/junit.xml           — JUnit XML (parsed by Canvas dashboard)
#   logs/test-results/coverage/           — coverage report (if --coverage)
#   logs/test-results/test-output.log     — full test output
#   logs/test-results/timestamp           — last run timestamp
#
# After tests complete, optionally pushes a Canvas observability card.
# Set CANVAS_BOARD=trading (default) or CANVAS_BOARD=main in env.
# ──────────────────────────────────────────────────────────────────────────────

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT_DIR"

RESULTS_DIR="logs/test-results"
mkdir -p "$RESULTS_DIR" "$RESULTS_DIR/coverage"

TIMESTAMP=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
echo "$TIMESTAMP" > "$RESULTS_DIR/timestamp"

EXTRA_ARGS=()

# Parse --coverage flag
COVERAGE=false
for arg in "$@"; do
    if [ "$arg" == "--coverage" ]; then
        COVERAGE=true
    else
        EXTRA_ARGS+=("$arg")
    fi
done

echo "🔄 Running tests... (coverage=$COVERAGE)" >&2

if [ "$COVERAGE" = true ]; then
    python3 -m pytest "${EXTRA_ARGS[@]}" \
        --junitxml="$RESULTS_DIR/junit.xml" \
        --cov=src \
        --cov-report=html:"$RESULTS_DIR/coverage/html" \
        --cov-report=term-missing:skip-covered \
        2>&1 | tee "$RESULTS_DIR/test-output.log"
    TEST_EXIT=${PIPESTATUS[0]}
else
    python3 -m pytest "${EXTRA_ARGS[@]}" \
        --junitxml="$RESULTS_DIR/junit.xml" \
        2>&1 | tee "$RESULTS_DIR/test-output.log"
    TEST_EXIT=${PIPESTATUS[0]}
fi

echo "✅ Test exit code: $TEST_EXIT" >&2

# Push Canvas observability card (best-effort)
if [ -f src/canvas_dashboard.py ]; then
    echo "🔄 Pushing Canvas observability card..." >&2
    python3 -m src.canvas_dashboard \
        --board "${CANVAS_BOARD:-trading}" \
        --junit "$RESULTS_DIR/junit.xml" \
        --expires 1 \
        2>&1 || echo "⚠️  Canvas push skipped (not fatal)" >&2
fi

exit $TEST_EXIT