import getpass
import hashlib
import json
import os
import platform
import random
import re
import shutil
import subprocess
import time

import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import NoSuchElementException, TimeoutException

LOGIN_URL = "https://accounts.google.com/AddSession?continue=https%3A%2F%2Fwww.youtube.com%2Fsignin%3Faction_handle_signin%3Dtrue%26app%3Ddesktop%26hl%3Den-GB%26next%3D%252F&hl=en-GB&passive=false&service=youtube&uilel=0"

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

# ---------------------------------------------------------------------------
# Session persistence / 2FA handling
# ---------------------------------------------------------------------------

# Cookies that only exist once Google considers the browser signed in.
LOGIN_COOKIE_NAMES = {
    "SID", "SSID", "APISID", "SAPISID",
    "__Secure-1PSID", "__Secure-3PSID", "__Secure-1PSIDTS", "__Secure-3PSIDTS",
}

# The two cookies that actually represent a Google session. manual_login()
# will not declare success until at least one of these is stored.
PRIMARY_SESSION_COOKIES = ("SID", "SSID", "__Secure-1PSID", "__Secure-3PSID")

# URL fragments Google uses for second-factor / abuse interstitials.
CHALLENGE_URL_MARKERS = (
    "accounts.google.com/signin/challenge",
    "/challenge/",
    "verifyoauthaction",
    "accounts.google.com/badstartpage",
    "/signin/rejected",
)

# Page copy that identifies a step-up challenge or bot check.
CHALLENGE_TEXT_MARKERS = (
    "2-step verification",
    "two-step verification",
    "verify it's you",
    "verify it is you",
    "security verification",
    "verify your identity",
    "confirm it's you",
    "unusual traffic",
    "enter the code",
    "check your phone",
    "recaptcha",
    "i'm not a robot",
    "captcha",
)

# Rejected sign-ins. These are credential errors, not 2FA, and must not be
# reported as a challenge or the operator will chase a verification that will
# never succeed.
BAD_CREDENTIAL_URL_MARKERS = ("/signin/rejected",)
BAD_CREDENTIAL_TEXT_MARKERS = (
    "couldn't find your google account",
    "could not find your google account",
    "wrong password",
    "incorrect password",
    "unusual traffic from your computer network",
)


def session_dir():
    """Where per-account browser profiles and cookie jars live."""
    return os.environ.get("SESSION_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "sessions"
    )


def _session_key(email):
    """Stable, filesystem-safe per-account key so accounts never share state."""
    return hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:16]


def session_paths(email):
    """(chrome user-data-dir, cookie jar) for one account."""
    key = _session_key(email)
    base = session_dir()
    return os.path.join(base, f"{key}_profile"), os.path.join(base, f"{key}_cookies.json")


def _ensure_session_dir():
    try:
        os.makedirs(session_dir(), exist_ok=True)
        return True
    except OSError as exc:
        print(f"[SESSION] WARNING: cannot create {session_dir()}: {exc}")
        return False


def save_session(driver, email):
    """Persist cookies so the next run can skip the password form."""
    _, cookie_file = session_paths(email)
    try:
        with open(cookie_file, "w", encoding="utf-8") as handle:
            json.dump(driver.get_cookies(), handle, indent=2)
        print(f"[SESSION] Saved session cookies -> {cookie_file}")
        return True
    except Exception as exc:
        print(f"[SESSION] WARNING: could not save cookies: {exc}")
        return False


def load_session(driver, email):
    """Restore a previously saved cookie jar. Returns True if cookies were applied."""
    _, cookie_file = session_paths(email)
    if not os.path.isfile(cookie_file):
        return False
    try:
        with open(cookie_file, "r", encoding="utf-8") as handle:
            cookies = json.load(handle)
    except Exception as exc:
        print(f"[SESSION] WARNING: could not read {cookie_file}: {exc}")
        return False

    applied = 0
    for cookie in cookies:
        try:
            driver.add_cookie(cookie)
            applied += 1
        except Exception:
            # Stale or domain-scoped cookies are expected to be rejected.
            pass
    print(f"[SESSION] Restored {applied}/{len(cookies)} cookies for {email}")
    return applied > 0


