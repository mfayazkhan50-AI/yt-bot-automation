import glob
import json
import os
import platform
import random
import re
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
# Browser binary resolution (works on VPS Linux without APT-installed Chrome)
# ---------------------------------------------------------------------------

def _first_existing(paths):
    for path in paths:
        if path and os.path.isfile(path):
            return path
    return None


def _playwright_candidates():
    """Chromium binaries installed by `python -m playwright install chromium`."""
    patterns = []

    custom = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    roots = [custom] if custom else []
    roots.append(os.path.join(os.path.expanduser("~"), ".cache", "ms-playwright"))
    roots.append("/ms-playwright")
    roots.append(os.path.join(os.getenv("LOCALAPPDATA", ""), "ms-playwright") if os.name == "nt" else "")
    roots.append(os.path.join(os.path.expanduser("~"), "Library", "Caches", "ms-playwright"))

    for root in roots:
        if not root:
            continue
        patterns += [
            os.path.join(root, "chromium-*", "chrome-linux", "chrome"),
            os.path.join(root, "chromium-*", "chrome-linux64", "chrome"),
            os.path.join(root, "chromium-*", "chrome-win64", "chrome.exe"),
            os.path.join(root, "chromium-*", "chrome-win", "chrome.exe"),
            os.path.join(root, "chromium-*", "chrome-mac", "Chromium.app", "Contents", "MacOS", "Chromium"),
            os.path.join(root, "chromium_headless_shell-*", "chrome-linux", "headless_shell"),
            os.path.join(root, "chromium_headless_shell-*", "chrome-linux64", "headless_shell"),
        ]

    matches = []
    for pattern in patterns:
        matches.extend(p for p in glob.glob(pattern) if os.path.isfile(p))

    # Prefer full Chromium over headless_shell, then highest version number.
    matches.sort(key=lambda p: ("headless_shell" not in p, _version_key(p)))
    return list(reversed(matches))


def _version_key(path):
    digits = ""
    for part in os.path.normpath(path).split(os.sep):
        if "-" in part and part.split("-")[-1].isdigit():
            digits = part.split("-")[-1]
    return int(digits) if digits else 0


def _system_candidates():
    if platform.system() == "Windows":
        return [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Chromium\Application\chrome.exe",
        ]
    if platform.system() == "Darwin":
        return [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        ]
    return [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/snap/bin/chromium",
    ]


def resolve_browser_path():
    """Return a usable Chrome/Chromium binary path, or None to let uc decide."""
    explicit = os.getenv("CHROMIUM_PATH") or os.getenv("BROWSER_PATH")
    if explicit:
        if os.path.isfile(explicit):
            print(f"[BROWSER] Using CHROMIUM_PATH from env: {explicit}")
            return explicit
        print(f"[BROWSER] WARNING: CHROMIUM_PATH does not exist: {explicit}")

    playwright_path = _first_existing(_playwright_candidates())
    if playwright_path:
        print(f"[BROWSER] Using Playwright Chromium: {playwright_path}")
        return playwright_path

    system_path = _first_existing(_system_candidates())
    if system_path:
        print(f"[BROWSER] Using system browser: {system_path}")
        return system_path

    print("[BROWSER] WARNING: No Chrome/Chromium binary found.")
    print("[BROWSER] On the VPS run: python -m playwright install chromium")
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


def _uc_kwargs(browser_path, major=None):
    """undetected-chromedriver rejects a reused ChromeOptions object, so build fresh."""
    options, proxy = _build_options()
    kwargs = {"use_subprocess": True, "options": options}
    if browser_path:
        kwargs["browser_executable_path"] = browser_path
    if major:
        kwargs["version_main"] = major
    return kwargs, proxy


