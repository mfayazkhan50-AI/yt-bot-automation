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
from selenium.webdriver.common.action_chains import ActionChains
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


def _sanitize_cookie(cookie):
    """Return a cookie dict Chrome will accept, or None if it is unusable.

    driver.get_cookies() emits fields that add_cookie() then rejects - most
    notably sameSite values ("no_restriction" / "unspecified") - and a single
    rejected cookie is silently skipped. Rebuild only the fields Chrome needs
    so one bad cookie cannot abort the whole jar.
    """
    if not isinstance(cookie, dict):
        return None
    name = cookie.get("name")
    value = cookie.get("value")
    if not name or value is None:
        return None

    cleaned = {
        "name": name,
        "value": value,
        "path": cookie.get("path") or "/",
    }
    if cookie.get("domain"):
        cleaned["domain"] = cookie["domain"]
    if cookie.get("secure") is not None:
        cleaned["secure"] = bool(cookie["secure"])
    if cookie.get("httpOnly") is not None:
        cleaned["httpOnly"] = bool(cookie["httpOnly"])
    expiry = cookie.get("expiry")
    if expiry is not None:
        try:
            cleaned["expiry"] = int(float(expiry))
        except (TypeError, ValueError):
            pass
    # sameSite is deliberately dropped - see the docstring above.
    return cleaned


def load_session(driver, email):
    """Inject a saved cookie jar into the CURRENT browser context.

    The driver must already be sitting on the cookie's domain (youtube.com):
    Selenium rejects add_cookie() when the page domain does not match, which is
    why restoring while on accounts.google.com reported "Restored 0/22".
    Returns True if at least one cookie was applied.
    """
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
        cleaned = _sanitize_cookie(cookie)
        if not cleaned:
            continue
        try:
            driver.add_cookie(cleaned)
            applied += 1
        except Exception:
            # Cookies for a different domain (e.g. .google.com while the page
            # is youtube.com) are expected to be rejected here.
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
    """True when a Google session cookie exists, else probe the YouTube UI.

    Cookies alone are not enough right after injecting a jar (the page may
    still be rendering), and the UI alone is not enough either. Check both.
    """
    if has_session_cookies(driver):
        return True
    for selector in ("#avatar-btn", "a#avatar-link", "button[aria-label='Account menu']"):
        try:
            if driver.find_elements(By.CSS_SELECTOR, selector):
                return True
        except Exception:
            pass
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


def restore_session(driver, email):
    """Reuse a saved YouTube session without ever showing the login form.

    Visits youtube.com first so the cookie domain context is valid, checks
    whether the persistent Chrome profile is already signed in, injects the
    saved jar if not, then reloads and re-checks. Returns True when the browser
    ends up signed in, and the caller must then NOT navigate to
    accounts.google.com - a fresh visit to that login form is what Google
    blocks with /signin/rejected on the VPS.
    """
    _, cookie_file = session_paths(email)
    has_jar = os.path.isfile(cookie_file)

    print("[LOGIN] Checking for an existing YouTube session...")
    try:
        driver.get("https://www.youtube.com")
        time.sleep(3)
    except Exception as exc:
        print(f"[LOGIN] Could not reach youtube.com: {exc}")
        return False

    # The persistent Chrome profile may already be signed in.
    if is_logged_in(driver):
        print("[LOGIN] Browser profile is already signed in.")
        return True

    if not has_jar:
        print("[LOGIN] No saved cookie jar for this account.")
        return False

    if not load_session(driver, email):
        print("[LOGIN] No cookies could be restored from the saved jar.")
        return False

    # Reload so the freshly injected cookies are actually sent with the request.
    try:
        driver.refresh()
        time.sleep(4)
    except Exception:
        pass

    # Give YouTube a moment to paint the signed-in top bar after the cookies
    # are injected; the DOM can lag well behind the cookie store in headless.
    if is_logged_in(driver) or _account_ui_present(driver, wait_seconds=10):
        print("[LOGIN] Session restored from saved cookies.")
        return True

    print("[LOGIN] Saved cookies did not establish a session.")
    return False


def login(driver, email, password):
    """Sign in, preferring a restored session and handling 2FA gracefully.

    Returns True only when a real Google session exists. Never raises
    TimeoutException to the caller.
    """
    driver.set_page_load_timeout(60)
    wait = WebDriverWait(driver, 15)
    manual_timeout = _manual_2fa_seconds()

    # 1. Reuse the saved session first. This navigates to youtube.com (a valid
    #    cookie domain) and never touches accounts.google.com, so a working
    #    session bypasses the login form completely.
    if restore_session(driver, email):
        save_session(driver, email)
        print(f"[LOGIN] SUCCESS - restored existing session for {email}")
        return True

    # 2. No usable session, so fall back to a full form login.
    print("[LOGIN] No usable session - starting fresh login...")
    driver.get(LOGIN_URL)
    time.sleep(4)

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