def detect_security_challenge(driver):
    """Return a short reason when Google demands a second factor, else None."""
    try:
        url = (driver.current_url or "").lower()
    except Exception:
        url = ""
    for marker in CHALLENGE_URL_MARKERS:
        if marker in url:
            return f"challenge url ({marker})"

    try:
        page = (driver.page_source or "").lower()
    except Exception:
        page = ""
    for marker in CHALLENGE_TEXT_MARKERS:
        if marker in page:
            return f"challenge text ('{marker}')"
    return None


def detect_bad_credentials(driver):
    """Return a reason when Google rejected the account/password outright."""
    try:
        url = (driver.current_url or "").lower()
    except Exception:
        url = ""
    for marker in BAD_CREDENTIAL_URL_MARKERS:
        if marker in url:
            return f"sign-in rejected by Google ({marker})"

    try:
        page = (driver.page_source or "").lower()
    except Exception:
        page = ""
    for marker in BAD_CREDENTIAL_TEXT_MARKERS:
        if marker in page:
            return f"sign-in rejected by Google ('{marker}')"
    return None


def session_cookie_names(driver):
    try:
        return {c.get("name") for c in driver.get_cookies()}
    except Exception:
        return set()


def has_session_cookies(driver):
    """True only when a real Google SID/SSID-family cookie is present."""
    return bool(session_cookie_names(driver) & set(PRIMARY_SESSION_COOKIES))


def wait_for_session_cookies(driver, timeout):
    """Poll until a real SID/SSID cookie is stored; returns the names found."""
    deadline = time.time() + max(0, timeout)
    while time.time() < deadline:
        names = session_cookie_names(driver) & set(PRIMARY_SESSION_COOKIES)
        if names:
            return sorted(names)
        time.sleep(2)
    return []


def is_logged_in(driver):
    """True when Google session cookies are present, else fall back to a UI probe."""
    if has_session_cookies(driver):
        return True
    try:
        return bool(driver.find_elements(By.CSS_SELECTOR, "#avatar-btn"))
    except Exception:
        return False


def _wait_for_password_or_challenge(driver, timeout):
    """Poll for the password box, returning early if a challenge appears.

    Returns (field, None) on success, (None, reason) for a challenge, and
    (None, None) if the password field never showed up.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        challenge = detect_security_challenge(driver)
        if challenge:
            return None, challenge

        for selector, by in (("Passwd", By.NAME), ("input[type='password']", By.CSS_SELECTOR)):
            try:
                for field in driver.find_elements(by, selector):
                    if field.is_displayed() and field.is_enabled():
                        return field, None
            except Exception:
                pass
        time.sleep(0.5)
    return None, None


def _manual_2fa_seconds():
    try:
        return max(0, int(os.getenv("MANUAL_2FA_TIMEOUT", "300").strip()))
    except (TypeError, ValueError):
        return 300


def _capture_failure(driver, tag):
    """Absolute-path screenshot + URL so failures are diagnosable from the log."""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), f"login_fail_{tag}_{stamp}.png"
    )
    try:
        driver.save_screenshot(path)
        print(f"[LOGIN] Screenshot saved: {path}")
    except Exception:
        pass
    try:
        print(f"[LOGIN] Stuck at URL: {driver.current_url}")
    except Exception:
        pass


def wait_for_manual_completion(driver, timeout, email):
    """Headful only: let a human finish 2FA in the visible browser window."""
    deadline = time.time() + timeout
    print(f"[LOGIN] >>> Complete the verification in the browser window.")
    print(f"[LOGIN] >>> Waiting up to {int(timeout)}s for {email} ...")
    while time.time() < deadline:
        time.sleep(3)
        try:
            url = (driver.current_url or "").lower()
        except Exception:
            continue
        if "youtube.com" in url or "myaccount.google.com" in url:
            print("[LOGIN] Verification cleared (redirected out of the login flow).")
            return True
        if not detect_security_challenge(driver) and "accounts.google.com/signin" not in url:
            print("[LOGIN] Verification cleared.")
            return True
    return False


# ---------------------------------------------------------------------------
# Browser binary resolution
#
# Only a genuine Chrome/Chromium install is used. Google fingerprints
# Playwright's Chromium build and answers it with a reCAPTCHA on essentially
# every sign-in, so that fallback has been removed entirely rather than left
# as an opt-in that someone could trip over in production.
# ---------------------------------------------------------------------------

def _first_existing(paths):
    for path in paths:
        if path and os.path.isfile(path):
            return path
    return None


def _is_playwright_binary(path):
    """True when a path points at Playwright's Chromium, which must not be used."""
    normalised = os.path.normpath(path or "").lower()
    return "ms-playwright" in normalised or "playwright" in normalised


