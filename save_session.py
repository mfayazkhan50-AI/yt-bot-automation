#!/usr/bin/env python3
"""Pre-authenticate Google accounts and save reusable browser sessions.

Standalone helper. It imports `comment` ONLY, so it never touches the YouTube
Data API, never calls Groq, and never generates a comment. It does not
import `app`, so no Flask server and no bot loop are started either.

For every account it:
  1. launches undetected-chromedriver with that account's own Chrome profile
  2. runs the normal Google login flow (reusing a saved session when valid)
  3. waits for the flow to finish, including a human 2FA step when headful
  4. writes the profile + cookie jar into sessions/

Usage
-----
    python save_session.py                    # every account in config.json
    python save_session.py --business business1
    python save_session.py --email someone@gmail.com
    python save_session.py --headful          # visible window, for manual 2FA
    python save_session.py --timeout 600      # headful 2FA window, seconds
    python save_session.py --dry-run          # list accounts, launch nothing

Exit codes: 0 all authenticated, 1 at least one failed, 2 setup problem.
"""

import argparse
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Load .env the same anchored + overriding way app.py does, so behaviour here
# matches the running service exactly.
from dotenv import load_dotenv  # noqa: E402

load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)

import comment as bot  # noqa: E402  (browser + login + session helpers only)

DEFAULT_CONFIG = os.path.join(BASE_DIR, "config.json")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return __import__("json").load(handle)
    except FileNotFoundError:
        print(f"[CONFIG] Not found: {path}")
        print(f"[CONFIG] Create it with:  cp {os.path.join(BASE_DIR, 'config.example.json')} {path}")
        return None
    except __import__("json").JSONDecodeError as exc:
        print(f"[CONFIG] {path} is not valid JSON: {exc}")
        return None


def collect_accounts(config, business_filter=None, email_filter=None):
    """Flatten config.json into (key, name, email, password) tuples."""
    businesses = config.get("businesses") or {}
    if not businesses:
        print("[CONFIG] No businesses found.")
        print("[CONFIG] Add accounts in the dashboard at /settings, or edit config.json.")
        return []

    selected = []
    for key, biz in businesses.items():
        name = biz.get("name") or key
        if business_filter and key.lower() != business_filter.lower() \
                and name.lower() != business_filter.lower():
            continue
        for account in biz.get("accounts") or []:
            email = (account.get("email") or "").strip()
            password = account.get("password") or ""
            if not email or not password:
                print(f"[CONFIG] {name}: skipping an account with no email/password")
                continue
            if email_filter and email.lower() != email_filter.lower():
                continue
            selected.append((key, name, email, password, account))
    return selected


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

