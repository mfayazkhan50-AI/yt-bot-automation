import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import NoSuchElementException, TimeoutException
from selenium.webdriver.support import expected_conditions as EC
import time
import random


LOGIN_URL = "https://accounts.google.com/AddSession?continue=https%3A%2F%2Fwww.youtube.com%2Fsignin%3Faction_handle_signin%3Dtrue%26app%3Ddesktop%26hl%3Den-GB%26next%3D%252F&hl=en-GB&passive=false&service=youtube&uilel=0"


def dismiss_google_prompts(driver):
    for _ in range(5):
        dismissed = False

        try:
            btn = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((By.XPATH, "//button[.//span[text()='Not now']]"))
            )
            print(f"[LOGIN] Clicking 'Not now'...")
            btn.click()
            time.sleep(3)
            dismissed = True
        except (TimeoutException, NoSuchElementException):
            pass

        try:
            btn = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((By.XPATH, "//button[.//span[text()='Cancel']]"))
            )
            print(f"[LOGIN] Clicking 'Cancel'...")
            btn.click()
            time.sleep(3)
            dismissed = True
        except (TimeoutException, NoSuchElementException):
            pass

        try:
            btn = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((By.XPATH, "//button[.//span[text()='No thanks']]"))
            )
            print(f"[LOGIN] Clicking 'No thanks'...")
            btn.click()
            time.sleep(3)
            dismissed = True
        except (TimeoutException, NoSuchElementException):
            pass

        try:
            btn = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((By.XPATH, "//button[.//span[text()='Continue']]"))
            )
            print(f"[LOGIN] Clicking 'Continue'...")
            btn.click()
            time.sleep(3)
            dismissed = True
        except (TimeoutException, NoSuchElementException):
            pass

        try:
            btn = WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((By.XPATH, "//button[.//span[text()='Skip']]"))
            )
            print(f"[LOGIN] Clicking 'Skip'...")
            btn.click()
            time.sleep(3)
            dismissed = True
        except (TimeoutException, NoSuchElementException):
            pass

        try:
            btn = WebDriverWait(driver, 2).until(
                EC.element_to_be_clickable((By.ID, "submit_approve_access"))
            )
            print(f"[LOGIN] Clicking approve access...")
            btn.click()
            time.sleep(3)
            dismissed = True
        except (TimeoutException, NoSuchElementException):
            pass

        # Chrome "Continue as [Name]" popup
        try:
            btn = WebDriverWait(driver, 2).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Continue as')]"))
            )
            print(f"[LOGIN] Clicking Chrome 'Continue as...'...")
            btn.click()
            time.sleep(3)
            dismissed = True
        except (TimeoutException, NoSuchElementException):
            pass

        # Chrome "Use Chrome without an account" link
        try:
            btn = WebDriverWait(driver, 2).until(
                EC.element_to_be_clickable((By.XPATH, "//button[contains(text(), 'Use Chrome without')]"))
            )
            print(f"[LOGIN] Clicking 'Use Chrome without account'...")
            btn.click()
            time.sleep(3)
            dismissed = True
        except (TimeoutException, NoSuchElementException):
            pass

        if not dismissed:
            break


def load_config():
    import json
    import os
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config.json")
    try:
        with open(config_path, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"active_business": "business1", "businesses": {}}
    except json.JSONDecodeError:
        return {"active_business": "business1", "businesses": {}}


def get_proxy_config():
    """Get proxy configuration from env vars or config.json."""
    config = load_config()
    proxy_host = os.environ.get("PROXY_HOST", "")
    proxy_port = os.environ.get("PROXY_PORT", "")
    proxy_user = os.environ.get("PROXY_USER", "")
    proxy_pass = os.environ.get("PROXY_PASS", "")

    if not proxy_host or not proxy_port:
        proxy_host = config.get("proxy_host", "")
        proxy_port = config.get("proxy_port", "")
        proxy_user = config.get("proxy_user", "")
        proxy_pass = config.get("proxy_pass", "")

    return proxy_host, proxy_port, proxy_user, proxy_pass


def create_driver():
    options = uc.ChromeOptions()
    options.add_argument("--disable-notifications")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1920,1080")
    # Additional options to prevent Chrome closure on Windows
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--start-maximized")
    options.add_argument("--user-data-dir=C:\\ChromeBotProfile")
    # Proxy configuration (uncomment and configure if needed):
    # options.add_argument(f'--proxy-server=http://{PROXY_HOST}:{PROXY_PORT}')
    # Disable automatic ChromeDriver version detection/update
    driver = uc.Chrome(
        use_subprocess=True,
        options=options,
        browser_executable_path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        version_main=152,  # Pinned version - prevents auto-update download
    )
    return driver