def _install_help():
    if platform.system() == "Windows":
        return [
            "[BROWSER] Install Google Chrome, then re-run:",
            "[BROWSER]   winget install --id Google.Chrome -e",
            "[BROWSER] or download https://www.google.com/chrome/",
        ]
    return [
        "[BROWSER] Install real Chrome (Ubuntu/Debian):",
        "[BROWSER]   curl -fsSL -o /tmp/chrome.deb \\",
        "[BROWSER]     https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb",
        "[BROWSER]   sudo dpkg -i /tmp/chrome.deb && sudo apt-get -f install -y",
        "[BROWSER] That comes from dl.google.com and does not need the APT mirrors.",
    ]


def _system_candidates():
    """Genuine Chrome/Chromium installs, most likely first."""
    system = platform.system()

    if system == "Windows":
        local = os.getenv("LOCALAPPDATA", "")
        program_files = os.getenv("ProgramFiles", r"C:\Program Files")
        program_files_x86 = os.getenv("ProgramFiles(x86)", r"C:\Program Files (x86)")
        return [
            os.path.join(program_files, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(program_files_x86, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(local, "Google", "Chrome", "Application", "chrome.exe") if local else "",
            os.path.join(program_files, "Google", "Chrome Beta", "Application", "chrome.exe"),
            os.path.join(program_files_x86, "Google", "Chrome Beta", "Application", "chrome.exe"),
            os.path.join(program_files, "Chromium", "Application", "chrome.exe"),
            os.path.join(program_files_x86, "Chromium", "Application", "chrome.exe"),
        ]

    if system == "Darwin":
        return [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]

    return [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/opt/google/chrome/chrome",
        "/opt/google/chrome-beta/chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
        "/usr/local/bin/chrome",
    ]


def _on_path_candidates():
    """Genuine chrome/chromium found via PATH (excludes Playwright builds)."""
    found = []
    for name in ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser"):
        hit = shutil.which(name)
        if hit and not _is_playwright_binary(hit):
            found.append(hit)
    return found


def resolve_browser_path():
    """Return a genuine Chrome/Chromium path, or None with install guidance.

    Only a genuine Chrome/Chromium install is ever returned. Playwright's
    Chromium is refused even when named explicitly, because Google fingerprints
    that build and serves it a reCAPTCHA on essentially every login.
    """
    explicit = os.getenv("CHROMIUM_PATH") or os.getenv("BROWSER_PATH")
    if explicit:
        if not os.path.isfile(explicit):
            print(f"[BROWSER] WARNING: CHROMIUM_PATH does not exist: {explicit}")
        elif _is_playwright_binary(explicit):
            print(f"[BROWSER] REFUSING Playwright Chromium set in CHROMIUM_PATH: {explicit}")
            print("[BROWSER] Google flags that build with reCAPTCHA on every login.")
            print("[BROWSER] Point CHROMIUM_PATH at a real Chrome binary instead.")
        else:
            print(f"[BROWSER] Using CHROMIUM_PATH from env: {explicit}")
            return explicit

    system_path = _first_existing(_system_candidates())
    if system_path:
        print(f"[BROWSER] Using system Chrome: {system_path}")
        return system_path

    path_browser = _first_existing(_on_path_candidates())
    if path_browser:
        print(f"[BROWSER] Using Chrome found on PATH: {path_browser}")
        return path_browser

    print("[BROWSER] ERROR: no genuine Chrome/Chromium install was found.")
    for line in _install_help():
        print(line)
    return None


# ---------------------------------------------------------------------------
# Config / proxy
# ---------------------------------------------------------------------------

def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return {"active_business": "business1", "businesses": {}}
    except json.JSONDecodeError:
        return {"active_business": "business1", "businesses": {}}


def get_proxy_config():
    """Proxy settings from env vars, falling back to config.json."""
    config = load_config()
    proxy = {
        "host": os.environ.get("PROXY_HOST", "").strip(),
        "port": os.environ.get("PROXY_PORT", "").strip(),
        "user": os.environ.get("PROXY_USER", "").strip(),
        "password": os.environ.get("PROXY_PASS", "").strip(),
    }

    if not proxy["host"] or not proxy["port"]:
        proxy = {
            "host": str(config.get("proxy_host", "") or "").strip(),
            "port": str(config.get("proxy_port", "") or "").strip(),
            "user": str(config.get("proxy_user", "") or "").strip(),
            "password": str(config.get("proxy_pass", "") or "").strip(),
        }

    return proxy


def detect_browser_major(browser_path):
    """
    Ask the binary for its version so undetected-chromedriver downloads the
    matching driver. Without this, uc picks a driver for a different Chrome
    major and the session dies with "This version of ChromeDriver only supports
    Chrome version X". Returns None when the version cannot be read.
    """
    if not browser_path:
        return None
    try:
        result = subprocess.run(
            [browser_path, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = f"{result.stdout or ''} {result.stderr or ''}"
        match = re.search(r"(\d+)\.(\d+)\.(\d+)", output)
        if match:
            major = int(match.group(1))
            print(f"[BROWSER] Detected browser major version: {major}")
            return major
    except Exception:
        pass
    return None


def _is_headless():
    return os.getenv("HEADLESS_MODE", "1").strip().lower() not in ("0", "false", "no", "off")


def _apply_proxy_auth(driver, proxy):
    """Best-effort proxy authentication via CDP (non fatal if unsupported)."""
    if not proxy.get("user"):
        return
    try:
        import base64

        token = base64.b64encode(
            f"{proxy['user']}:{proxy.get('password', '')}".encode()
        ).decode()
        driver.execute_cdp_cmd(
            "Network.setExtraHTTPHeaders", {"Proxy-Authorization": f"Basic {token}"}
        )
        print("[BROWSER] Proxy authentication applied via CDP")
    except Exception as exc:
        print(f"[BROWSER] WARNING: could not apply proxy auth: {exc}")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def _build_options():
    options = uc.ChromeOptions()

    if _is_headless():
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")

    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-notifications")
    options.add_argument("--disable-extensions")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--lang=en-US")
    options.add_argument("--window-size=1920,1080")

    proxy = get_proxy_config()
    if proxy.get("host") and proxy.get("port"):
        server = proxy["host"]
        if not server.startswith(("http://", "https://", "socks5://")):
            server = f"http://{server}"
        options.add_argument(f"--proxy-server={server}:{proxy['port']}")
        print(f"[BROWSER] Proxy enabled: {server}:{proxy['port']}")

    return options, proxy


def _major_from_error(text):
    """uc reports "Current browser version is 151.0.7922.34" when the driver mismatches."""
    match = re.search(r"Current browser version is\s+(\d+)\.", text or "")
    return int(match.group(1)) if match else None


def _apply_debug_port(options):
    """Expose Chrome DevTools Protocol on localhost for live inspection.

    Chrome >= 136 refuses --remote-debugging-port unless a non-default
    --user-data-dir is also passed, so callers only use this with a profile.
    """
    port = (os.getenv("CHROME_DEBUG_PORT", "") or "").strip()
    if not port:
        return
    if not port.isdigit():
        print(f"[BROWSER] WARNING: CHROME_DEBUG_PORT must be numeric, got {port!r}")
        return
    options.add_argument(f"--remote-debugging-port={port}")
    print(f"[BROWSER] Remote debugging enabled on 127.0.0.1:{port} (CDP)")


def _uc_kwargs(browser_path, major=None, profile_dir=None):
    """undetected-chromedriver rejects a reused ChromeOptions object, so build fresh."""
    options, proxy = _build_options()
    kwargs = {"use_subprocess": True, "options": options}
    if browser_path:
        kwargs["browser_executable_path"] = browser_path
    if major:
        kwargs["version_main"] = major
    if profile_dir:
        # A persistent profile is the main defence against repeated 2FA: Google
        # trusts a device it has already verified instead of challenging again.
        os.makedirs(profile_dir, exist_ok=True)
        kwargs["user_data_directory"] = profile_dir
        _apply_debug_port(options)
    return kwargs, proxy


def create_driver(profile_dir=None):
    """Start undetected-chromedriver with an explicit browser binary when available."""
    browser_path = resolve_browser_path()

    if not browser_path:
        raise RuntimeError(
            "No genuine Chrome/Chromium binary found.\n"
            "Playwright's Chromium is not used for login because Google flags it\n"
            "with reCAPTCHA, so install real Chrome and retry:\n"
            + "\n".join(_install_help())
            + "\nor point CHROMIUM_PATH in .env at an existing real Chrome binary."
        )

    major = detect_browser_major(browser_path)
    kwargs, proxy = _uc_kwargs(browser_path, major, profile_dir)

    try:
        driver = uc.Chrome(**kwargs)
    except Exception as exc:
        reported_major = _major_from_error(str(exc))
        print(f"[BROWSER] undetected-chromedriver failed: {str(exc).splitlines()[0][:160]}")

        if reported_major and reported_major != major:
            print(f"[BROWSER] Retrying undetected-chromedriver pinned to major {reported_major}")
            retry_kwargs, proxy = _uc_kwargs(browser_path, reported_major, profile_dir)
            try:
                driver = uc.Chrome(**retry_kwargs)
            except Exception as retry_exc:
                print(f"[BROWSER] undetected-chromedriver retry failed: {str(retry_exc).splitlines()[0][:160]}")
                print("[BROWSER] Falling back to Selenium + Selenium Manager...")
                options, proxy = _build_options()
                driver = _create_with_selenium(options, browser_path, profile_dir)
        else:
            print("[BROWSER] Falling back to Selenium + Selenium Manager...")
            options, proxy = _build_options()
            driver = _create_with_selenium(options, browser_path, profile_dir)

    try:
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"},
        )
    except Exception:
        pass

    _apply_proxy_auth(driver, proxy)
    print(f"[BROWSER] Driver ready (headless={_is_headless()})")
    return driver


def _create_with_selenium(options, browser_path, profile_dir=None):
    from selenium import webdriver

    chrome_options = webdriver.ChromeOptions()
    for argument in getattr(options, "arguments", []) or []:
        chrome_options.add_argument(argument)
    if profile_dir:
        os.makedirs(profile_dir, exist_ok=True)
        chrome_options.add_argument(f"--user-data-dir={profile_dir}")
        _apply_debug_port(chrome_options)
    if browser_path:
        chrome_options.binary_location = browser_path
    return webdriver.Chrome(options=chrome_options)


def safe_quit(driver):
    """Close a driver without letting Windows teardown errors escape.

    By the time quit() runs, uc's service pipe is often already gone, which
    raises OSError [WinError 6] 'The handle is invalid'; uc then raises the
    same error again from __del__ during garbage collection. Neither means
    the login or comment failed, so both are suppressed.
    """
    if driver is None:
        return
    try:
        driver.quit()
    except OSError as exc:
        print(f"[BROWSER] Teardown OSError ignored (WinError 6 is expected): {exc}")
    except Exception as exc:
        print(f"[BROWSER] Teardown error ignored: {exc}")

    # Stop uc's service subprocess so nothing is left running.
    try:
        service = getattr(driver, "service", None)
        if service is not None:
            service.stop()
    except Exception:
        pass


def _patch_uc_del():
    """Make undetected-chromedriver's __del__ swallow its teardown error.

    uc.Chrome.__del__ calls self.quit() unguarded. On Windows that raises
    OSError [WinError 6] 'The handle is invalid' during garbage collection,
    which Python prints as 'Exception ignored in ...' well after the run has
    already succeeded - so healthy runs looked broken. Guard it at the source;
    this is the only way to stop the noise, since the traceback is emitted by
    the interpreter after our own teardown code has finished.
    """
    chrome_cls = getattr(uc, "Chrome", None)
    if chrome_cls is None or getattr(chrome_cls, "_ytbot_del_patched", False):
        return
    original_del = getattr(chrome_cls, "__del__", None)
    if original_del is None:
        return

    def _safe_del(self):
        try:
            original_del(self)
        except Exception:
            pass

    chrome_cls.__del__ = _safe_del
    chrome_cls._ytbot_del_patched = True


_patch_uc_del()


# ---------------------------------------------------------------------------
# Google prompts / login
# ---------------------------------------------------------------------------

def dismiss_google_prompts(driver):
    for _ in range(5):
        dismissed = False

        for label, xpath in (
            ("Not now", "//button[.//span[text()='Not now']]"),
            ("Cancel", "//button[.//span[text()='Cancel']]"),
            ("No thanks", "//button[.//span[text()='No thanks']]"),
            ("Continue", "//button[.//span[text()='Continue']]"),
            ("Skip", "//button[.//span[text()='Skip']]"),
            ("Continue as", "//button[contains(text(), 'Continue as')]"),
            ("Use Chrome without an account", "//button[contains(text(), 'Use Chrome without')]"),
        ):
            try:
                button = WebDriverWait(driver, 2).until(EC.element_to_be_clickable((By.XPATH, xpath)))
                print(f"[LOGIN] Clicking '{label}'...")
                button.click()
                time.sleep(3)
                dismissed = True
            except (TimeoutException, NoSuchElementException):
                pass
            except Exception:
                pass

        try:
            button = WebDriverWait(driver, 2).until(
                EC.element_to_be_clickable((By.ID, "submit_approve_access"))
            )
            print("[LOGIN] Clicking approve access...")
            button.click()
            time.sleep(3)
            dismissed = True
        except (TimeoutException, NoSuchElementException):
            pass
        except Exception:
            pass

        if not dismissed:
            break


def _handle_challenge(driver, email, manual_timeout):
    """Deal with a 2FA / security check the bot cannot answer on its own.

    Never raises. Returns True only when the account ends up usable.
    """
    print("[LOGIN] SECURITY CHALLENGE - Google requires an extra verification step.")
    _capture_failure(driver, "security_challenge")

    if _is_headless():
        print("[LOGIN] Headless mode: nobody can answer the prompt, so waiting cannot help.")
        print("[LOGIN] Verify this account once from a machine with a display, then copy")
        print("[LOGIN] the sessions/ folder to the VPS so the headless bot reuses it:")
        print("[LOGIN]   python save_session.py --headful")
        print("[LOGIN] If the challenge repeats every run, point the bot at a residential proxy.")
        return False

    if manual_timeout > 0 and wait_for_manual_completion(driver, manual_timeout, email):
        if is_logged_in(driver):
            save_session(driver, email)
            print(f"[LOGIN] SUCCESS - challenge completed manually for {email}")
            return True
        print("[LOGIN] Verification appeared to clear but no session cookie was issued.")
        return False

    print(f"[LOGIN] Manual completion not completed within {manual_timeout}s.")
    return False


def login(driver, email, password):
    """Sign in, handling 2FA challenges gracefully.

    Returns True only when a real Google session exists. Never raises
    TimeoutException to the caller.
    """
    driver.set_page_load_timeout(60)
    wait = WebDriverWait(driver, 15)
    manual_timeout = _manual_2fa_seconds()

    print("[LOGIN] Navigating to Google login...")
    driver.get(LOGIN_URL)
    time.sleep(4)

    # Restore the saved cookie jar only after navigating onto the matching
    # domain: Selenium rejects add_cookie() when the current page's domain does
    # not match, so loading the jar while sitting on about:blank silently
    # dropped every cookie. The persistent Chrome profile normally carries the
    # session already; this also covers a sessions/<key>_cookies.json copied in
    # on its own.
    print("[LOGIN] Restoring saved session (if any)...")
    load_session(driver, email)

    # A restored profile/cookie jar can bypass the whole login form.
    if is_logged_in(driver):
        save_session(driver, email)
        print(f"[LOGIN] SUCCESS - restored existing session for {email}")
        return True

    print(f"[LOGIN] Entering email: {email}")
    try:
        email_field = wait.until(EC.visibility_of_element_located((By.NAME, "identifier")))
    except TimeoutException:
        challenge = detect_security_challenge(driver)
        if challenge:
            print(f"[LOGIN] {challenge} appeared at the first step.")
            return _handle_challenge(driver, email, manual_timeout)
        print("[LOGIN] ERROR - email field never appeared and no challenge was detected.")
        _capture_failure(driver, "no_email_field")
        return False

    try:
        email_field.clear()
        email_field.send_keys(email)
    except Exception as exc:
        print(f"[LOGIN] ERROR - could not type the email: {exc}")
        _capture_failure(driver, "email_entry")
        return False
    time.sleep(1)

    print("[LOGIN] Clicking Next...")
    try:
        wait.until(EC.element_to_be_clickable((By.ID, "identifierNext"))).click()
    except TimeoutException:
        print("[LOGIN] ERROR - 'Next' was not clickable after the email step.")
        _capture_failure(driver, "identifier_next")
        return False
    time.sleep(4)

    # This is where 2FA used to blow up with an uncaught TimeoutException.
    print("[LOGIN] Waiting for the password field (watching for a 2FA challenge)...")
    pass_field, challenge = _wait_for_password_or_challenge(driver, timeout=20)
    if challenge:
        rejected = detect_bad_credentials(driver)
        if rejected:
            print(f"[LOGIN] {rejected}")
            print("[LOGIN] This is a credential problem, not a verification prompt.")
            _capture_failure(driver, "bad_credentials")
            return False
        print(f"[LOGIN] {challenge} appeared before the password prompt.")
        return _handle_challenge(driver, email, manual_timeout)
    if pass_field is None:
        print("[LOGIN] ERROR - timed out waiting for the password field.")
        _capture_failure(driver, "no_password_field")
        return False

    print("[LOGIN] Entering password...")
    try:
        pass_field.clear()
        pass_field.send_keys(password)
    except Exception as exc:
        print(f"[LOGIN] ERROR - could not type the password: {exc}")
        _capture_failure(driver, "password_entry")
        return False
    time.sleep(1)

    print("[LOGIN] Clicking Next...")
    try:
        WebDriverWait(driver, 15).until(EC.element_to_be_clickable((By.ID, "passwordNext"))).click()
    except TimeoutException:
        print("[LOGIN] ERROR - password 'Next' was not clickable.")
        _capture_failure(driver, "password_next")
        return False
    time.sleep(5)

    dismiss_google_prompts(driver)

    # Challenge can also be raised *after* a correct password.
    challenge = detect_security_challenge(driver)
    if challenge:
        rejected = detect_bad_credentials(driver)
        if rejected:
            print(f"[LOGIN] {rejected}")
            print("[LOGIN] Check the email and password in config.json / Settings.")
            _capture_failure(driver, "bad_credentials")
            return False
        print(f"[LOGIN] {challenge} raised after the password was accepted.")
        return _handle_challenge(driver, email, manual_timeout)

    print("[LOGIN] Visiting YouTube to establish the session...")
    try:
        driver.get("https://www.youtube.com")
        time.sleep(5)
        dismiss_google_prompts(driver)
        print(f"[LOGIN] YouTube URL: {driver.current_url}")
    except Exception as exc:
        print(f"[LOGIN] YouTube visit failed: {exc}")

    if not is_logged_in(driver):
        print("[LOGIN] WARNING - login flow finished but no session cookie was issued.")
        _capture_failure(driver, "not_signed_in")
        return False

    save_session(driver, email)
    print(f"[LOGIN] SUCCESS - logged in as {email}")
    return True


def manual_login():
    """Interactive helper: sign in once in a real browser, then save the session.

    Run this on a machine with a display (or a VNC/X session) using
        python save_session.py --headful
    Afterwards copy the sessions/ folder to the VPS; the headless bot reuses it
    and should stop being challenged.

    The browser is deliberately left open until the SID/SSID session cookies
    are actually stored, because closing it during a 2FA step invalidates the
    very session we are trying to capture.
    """
    # Manual login is interactive by definition, so never let a stray
    # HEADLESS_MODE=1 hide the window the operator needs to use.
    os.environ["HEADLESS_MODE"] = "0"

    browser_path = resolve_browser_path()
    if not browser_path:
        print("[MANUAL] Cannot continue without a real Chrome install.")
        return False
    print(f"[MANUAL] Real browser: {browser_path}")
    if _is_playwright_binary(browser_path):
        print("[MANUAL] WARNING: this is Playwright Chromium; Google will likely challenge it.")

    if not _ensure_session_dir():
        return False

    email = input("Google email: ").strip()
    if not email:
        print("[MANUAL] No email given.")
        return False
    password = getpass.getpass("Google password: ")
    profile_dir, cookie_file = session_paths(email)
    print(f"[MANUAL] Profile: {profile_dir}")

    driver = create_driver(profile_dir=profile_dir)
    succeeded = False
    try:
        succeeded = login(driver, email, password)

        if succeeded and not has_session_cookies(driver):
            # login() can clear via a UI probe; for manual capture we insist on
            # the actual session cookies before writing the jar.
            print("[MANUAL] Signed in, but no SID/SSID cookie yet - waiting...")
            wait_for_session_cookies(driver, max(60, _manual_2fa_seconds()))

        if has_session_cookies(driver):
            names = sorted(session_cookie_names(driver) & set(PRIMARY_SESSION_COOKIES))
            print(f"[MANUAL] Google session cookies present: {', '.join(names)}")
            save_session(driver, email)

            # Keep the window open so a slow 2FA step can still finish and any
            # late cookies get captured on the second save below.
            print("[MANUAL] The browser will stay OPEN until you press Enter.")
            try:
                input("[MANUAL] Finish any verification, then press Enter to save and close: ")
            except EOFError:
                pass
            save_session(driver, email)
            print(f"[MANUAL] Session saved to {cookie_file}")
            print("[MANUAL] Copy the whole sessions/ folder to the VPS.")
            succeeded = True
        else:
            print("[MANUAL] Login did not complete - session NOT saved.")
            _capture_failure(driver, "manual_incomplete")
            succeeded = False
    finally:
        safe_quit(driver)

    return succeeded


# ---------------------------------------------------------------------------
# Commenting
# ---------------------------------------------------------------------------

def post_comment(driver, video_url, comment_text, timeout=20):
    try:
        print(f"[COMMENTER] Navigating to: {video_url}")
        driver.set_page_load_timeout(60)
        driver.get(video_url)
        time.sleep(random.uniform(5, 8))
        print(f"[COMMENTER] Page loaded. URL: {driver.current_url}")
    except Exception as exc:
        print(f"[COMMENTER] Failed to load video page: {exc}")
        return False

    try:
        driver.find_element(
            By.CSS_SELECTOR,
            "#movie_player > div.ytp-chrome-bottom > div.ytp-chrome-controls > div.ytp-left-controls > button",
        ).click()
    except Exception:
        pass

    time.sleep(2)
    driver.execute_script("window.scrollTo(0, 600);")
    time.sleep(random.uniform(2, 4))

    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located(
                (By.CSS_SELECTOR, "ytd-comments ytd-comment-simplebox-renderer")
            )
        )

        driver.find_element(
            By.CSS_SELECTOR,
            "ytd-comments ytd-comment-simplebox-renderer div#placeholder-area",
        ).click()
        time.sleep(1)

        comment_box = driver.find_element(By.CSS_SELECTOR, "#contenteditable-root")
        comment_box.clear()
        for char in comment_text:
            comment_box.send_keys(char)
            time.sleep(random.uniform(0.03, 0.09))

        time.sleep(random.uniform(1, 2))
        submit_btn = driver.find_element(By.ID, "submit-button")
        driver.execute_script("arguments[0].scrollIntoView(true);", submit_btn)
        time.sleep(1)
        driver.execute_script("arguments[0].click();", submit_btn)
        time.sleep(random.uniform(3, 5))

        print(f"[COMMENTER] Comment posted: {comment_text[:60]}...")
        return True

    except (TimeoutException, NoSuchElementException) as exc:
        print(f"[COMMENTER] Failed to post comment: {exc}")
        try:
            driver.save_screenshot("comment_fail.png")
            print("[COMMENTER] Screenshot saved: comment_fail.png")
        except Exception:
            pass
        return False


def process_comments(email, password, video_comments, delay_min_seconds=10, delay_max_seconds=20):
    driver = None
    results = {"success": 0, "failed": 0}

    if not video_comments:
        print("[COMMENTER] Nothing to post for this account.")
        return results

    try:
        _ensure_session_dir()
        profile_dir, _ = session_paths(email)
        driver = create_driver(profile_dir=profile_dir)
        driver.set_page_load_timeout(60)
        driver.set_script_timeout(30)

        if not login(driver, email, password):
            # Previously login() returned True even when it failed, so the bot
            # tried to post comments while signed out.
            print("[COMMENTER] Login did not complete - skipping this account.")
            print("[COMMENTER] Check bot.log for the [LOGIN] reason and screenshot above it.")
            results["failed"] += len(video_comments)
            return results

        time.sleep(3)

        for index, item in enumerate(video_comments):
            video_url = item["video"]["url"]
            comment_text = item["comment"]

            print(f"\n[COMMENTER] === Comment {index + 1}/{len(video_comments)} ===")
            print(f"[COMMENTER] Video: {item['video'].get('title', '')}")
            print(f"[COMMENTER] Comment: {comment_text[:90]}")

            if post_comment(driver, video_url, comment_text):
                results["success"] += 1
            else:
                results["failed"] += 1

            delay = random.uniform(delay_min_seconds, delay_max_seconds)
            print(f"[COMMENTER] Waiting {int(delay)}s before next comment...")
            time.sleep(delay)

    except Exception as exc:
        print(f"[COMMENTER] Error: {exc}")
        import traceback

        traceback.print_exc()
    finally:
        safe_quit(driver)

    return results