def authenticate(email, password, headless, account=None):
    """Log one account in and persist its session. Returns True on success."""
    profile_dir, cookie_file = bot.session_paths(email)

    print()
    print("-" * 72)
    print(f"[ACCOUNT] {email}")
    print(f"[ACCOUNT] profile  : {profile_dir}")
    print(f"[ACCOUNT] headless : {headless}")

    # Prefer the account's own proxy, then business/global/env (see resolve_proxy).
    session_id = bot.new_proxy_session_id(email)
    proxy = bot.resolve_proxy(account=account, session_id=session_id)
    if proxy.get("host"):
        print(f"[ACCOUNT] proxy    : {bot._redact_proxy(proxy)} "
              f"(sticky session: {proxy.get('session') or 'none'})")
    else:
        print("[ACCOUNT] proxy    : none (direct)")

    driver = None
    try:
        bot._ensure_session_dir()
        driver = bot.create_driver(profile_dir=profile_dir, proxy=proxy)

        logged_in = bot.login(driver, email, password)

        if not logged_in and not headless:
            # Headful run: the operator may still be finishing a 2FA step in the
            # visible window, so keep the browser open until the session cookies
            # actually appear before declaring failure.
            print("[RESULT] Waiting for you to finish the verification in the window...")
            logged_in = bool(bot.wait_for_session_cookies(driver, bot._manual_2fa_seconds()))

        if not logged_in:
            print(f"[RESULT] Login did not complete for {email}")
            return False

        # login() saves on its success paths; save again to capture the cookies
        # issued by the final YouTube visit.
        bot.save_session(driver, email)

        if not bot.has_session_cookies(driver):
            print(f"[RESULT] Logged in but no SID/SSID session cookie was issued for {email}")
            return False

        print(f"[RESULT] Session successfully saved for {email}")
        print(f"[RESULT] cookies: {cookie_file}")
        return True

    except Exception as exc:
        print(f"[RESULT] ERROR authenticating {email}: {exc}")
        return False
    finally:
        bot.safe_quit(driver)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Pre-authenticate Google accounts and save browser sessions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--business", help="only this business key or name (business1, AIRCS, ...)")
    parser.add_argument("--email", help="only this account")
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    parser.add_argument("--headful", action="store_true",
                        help="show the browser window so 2FA can be completed by hand")
    parser.add_argument("--timeout", type=int,
                        help="headful seconds to wait for a manual 2FA (default 300)")
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be done without launching a browser")
    args = parser.parse_args()

    # HEADLESS_MODE in .env sets the default; --headful overrides it.
    env_headless = os.getenv("HEADLESS_MODE", "1").strip().lower() not in ("0", "false", "no", "off")
    headless = False if args.headful else env_headless
    os.environ["HEADLESS_MODE"] = "1" if headless else "0"
    if args.timeout is not None:
        os.environ["MANUAL_2FA_TIMEOUT"] = str(max(0, args.timeout))

    print("=" * 72)
    print("YT BOT - session saver")
    print("=" * 72)
    print(f"[SCOPE] Browser + login only. No video search, no LLM, no comments.")
    print(f"[SCOPE] openai client imported: {'openai' in sys.modules} (must be False)")
    print(f"[MODE ] headless={headless}"
          + ("  (--headful, complete 2FA by hand)" if not headless else ""))
    if not headless:
        print(f"[MODE ] manual 2FA window: {bot._manual_2fa_seconds()}s")

    # Proxy state matters for 2FA: a data-centre IP is challenged far more often.
    proxy = bot.get_proxy_config()
    if proxy.get("host") and proxy.get("port"):
        print(f"[PROXY] {proxy['host']}:{proxy['port']} "
              f"(auth: {'yes' if proxy.get('user') else 'no'})")
    else:
        print("[PROXY] not configured - traffic comes from this machine's IP")

    browser = bot.resolve_browser_path()
    if not browser:
        print()
        print("[FATAL] No real Chrome/Chromium binary found.")
        print("[FATAL] Playwright's Chromium is not used for login because")
        print("[FATAL] Google flags it with reCAPTCHA. Install real Chrome:")
        for line in bot._install_help():
            print("[FATAL] " + line.replace("[BROWSER] ", ""))
        return 2
    print(f"[BROWSER] {browser}")

    config = load_config(args.config)
    if config is None:
        return 2

    accounts = collect_accounts(config, args.business, args.email)
    if not accounts:
        print()
        if args.business or args.email:
            print("[FATAL] No account matched the filter you passed.")
            print("[FATAL] Run without --business/--email to see everything in the config.")
        else:
            print("[FATAL] No accounts with both an email and a password are configured.")
            print("[FATAL] Add them in the dashboard at /settings, or in config.json")
            print("[FATAL] under businesses.<key>.accounts[].")
        return 2

    print(f"[FOUND ] {len(accounts)} account(s) to authenticate")
    for _key, name, email, _pw, _acct in accounts:
        print(f"         - {name}: {email}")

    if args.dry_run:
        print()
        print("[DRY-RUN] Stopping before the browser launches.")
        return 0

    succeeded, failed = [], []
    for _key, _name, email, password, account in accounts:
        if authenticate(email, password, headless, account=account):
            succeeded.append(email)
        else:
            failed.append(email)

    print()
    print("=" * 72)
    print(f"[SUMMARY] {len(succeeded)}/{len(accounts)} authenticated")
    for email in succeeded:
        print(f"[SUMMARY]   ok   {email}")
    for email in failed:
        print(f"[SUMMARY]   FAIL {email}")
    print(f"[SUMMARY] sessions stored in {bot.session_dir()}")
    print("=" * 72)

    if not succeeded:
        print()
        print("No account could be authenticated. Google usually challenges VPS IPs.")
        print("Try:  python save_session.py --headful   from a machine with a display,")
        print("then copy the sessions/ folder to the VPS. Or configure PROXY_* in .env.")
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
