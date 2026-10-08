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
step "1/9  Bootstrap config.json and .env from the committed templates"
# config.json and .env are gitignored because they hold the client's Google
# account passwords and API keys, so a fresh clone does not contain them.
# The bot cannot do anything without them, so create them here from the safe
# templates that ARE committed. Existing files are never overwritten.
for tmpl in config.example.json .env.example; do
  if [ ! -f "${PROJECT_DIR}/${tmpl}" ]; then
    echo "ERROR: ${tmpl} is missing. Pull the latest code first:"
    echo "         cd ${PROJECT_DIR} && git pull"
    exit 1
  fi
done

if [ ! -f "${PROJECT_DIR}/config.json" ]; then
  cp "${PROJECT_DIR}/config.example.json" "${PROJECT_DIR}/config.json"
  echo "Created config.json from config.example.json"
else
  echo "config.json already exists - left untouched"
fi

if [ ! -f "${PROJECT_DIR}/.env" ]; then
  cp "${PROJECT_DIR}/.env.example" "${PROJECT_DIR}/.env"
  chmod 600 "${PROJECT_DIR}/.env"
  echo "Created .env from .env.example (permissions 600)"
else
  echo ".env already exists - left untouched"
fi

echo
echo "Preflight check - required secrets:"
MISSING=""
for var in GROQ_API_KEY YOUTUBE_API_KEY DASHBOARD_USER DASHBOARD_PASS; do
  if [ -z "$(grep -E "^${var}=." "${PROJECT_DIR}/.env" 2>/dev/null)" ]; then
    MISSING="${MISSING} ${var}"
  else
    echo "  ${var}: set"
  fi
done

if [ -n "${MISSING}" ]; then
  cat <<EOF

WARNING: these variables are still empty in .env: ${MISSING}

The service will start, but it CANNOT post comments until they are filled in:
  nano ${PROJECT_DIR}/.env

Gmail accounts are added in the dashboard, not in a file:
  open http://<VPS_IP>:5000/settings and add accounts per business.
EOF
fi
echo

# ---------------------------------------------------------------------------
step "2/9  Stop any previously running instance"
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
step "3/9  Activate virtualenv and install Python dependencies"
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

# ---------------------------------------------------------------------------
step "4/9  Install real Google Chrome"
# Google fingerprints Playwright's Chromium and throws a reCAPTCHA at it on
# every login, so real Chrome is required. This VPS cannot use the normal APT
# mirrors, so the official .deb is fetched straight from dl.google.com.
CHROME_BIN="$(command -v google-chrome || true)"
if [ -z "${CHROME_BIN}" ] && [ -x /opt/google/chrome/chrome ]; then
  CHROME_BIN=/opt/google/chrome/chrome
fi

if [ -n "${CHROME_BIN}" ]; then
  echo "Real Chrome already installed: $(${CHROME_BIN} --version 2>/dev/null || echo unknown)"
else
  case "$(dpkg --print-architecture 2>/dev/null || echo amd64)" in
    arm64|armhf) CHROME_DEB="google-chrome-stable_current_arm64.deb" ;;
    *)           CHROME_DEB="google-chrome-stable_current_amd64.deb" ;;
  esac
  echo "Downloading ${CHROME_DEB} from dl.google.com ..."
  if curl -fsSL -o /tmp/google-chrome.deb \
      "https://dl.google.com/linux/direct/${CHROME_DEB}"; then
    # dpkg does not resolve dependencies; -f install repairs them if the
    # mirrors are reachable. If not, we still try to run and report clearly.
    dpkg -i /tmp/google-chrome.deb || apt-get -f install -y || true
    CHROME_BIN="$(command -v google-chrome || true)"
    [ -z "${CHROME_BIN}" ] && [ -x /opt/google/chrome/chrome ] && CHROME_BIN=/opt/google/chrome/chrome
    if [ -n "${CHROME_BIN}" ]; then
      echo "Installed: $(${CHROME_BIN} --version 2>/dev/null || echo unknown)"
    else
      echo "WARNING: Chrome .deb installed but the binary is not on PATH."
      echo "         Set CHROMIUM_PATH in .env manually."
    fi
  else
    echo "WARNING: could not download Chrome from dl.google.com."
    echo "         Playwright Chromium is NOT used for login (Google flags it)."
    echo "         Install real Chrome, then re-run this script."
  fi
fi

# ---------------------------------------------------------------------------
step "5/9  Verify a real Chrome binary is discoverable"
python -c "
import sys
sys.path.insert(0, '${PROJECT_DIR}')
from comment import resolve_browser_path, _is_playwright_binary

path = resolve_browser_path()
print()
if not path:
    print('RESULT: FAIL - no real Chrome binary found')
    sys.exit(1)
if _is_playwright_binary(path):
    print('RESULT: FAIL - resolved to Playwright Chromium, which Google flags')
    sys.exit(1)
print('RESULT: OK')
print('CHROMIUM_PATH=' + path)
"

# ---------------------------------------------------------------------------
step "6/9  Headless driver self-test (start, load a page, quit)"
# Same code path the bot uses. Safe: it opens YouTube and closes again, it
# never logs into Google and never posts a comment.
python -c "
import sys
sys.path.insert(0, '${PROJECT_DIR}')
from comment import create_driver, safe_quit

driver = create_driver()
try:
    print('browserVersion :', driver.capabilities.get('browserVersion'))
    driver.get('https://www.youtube.com')
    print('page title     :', driver.title)
    print('DRIVER TEST: PASS')
finally:
    safe_quit(driver)
"

# ---------------------------------------------------------------------------
step "7/9  Install and start the systemd service"
install -m 644 "${PROJECT_DIR}/deploy/yt-bot.service" "/etc/systemd/system/${SERVICE_NAME}.service"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"
sleep 8

# ---------------------------------------------------------------------------
step "8/9  Verify the service and show startup output"
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
step "9/9  Graceful restart test (proves systemd stop + auto-start work)"
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