ACCOUNT_UI_SELECTORS = (
    "#avatar-btn",
    "ytd-masthead #avatar-btn",
    "a#avatar-link",
    "ytd-topbar-menu-button-renderer #avatar-btn",
    "button[aria-label='Account menu']",
    "button[aria-label*='Account']",
)

# Elements that only appear when the visitor is signed OUT.
SIGNED_OUT_SELECTORS = (
    "ytd-masthead a[href*='accounts.google.com/ServiceLogin']",
    "ytd-masthead a[href*='accounts.google.com/signin']",
    "ytd-masthead ytd-button-renderer a[href*='signin']",
)


def _account_ui_present(driver, wait_seconds=0):
    """True when a signed-in account avatar/menu is visible in the top bar."""
    deadline = time.time() + max(0, wait_seconds)
    while True:
        for selector in ACCOUNT_UI_SELECTORS:
            try:
                if driver.find_elements(By.CSS_SELECTOR, selector):
                    return True
            except Exception:
                pass
        if time.time() >= deadline:
            return False
        time.sleep(1)


def _signed_out_ui_present(driver):
    """True when the top bar clearly shows the signed-out 'Sign in' control."""
    for selector in SIGNED_OUT_SELECTORS:
        try:
            if driver.find_elements(By.CSS_SELECTOR, selector):
                return True
        except Exception:
            pass
    return False


def session_ok(driver):
    """Fast, tolerant login check: account avatar OR a real session cookie."""
    return _account_ui_present(driver, wait_seconds=1) or has_session_cookies(driver)


def verify_logged_in(driver, timeout=10, reloads=2):
    """Confirm the browser holds a real, usable YouTube login.

    The account avatar is the strongest signal, but the headless masthead can
    paint several seconds after the cookies are in place - or only after a
    reload. A single slow render used to produce a false "Not logged in" that
    aborted an entire account's queue, so this reloads and re-waits, and only
    reports failure when there is no session cookie at all. A live cookie is
    trusted because the post-submit verification still catches comments that do
    not actually go live.
    """
    have_cookies = has_session_cookies(driver)

    if _account_ui_present(driver, wait_seconds=3):
        return True

    if not have_cookies:
        if _signed_out_ui_present(driver):
            print("[LOGIN] YouTube shows the signed-out header and no session cookie exists.")
        return False

    for attempt in range(max(1, reloads)):
        try:
            if attempt == 0:
                driver.get("https://www.youtube.com")
            else:
                driver.refresh()
        except Exception:
            pass
        if _account_ui_present(driver, wait_seconds=timeout):
            return True

    print("[LOGIN] Avatar not detected after reloads, but a Google session cookie is present.")
    return True


