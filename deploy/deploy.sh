#!/usr/bin/env bash
#
# yt-bot deployment script for the ArenHost VPS (Ubuntu 22.04, project at /root/yt-bot-automation).
#
# Usage:
#   scp -r deploy <user>@<VPS_IP>:/root/yt-bot-automation/
#   ssh <user>@<VPS_IP>
#   cd /root/yt-bot-automation && bash deploy/deploy.sh
#
# The script is idempotent: run it again after any code update to redeploy.

set -euo pipefail

PROJECT_DIR="/root/yt-bot-automation"
VENV_DIR="${PROJECT_DIR}/venv"
SERVICE_NAME="yt-bot"

cd "${PROJECT_DIR}"

step() { echo; echo "=============================================================="; echo ">> $*"; echo "=============================================================="; }

# ---------------------------------------------------------------------------
step "1/8  Stop any previously running instance"
if command -v systemctl >/dev/null 2>&1; then
  systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
  systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
fi
pkill -f "python app.py" 2>/dev/null || true
# systemd's ExecStart is "<venv>/bin/python <project>/app.py", which does NOT
# contain the literal string "python app.py", so match on app.py as well.
pkill -f "/app.py" 2>/dev/null || true
sleep 2
echo "Remaining app.py processes:"
pgrep -af "app.py" || echo "  none"

# ---------------------------------------------------------------------------
step "2/8  Activate virtualenv and install Python dependencies"
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# ---------------------------------------------------------------------------
step "3/8  Install the Chromium binary (this VPS cannot use APT)"
# Playwright downloads its own Chromium, bypassing the blocked Ubuntu mirrors.
python -m pip install --upgrade playwright
python -m playwright install chromium

# ---------------------------------------------------------------------------
step "4/8  Verify the browser binary is discoverable"
python -c "
import sys
sys.path.insert(0, '${PROJECT_DIR}')
from comment import resolve_browser_path

path = resolve_browser_path()
print()
if not path:
    print('RESULT: FAIL - no Chrome/Chromium binary found')
    sys.exit(1)
print('RESULT: OK')
print('CHROMIUM_PATH=' + path)
"

# ---------------------------------------------------------------------------
step "5/8  Headless driver self-test (start, load a page, quit)"
# Same code path the bot uses. Safe: it opens YouTube and closes again, it
# never logs into Google and never posts a comment.
python -c "
import sys
sys.path.insert(0, '${PROJECT_DIR}')
from comment import create_driver

driver = create_driver()
try:
    print('browserVersion :', driver.capabilities.get('browserVersion'))
    driver.get('https://www.youtube.com')
    print('page title     :', driver.title)
    print('DRIVER TEST: PASS')
finally:
    driver.quit()
"

# ---------------------------------------------------------------------------
step "6/8  Install and start the systemd service"
install -m 644 "${PROJECT_DIR}/deploy/yt-bot.service" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"
sleep 8

# ---------------------------------------------------------------------------
step "7/8  Verify the service and show startup output"
echo "enabled : $(systemctl is-enabled ${SERVICE_NAME} 2>/dev/null || true)"
echo "active  : $(systemctl is-active ${SERVICE_NAME} 2>/dev/null || true)"

echo
echo "--- systemctl status ---"
systemctl status "${SERVICE_NAME}" --no-pager -l || true

echo
echo "--- last 40 lines of ${PROJECT_DIR}/bot.log ---"
tail -n 40 "${PROJECT_DIR}/bot.log" || echo "(bot.log not created yet)"

echo
echo "--- journal (live proof the service is logging) ---"
journalctl -u "${SERVICE_NAME}" -n 25 --no-pager || true

# ---------------------------------------------------------------------------
step "8/8  Graceful restart test (proves systemd stop + auto-start work)"
PID_BEFORE="$(systemctl show "${SERVICE_NAME}" -p MainPID --value)"
echo "PID before restart: ${PID_BEFORE}"
systemctl restart "${SERVICE_NAME}"
sleep 10
PID_AFTER="$(systemctl show "${SERVICE_NAME}" -p MainPID --value)"
echo "PID after restart : ${PID_AFTER}"
echo "restart count     : $(systemctl show "${SERVICE_NAME}" -p NRestarts --value)"

if [ "${PID_BEFORE}" = "${PID_AFTER}" ]; then
  echo "WARNING: PID did not change - the service may not have actually restarted."
else
  echo "RESULT: PASS - systemd stopped the old process and started a new one"
fi

if [ "$(systemctl is-active "${SERVICE_NAME}")" = "active" ]; then
  echo "RESULT: PASS - service is active after restart"
else
  echo "RESULT: FAIL - service is not active"
  systemctl status "${SERVICE_NAME}" --no-pager -l || true
fi

cat <<'EOF'

==============================================================
DEPLOYMENT FINISHED
==============================================================
Useful commands:
  systemctl status  yt-bot          # is it alive?
  systemctl restart yt-bot          # apply a code update
  systemctl stop    yt-bot          # graceful stop
  journalctl -u yt-bot -f           # live logs
  tail -f /root/yt-bot-automation/bot.log
  bash /root/yt-bot-automation/deploy/verify.sh   # health check

Dashboard: http://VPS_IP:5000  (basic auth required)

Reminder: add the Gmail accounts for each business at /settings
before the bot can post anything.
EOF