def login(driver, email, password):
    wait = WebDriverWait(driver, 15)
    driver.set_page_load_timeout(60)

    print(f"[LOGIN] Navigating to Google login...")
    driver.get(LOGIN_URL)
    time.sleep(4)

    print(f"[LOGIN] Entering email: {email}")
    email_field = wait.until(EC.visibility_of_element_located((By.NAME, "identifier")))
    email_field.clear()
    email_field.send_keys(email)
    time.sleep(1)

    print(f"[LOGIN] Clicking Next...")
    next_btn = wait.until(EC.element_to_be_clickable((By.ID, "identifierNext")))
    next_btn.click()
    time.sleep(4)

    print(f"[LOGIN] Entering password...")
    driver.save_screenshot("before_password.png")
    pass_field = wait.until(EC.visibility_of_element_located((By.NAME, "Passwd")))
    pass_field.clear()
    pass_field.send_keys(password)
    time.sleep(1)

    print(f"[LOGIN] Clicking Next...")
    next_btn2 = wait.until(EC.element_to_be_clickable((By.ID, "passwordNext")))
    next_btn2.click()
    time.sleep(5)

    dismiss_google_prompts(driver)

    current_url = driver.current_url
    print(f"[LOGIN] Current URL: {current_url}")

    if "youtube.com" in current_url or "myaccount.google.com" in current_url or "accounts.google.com/b" in current_url:
        print(f"[LOGIN] SUCCESS - Logged in as {email}")
    elif "challenges" in current_url or "skotp" in current_url or "challenge" in current_url:
        print(f"[LOGIN] SECURITY CHALLENGE - Manual intervention needed")
        print(f"[LOGIN] Waiting 60s for manual approval...")
        time.sleep(60)
        dismiss_google_prompts(driver)
    else:
        print(f"[LOGIN] WARNING - Might not be logged in. URL: {current_url}")

    print(f"[LOGIN] Visiting YouTube homepage to establish session...")
    try:
        driver.get("https://www.youtube.com")
        time.sleep(5)
        dismiss_google_prompts(driver)
        print(f"[LOGIN] YouTube homepage URL: {driver.current_url}")
    except Exception as e:
        print(f"[LOGIN] YouTube homepage visit failed: {e}")

    return True


def post_comment(driver, video_url, comment_text, timeout=20):
    try:
        print(f"[COMMENTER] Navigating to: {video_url}")
        driver.set_page_load_timeout(60)
        driver.get(video_url)
        time.sleep(random.uniform(5, 8))
        print(f"[COMMENTER] Page loaded. URL: {driver.current_url}")
    except Exception as e:
        print(f"[COMMENTER] Failed to load video page: {e}")
        try:
            print(f"[COMMENTER] Current URL after failure: {driver.current_url}")
        except Exception:
            pass
        return False

    try:
        driver.find_element(
            By.CSS_SELECTOR,
            "#movie_player > div.ytp-chrome-bottom > div.ytp-chrome-controls > div.ytp-left-controls > button",
        ).click()
    except (NoSuchElementException, Exception):
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
        for char in comment_text:
            comment_box.send_keys(char)
            time.sleep(random.uniform(0.05, 0.15))

        time.sleep(random.uniform(1, 2))
        submit_btn = driver.find_element(By.ID, "submit-button")
        driver.execute_script("arguments[0].scrollIntoView(true);", submit_btn)
        time.sleep(1)
        driver.execute_script("arguments[0].click();", submit_btn)
        time.sleep(random.uniform(3, 5))

        print(f"[COMMENTER] Comment posted: {comment_text[:50]}...")
        return True

    except (TimeoutException, NoSuchElementException) as e:
        print(f"[COMMENTER] Failed to post comment: {e}")
        try:
            print(f"[COMMENTER] Current URL: {driver.current_url}")
            driver.save_screenshot("comment_fail.png")
            print(f"[COMMENTER] Screenshot saved: comment_fail.png")
        except Exception:
            pass
        return False


def process_comments(email, password, video_comments):
    driver = None
    results = {"success": 0, "failed": 0}

    try:
        driver = create_driver()
        driver.set_page_load_timeout(60)
        driver.set_script_timeout(30)
        login(driver, email, password)
        time.sleep(3)

        for idx, item in enumerate(video_comments):
            video_url = item["video"]["url"]
            comment_text = item["comment"]

            print(f"\n[COMMENTER] === Comment {idx + 1}/{len(video_comments)} ===")
            print(f"[COMMENTER] Video: {item['video']['title']}")
            print(f"[COMMENTER] Comment: {comment_text[:80]}...")

            success = post_comment(driver, video_url, comment_text)
            if success:
                results["success"] += 1
            else:
                results["failed"] += 1

            delay = random.uniform(10, 20)
            print(f"[COMMENTER] Waiting {int(delay)}s before next comment...")
            time.sleep(delay)

    except Exception as e:
        print(f"[COMMENTER] Error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass

    return results


# Legacy constant (kept for reference)
LOGIN_URL = "https://accounts.google.com/AddSession?continue=https%3A%2F%2Fwww.youtube.com%2Fsignin%3Faction_handle_signin%3Dtrue%26app%3Ddesktop%26hl%3Den-GB%26next%3D%252F&hl=en-GB&passive=false&service=youtube&uilel=0"