def _normalize_comment_text(text):
    """Collapse whitespace so DOM/newline differences do not hide a match."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def _comment_texts_in_dom(driver):
    """Return the text of every rendered comment, excluding the composer."""
    script = """
        const out = [];
        document.querySelectorAll('#content-text').forEach(function (node) {
            if (node.closest('ytd-comment-simplebox-renderer, ytd-commentbox, ytd-comment-composer, #simple-box')) {
                return;
            }
            out.push(node.textContent || '');
        });
        return out;
    """
    try:
        return driver.execute_script(script) or []
    except Exception:
        return []


def _wait_for_comment(driver, comment_text, timeout=20):
    """Poll the comment list (nudging lazy-load) for the exact posted text."""
    target = _normalize_comment_text(comment_text)
    if not target:
        return False
    prefix = target[:40]
    deadline = time.time() + max(1, timeout)
    while time.time() < deadline:
        for text in _comment_texts_in_dom(driver):
            normalized = _normalize_comment_text(text)
            if normalized == target or (prefix and prefix in normalized):
                return True
        try:
            driver.execute_script(
                "const c = document.querySelector('ytd-comments#comments') || "
                "document.querySelector('#comments');"
                "if (c) { c.scrollIntoView(); }"
                "window.scrollBy(0, 500);"
            )
        except Exception:
            pass
        time.sleep(2)
    return False


def _verify_comment_live(driver, comment_text, timeout=20):
    """Reload and confirm the comment survived - optimistic DOM is not enough.

    YouTube briefly renders a submitted comment even when it is silently held
    or dropped, so success is only declared if the text is still present after
    a fresh page load (retried once in case the comment section lazy-loads).
    """
    try:
        driver.refresh()
        time.sleep(random.uniform(3, 5))
    except Exception:
        pass
    if _wait_for_comment(driver, comment_text, timeout=timeout):
        return True
    try:
        driver.refresh()
        time.sleep(random.uniform(3, 5))
    except Exception:
        pass
    return _wait_for_comment(driver, comment_text, timeout=timeout)


def _human_type(element, text):
    """Type character by character with human-like, slightly irregular timing."""
    try:
        element.click()
    except Exception:
        pass
    try:
        element.clear()
    except Exception:
        pass
    next_break = random.randint(15, 25)
    for index, char in enumerate(text):
        element.send_keys(char)
        if char == " ":
            pause = random.uniform(0.06, 0.18)
        elif char in ".,!?":
            pause = random.uniform(0.12, 0.30)
        else:
            pause = random.uniform(0.04, 0.12)
        time.sleep(pause)
        if index >= next_break:
            time.sleep(random.uniform(0.6, 1.5))
            next_break = index + random.randint(15, 25)


def _capture_comment_failure(driver, tag):
    """Absolute-path screenshot so a failed/ghosted comment is diagnosable."""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), f"comment_fail_{tag}_{stamp}.png"
    )
    try:
        driver.save_screenshot(path)
        print(f"[COMMENTER] Screenshot saved: {path}")
    except Exception:
        pass


# A human watches/reads the video before commenting; posting within seconds of
# arrival is a strong bot signal that gets comments silently dropped.
PRE_COMMENT_DWELL_MIN_SECONDS = 20
PRE_COMMENT_DWELL_MAX_SECONDS = 60

# Short "reconsider" pause between finishing the text and pressing submit.
SUBMIT_PAUSE_MIN_SECONDS = 2
SUBMIT_PAUSE_MAX_SECONDS = 5


def _pre_comment_dwell():
    """Randomized 20-60s read/watch pause; override with PRE_COMMENT_DWELL_*."""
    low, high = PRE_COMMENT_DWELL_MIN_SECONDS, PRE_COMMENT_DWELL_MAX_SECONDS
    try:
        low = float(os.environ.get("PRE_COMMENT_DWELL_MIN", low))
        high = float(os.environ.get("PRE_COMMENT_DWELL_MAX", high))
    except (TypeError, ValueError):
        low, high = PRE_COMMENT_DWELL_MIN_SECONDS, PRE_COMMENT_DWELL_MAX_SECONDS
    if high < low:
        low, high = high, low
    if high <= 0:
        return
    pause = random.uniform(low, high)
    print(f"[COMMENTER] Reading/watching for {int(pause)}s before commenting...")
    time.sleep(pause)


# The composer is lazy-loaded and YouTube has changed its DOM over time, so try
# several known markers before giving up.
PLACEHOLDER_SELECTORS = (
    "ytd-comments #placeholder-area",
    "ytd-comment-simplebox-renderer #placeholder-area",
    "ytd-commentbox #placeholder-area",
    "#simple-box #placeholder-area",
    "#simplebox-placeholder",
    "ytd-comments ytd-comment-simplebox-renderer",
    "#placeholder-area",
)

COMMENT_BOX_SELECTORS = (
    "#contenteditable-root",
    "ytd-commentbox #contenteditable-root",
    "div#contenteditable-root[contenteditable='true']",
    "div[contenteditable='true']",
)

SUBMIT_SELECTORS = (
    "#submit-button",
    "ytd-commentbox #submit-button",
    "ytd-button-renderer#submit-button button",
)


def _first_clickable(driver, selectors, timeout=15):
    """Return the first displayed+enabled element matching any selector."""
    deadline = time.time() + max(0, timeout)
    while True:
        for selector in selectors:
            try:
                found = driver.find_elements(By.CSS_SELECTOR, selector)
            except Exception:
                found = []
            for element in found:
                try:
                    if element.is_displayed() and element.is_enabled():
                        return element
                except Exception:
                    continue
        if time.time() >= deadline:
            return None
        time.sleep(0.5)


def _any_present(driver, selectors):
    for selector in selectors:
        try:
            if driver.find_elements(By.CSS_SELECTOR, selector):
                return True
        except Exception:
            pass
    return False


def _scroll_to_comments(driver, timeout=20):
    """Scroll until the comment composer renders (YouTube lazy-loads comments)."""
    deadline = time.time() + max(0, timeout)
    while True:
        if _any_present(driver, PLACEHOLDER_SELECTORS):
            return True
        try:
            driver.execute_script(
                "const c = document.querySelector('ytd-comments#comments') || "
                "document.querySelector('#comments');"
                "if (c) { c.scrollIntoView({block: 'start'}); }"
                "window.scrollBy(0, Math.max(400, window.innerHeight * 0.8));"
            )
        except Exception:
            pass
        if time.time() >= deadline:
            return _any_present(driver, PLACEHOLDER_SELECTORS)
        time.sleep(1)


def post_comment(driver, video_url, comment_text, timeout=20):
    # 1. Never type into the comment box while signed out - that is how a
    #    "posted" log was produced for a comment that never actually existed.
    #    session_ok() is intentionally tolerant (avatar OR session cookie) so a
    #    slow masthead render cannot skip a valid comment.
    if not session_ok(driver):
        print("[ERROR] Not logged in - skipping comment")
        return False

    try:
        print(f"[COMMENTER] Navigating to: {video_url}")
        driver.set_page_load_timeout(60)
        driver.get(video_url)
        time.sleep(random.uniform(4, 7))
        print(f"[COMMENTER] Page loaded. URL: {driver.current_url}")
    except Exception as exc:
        print(f"[COMMENTER] Failed to load video page: {exc}")
        return False

    # Human-like: stop the video, then read down to the comment section.
    try:
        driver.find_element(
            By.CSS_SELECTOR,
            "#movie_player > div.ytp-chrome-bottom > div.ytp-chrome-controls > div.ytp-left-controls > button",
        ).click()
    except Exception:
        pass

    time.sleep(random.uniform(1.5, 3))

    # The composer only exists once the comments section has scrolled into view,
    # so scroll-and-wait instead of a single blind scrollTo.
    if not _scroll_to_comments(driver, timeout=max(15, timeout)):
        print("[COMMENTER] Comment composer did not render after scrolling.")
        _capture_comment_failure(driver, "no_composer")
        return False

    # Watch/read the page for a while before engaging - posting immediately on
    # arrival is what makes the second and later comments look automated.
    _pre_comment_dwell()

    try:
        placeholder = _first_clickable(driver, PLACEHOLDER_SELECTORS, timeout=timeout)
        if placeholder is None:
            print("[COMMENTER] Could not find the comment box placeholder.")
            _capture_comment_failure(driver, "no_placeholder")
            return False

        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", placeholder)
        time.sleep(random.uniform(0.8, 1.8))

        # Hover, pause, then click - as a person would - before typing. Fall back
        # to a native then JS click if the element is covered by an overlay.
        clicked = False
        try:
            ActionChains(driver).move_to_element(placeholder).pause(
                random.uniform(0.3, 0.8)
            ).click().perform()
            clicked = True
        except Exception:
            pass
        if not clicked:
            try:
                placeholder.click()
            except Exception:
                try:
                    driver.execute_script("arguments[0].click();", placeholder)
                except Exception:
                    pass
        time.sleep(random.uniform(1.0, 2.0))

        comment_box = _first_clickable(driver, COMMENT_BOX_SELECTORS, timeout=timeout)
        if comment_box is None:
            print("[COMMENTER] Comment editor did not appear after clicking the box.")
            _capture_comment_failure(driver, "no_editor")
            return False
        _human_type(comment_box, comment_text)
        time.sleep(random.uniform(1.5, 3.0))

        submit_btn = _first_clickable(driver, SUBMIT_SELECTORS, timeout=timeout)
        if submit_btn is None:
            print("[COMMENTER] Submit button was not found.")
            _capture_comment_failure(driver, "no_submit")
            return False
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", submit_btn)
        time.sleep(random.uniform(0.8, 1.5))
        WebDriverWait(driver, timeout).until(
            lambda d: _first_clickable(d, SUBMIT_SELECTORS, timeout=0)
        )
        # A human re-reads before hitting submit rather than firing instantly.
        submit_pause = random.uniform(SUBMIT_PAUSE_MIN_SECONDS, SUBMIT_PAUSE_MAX_SECONDS)
        print(f"[COMMENTER] Pausing {int(submit_pause)}s before submitting...")
        time.sleep(submit_pause)
        try:
            submit_btn.click()
        except Exception:
            driver.execute_script("arguments[0].click();", submit_btn)
        time.sleep(random.uniform(3, 5))

    except (TimeoutException, NoSuchElementException) as exc:
        print(f"[COMMENTER] Failed to post comment: {exc}")
        _capture_comment_failure(driver, "submit")
        return False

    # 2. A success log is only honest if the comment is still there after reload.
    if not _verify_comment_live(driver, comment_text, timeout=timeout):
        print("[WARNING] Ghost comment detected / YouTube dropped comment")
        _capture_comment_failure(driver, "ghost")
        return False

    print(f"[COMMENTER] Comment posted: {comment_text[:60]}...")
    return True


def process_comments(email, password, video_comments, delay_min_seconds=150, delay_max_seconds=300):
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

        # Belt-and-braces session check. verify_logged_in() already retries and
        # trusts a real session cookie, so a failure here is a genuine
        # signed-out state. Repair once, and if that still fails leave the queue
        # intact for the next cycle instead of burning all comments at once.
        if not verify_logged_in(driver):
            print("[COMMENTER] Session not confirmed - attempting a fresh sign-in once...")
            if not login(driver, email, password) or not verify_logged_in(driver):
                print("[ERROR] Not logged in - skipping comment")
                print("[COMMENTER] No usable session; leaving this queue for the next cycle.")
                return results

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
