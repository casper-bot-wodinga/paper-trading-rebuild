#!/usr/bin/env bash
# ============================================================================
# Setup Cull Cron — creates cron job for weekly virtual trader culling
#
# Runs Sunday 23:00 ET (Monday 03:00 UTC).
#
# The cull script:
#   - Ranks active virtual traders by 7-day P&L
#   - Culls the bottom N per base trader
#   - Generates replacements, sourcing at least one from prompt_sweep results
#   - Falls back to random variant generation when sweep results are stale
#
# Usage:
#   ./scripts/setup_cull_cron.sh                    # install the cron job
#   ./scripts/setup_cull_cron.sh --remove            # remove the cron job
#   ./scripts/setup_cull_cron.sh --status            # show current cron entries
#   ./scripts/setup_cull_cron.sh --cull-count 4      # cull 4 per base (default: 3)
#
# Env:
#   VT_MOCK=1     — enable mock mode for testing
#   PROJECT_DIR   — override project directory (default: /home/openclaw/projects/paper-trading-rebuild)
#   VENV_DIR      — override venv path (default: PROJECT_DIR/venv)
# ============================================================================

set -euo pipefail

# ── Config ──────────────────────────────────────────────────────────────────

PROJECT_DIR="${PROJECT_DIR:-/home/openclaw/projects/paper-trading-rebuild}"
VENV_DIR="${VENV_DIR:-${PROJECT_DIR}/venv}"
PYTHON="${VENV_DIR}/bin/python3"
CULL_SCRIPT="${PROJECT_DIR}/src/virtual_cull.py"
CRON_MARKER="# setup_cull_cron — do not remove this comment"
CRON_FILE="/tmp/setup_cull_cron_$$.txt"

# ── Defaults ─────────────────────────────────────────────────────────────────

CULL_COUNT=3

# ── Helpers ──────────────────────────────────────────────────────────────────

info()  { echo "ℹ️  $*"; }
ok()    { echo "✅ $*"; }
err()   { echo "❌ $*" >&2; }

# ── Parse args ──────────────────────────────────────────────────────────────

REMOVE=false
STATUS=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cull-count|-c) CULL_COUNT="$2"; shift 2 ;;
        --remove|-r)     REMOVE=true; shift ;;
        --status|-s)     STATUS=true; shift ;;
        --help|-h)       head -30 "$0"; exit 0 ;;
        *)               err "Unknown arg: $1"; exit 1 ;;
    esac
done

# Validate cull count
if ! [[ "$CULL_COUNT" =~ ^[1-9][0-9]*$ ]]; then
    err "Cull count must be a positive integer (got: $CULL_COUNT)"
    exit 1
fi

# ── Status mode ──────────────────────────────────────────────────────────────

if $STATUS; then
    echo "📋 Current cull cron status:"
    crontab -l 2>/dev/null | grep "virtual_cull.py" || echo "   No cull cron job found"
    echo ""
    info "Expected: Sunday 23:00 ET (Monday 03:00 UTC)"
    info "Project dir: $PROJECT_DIR"
    info "Cull count:   $CULL_COUNT"
    info "Mock mode:    ${VT_MOCK:-0}"
    exit 0
fi

# ── Remove mode ──────────────────────────────────────────────────────────────

if $REMOVE; then
    info "Removing cull cron job..."
    crontab -l 2>/dev/null | grep -v "$CRON_MARKER" | grep -v "virtual_cull.py" > "$CRON_FILE" || true
    crontab "$CRON_FILE"
    rm -f "$CRON_FILE"
    ok "Cull cron job removed"
    exit 0
fi

# ── Validate paths ──────────────────────────────────────────────────────────

if [[ ! -f "$CULL_SCRIPT" ]]; then
    err "virtual_cull.py not found at $CULL_SCRIPT"
    exit 1
fi

# ── Build cron entry ─────────────────────────────────────────────────────────

# Build env vars for mock mode
MOCK_ENV=""
if [[ "${VT_MOCK:-0}" == "1" ]]; then
    MOCK_ENV="VT_MOCK=1 "
fi

# Cull cron — runs Sunday 23:00 ET (Monday 03:00 UTC)
# ET is UTC-4 (EDT) or UTC-5 (EST). We use 03:00 UTC (Sun 23:00 ET year-round).
# minute hour day-of-month month day-of-week
CULL_CRON="0 3 * * 1 cd ${PROJECT_DIR} && ${MOCK_ENV}${PYTHON} ${CULL_SCRIPT} >> ${PROJECT_DIR}/logs/cull_cron.log 2>&1"

# ── Apply ────────────────────────────────────────────────────────────────────

# Ensure logs directory exists
mkdir -p "${PROJECT_DIR}/logs"

# Read existing crontab, remove old cull entries, add new one
crontab -l 2>/dev/null | grep -v "$CRON_MARKER" | grep -v "virtual_cull.py" > "$CRON_FILE" || true

# Append new entry
cat >> "$CRON_FILE" << EOF

# $CRON_MARKER
# Weekly virtual trader culling — Sunday 23:00 ET (Monday 03:00 UTC)
$CULL_CRON
EOF

crontab "$CRON_FILE"
rm -f "$CRON_FILE"

# ── Verify ───────────────────────────────────────────────────────────────────

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║           Cull Cron Job Installed                          ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║ Schedule:  Monday 03:00 UTC (Sunday 23:00 ET)              ║"
echo "║ Cull per base:  $CULL_COUNT                                          ║"
echo "║                                                              ║"
echo "║ Script:    ${CULL_SCRIPT}  ║"
echo "║ Log:       ${PROJECT_DIR}/logs/cull_cron.log  ║"
echo "║ Mock mode: ${VT_MOCK:-0}                               ║"
echo "╚══════════════════════════════════════════════════════════════╝"

echo ""
info "Current cron entries:"
crontab -l | grep "virtual_cull.py" || echo "  (none found — unexpected)"

ok "Done!"