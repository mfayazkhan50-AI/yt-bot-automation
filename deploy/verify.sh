#!/usr/bin/env bash
#
# Read-only health check for the yt-bot service. Safe to run any time.
# Usage: bash deploy/verify.sh

set -uo pipefail

PROJECT_DIR="/root/yt-bot-automation"
SERVICE_NAME="yt-bot"
VENV_PY="${PROJECT_DIR}/venv/bin/python"

echo "=============================================================="
echo " 1. Browser binary"
echo "=============================================================="
"${VENV_PY}" -c "
import sys
sys.path.insert(0, '${PROJECT_DIR}')
from comment import resolve_browser_path
path = resolve_browser_path()
print('CHROMIUM_PATH =', path or 'NOT FOUND')
sys.exit(0 if path else 1)
" || echo "RESULT: browser check FAILED"

echo
echo "=============================================================="
echo " 2. Service state"
echo "=============================================================="
echo "enabled : $(systemctl is-enabled ${SERVICE_NAME} 2>/dev/null)"
echo "active  : $(systemctl is-active ${SERVICE_NAME} 2>/dev/null)"
systemctl show "${SERVICE_NAME}" -p MainPID -p NRestarts -p ActiveEnterTimestamp 2>/dev/null

echo
echo "=============================================================="
echo " 3. Process"
echo "=============================================================="
pgrep -af "app.py" || echo "no app.py process found"

echo
echo "=============================================================="
echo " 4. Daily counters (per business limit)"
echo "=============================================================="
if [ -f "${PROJECT_DIR}/bot_state.json" ]; then
  cat "${PROJECT_DIR}/bot_state.json"
else
  echo "no bot_state.json yet (created on first cycle)"
fi

echo
echo "=============================================================="
echo " 5. Last 30 log lines"
echo "=============================================================="
tail -n 30 "${PROJECT_DIR}/bot.log" 2>/dev/null || echo "(no bot.log yet)"