def create_driver():
    """Start undetected-chromedriver with an explicit browser binary when available."""
    browser_path = resolve_browser_path()

    if not browser_path:
        raise RuntimeError(
            "No Chrome/Chromium binary found (this VPS cannot install Chrome via APT).\n"
            "Fix it with:\n"
            "  python -m pip install playwright\n"
            "  python -m playwright install chromium\n"
            "or point CHROMIUM_PATH in .env at an existing chrome/chromium binary."
        )

    major = detect_browser_major(browser_path)
    kwargs, proxy = _uc_kwargs(browser_path, major)

    try:
        driver = uc.Chrome(**kwargs)
    except Exception as exc:
        reported_major = _major_from_error(str(exc))
        print(f"[BROWSER] undetected-chromedriver failed: {str(exc).splitlines()[0][:160]}")

        if reported_major and reported_major != major:
            print(f"[BROWSER] Retrying undetected-chromedriver pinned to major {reported_major}")
            retry_kwargs, proxy = _uc_kwargs(browser_path, reported_major)
            try:
                driver = uc.Chrome(**retry_kwargs)
            except Exception as retry_exc:
                print(f"[BROWSER] undetected-chromedriver retry failed: {str(retry_exc).splitlines()[0][:160]}")
                print("[BROWSER] Falling back to Selenium + Selenium Manager...")
                options, proxy = _build_options()
                driver = _create_with_selenium(options, browser_path)
        else:
            print("[BROWSER] Falling back to Selenium + Selenium Manager...")
            options, proxy = _build_options()
            driver = _create_with_selenium(options, browser_path)

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


def _create_with_selenium(options, browser_path):
    from selenium import webdriver

    chrome_options = webdriver.ChromeOptions()
    for argument in getattr(options, "arguments", []) or []:
        chrome_options.add_argument(argument)
    if browser_path:
        chrome_options.binary_location = browser_path
    return webdriver.Chrome(options=chrome_options)


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


def login(driver, email, password):
    wait = WebDriverWait(driver, 15)
    driver.set_page_load_timeout(60)

    print("[LOGIN] Navigating to Google login...")
    driver.get(LOGIN_URL)
    time.sleep(4)

    print(f"[LOGIN] Entering email: {email}")
    email_field = wait.until(EC.visibility_of_element_located((By.NAME, "identifier")))
    email_field.clear()
    email_field.send_keys(email)
    time.sleep(1)

    print("[LOGIN] Clicking Next...")
    wait.until(EC.element_to_be_clickable((By.ID, "identifierNext"))).click()
    time.sleep(4)

    print("[LOGIN] Entering password...")
    driver.save_screenshot("before_password.png")
    pass_field = wait.until(EC.visibility_of_element_located((By.NAME, "Passwd")))
    pass_field.clear()
    pass_field.send_keys(password)
    time.sleep(1)

    print("[LOGIN] Clicking Next...")
    wait.until(EC.element_to_be_clickable((By.ID, "passwordNext"))).click()
    time.sleep(5)

    dismiss_google_prompts(driver)

    current_url = driver.current_url
    print(f"[LOGIN] Current URL: {current_url}")

    if "youtube.com" in current_url or "myaccount.google.com" in current_url:
        print(f"[LOGIN] SUCCESS - Logged in as {email}")
    elif "challenge" in current_url.lower():
        print("[LOGIN] SECURITY CHALLENGE - manual intervention needed")
        print("[LOGIN] Waiting 120s to see if it clears on its own...")
        time.sleep(120)
        dismiss_google_prompts(driver)
    else:
        print(f"[LOGIN] WARNING - may not be logged in. URL: {current_url}")

    print("[LOGIN] Visiting YouTube to establish session...")
    try:
        driver.get("https://www.youtube.com")
        time.sleep(5)
        dismiss_google_prompts(driver)
        print(f"[LOGIN] YouTube URL: {driver.current_url}")
    except Exception as exc:
        print(f"[LOGIN] YouTube visit failed: {exc}")

    return True


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
        driver = create_driver()
        driver.set_page_load_timeout(60)
        driver.set_script_timeout(30)
        login(driver, email, password)
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
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    return results
