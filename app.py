import json
import threading
import time
import os
import random
import secrets
import shutil
import logging
import signal
import sys
from datetime import datetime, date
from logging.handlers import RotatingFileHandler
from functools import wraps
from flask import Flask, render_template, request, jsonify, Response
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load .env exactly once, anchored to this file, BEFORE any project module.
#   anchor   -> absolute path, immune to CWD and to a stray parent .env
#   override -> .env wins over stale values inherited from systemd or a shell
# Must stay above the local imports; those read os.environ lazily at call time.
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")
load_dotenv(ENV_PATH, override=True)

from video_finder import find_videos                                 # noqa: E402
from comment_generator import generate_comment, is_video_relevant    # noqa: E402
from comment import process_comments                                 # noqa: E402

app = Flask(__name__)

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CONFIG_EXAMPLE_PATH = os.path.join(BASE_DIR, "config.example.json")
LOG_FILE = os.path.join(BASE_DIR, "bot.log")
STATE_FILE = os.path.join(BASE_DIR, "bot_state.json")
# How long the bot loop may finish its current step before the process exits on SIGTERM.
GRACEFUL_EXIT_SECONDS = 30

log_handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=3, encoding="utf-8")
log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

logging.basicConfig(level=logging.INFO, handlers=[log_handler, stream_handler])
logger = logging.getLogger("bot")


def _validate_startup_env():
    """Surface .env resolution problems at boot instead of at first request.

    A missing or empty DASHBOARD_USER/DASHBOARD_PASS pair makes every request
    return 401, which is indistinguishable from a wrong password. Log it once,
    loudly, at startup. Values are never logged - only set/EMPTY.
    """
    keys = ("DASHBOARD_USER", "DASHBOARD_PASS", "OPENROUTER_API_KEY", "YOUTUBE_API_KEY", "LLM_MODEL")
    if not os.path.isfile(ENV_PATH):
        logger.warning(".env NOT found at %s - relying on the process environment only", ENV_PATH)
    logger.info("Env loaded from %s | %s", ENV_PATH, " ".join(
        f"{k}={'set' if os.environ.get(k, '').strip() else 'EMPTY'}" for k in keys))
    missing = [k for k in ("DASHBOARD_USER", "DASHBOARD_PASS") if not os.environ.get(k, "").strip()]
    if missing:
        logger.error(
            "FATAL: %s empty. Auth compares with no fallback, so every request would 401 "
            "and nobody could log in. Populate %s", ", ".join(missing), ENV_PATH)


_validate_startup_env()

bot_status = {
    "running": False,
    "business": None,
    "progress": {"current": 0, "total": 0, "success": 0, "failed": 0},
    "logs": [],
    "thread": None,
    "last_run": None,
    "today_comments": 0,
    # Per-business daily counters: {"business1": 3, "business2": 0, ...}
    "daily_counts": {},
    "cycle_date": None,
    "current_biz_index": 0,  # 0=business1, 1=business2, 2=business3
}

shutdown_flag = False


def load_state():
    """Restore per-business daily counters so a restart cannot reset the daily limits."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            state = json.load(handle)
        if state.get("date") == date.today().isoformat():
            counts = state.get("counts", {})
            if isinstance(counts, dict):
                return counts
        logger.info(f"State file is from {state.get('date') or 'unknown'}, starting fresh for today")
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning(f"Could not read bot_state.json ({exc}), starting fresh")
    return {}


def save_state(counts):
    """Persist per-business daily counters to disk."""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as handle:
            json.dump({"date": date.today().isoformat(), "counts": counts}, handle, indent=2)
    except OSError as exc:
        logger.warning(f"Could not save bot_state.json: {exc}")


def ensure_new_day():
    """
    At midnight: clear every per-business counter and send the cycle back to business1.
    Returns True when a reset happened.
    """
    today = date.today().isoformat()
    if bot_status["cycle_date"] == today:
        return False

    previous = bot_status["cycle_date"]
    bot_status["daily_counts"] = {}
    bot_status["today_comments"] = 0
    bot_status["current_biz_index"] = 0
    bot_status["cycle_date"] = today
    save_state(bot_status["daily_counts"])

    if previous:
        add_log(f"New day ({today}) - all daily counters reset and cycle restarted at business1.")
    else:
        add_log(f"Daily counters initialised for {today}, cycle starts at business1.")
    return True


def business_count_today(biz_key):
    return int(bot_status["daily_counts"].get(biz_key, 0) or 0)


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = os.environ.get("DASHBOARD_USER", "")
        password = os.environ.get("DASHBOARD_PASS", "")
        auth = request.authorization
        # An unset credential must never authenticate anyone: Flask cannot send
        # an empty username, so comparing against "" would otherwise be an
        # accidental pass/fail depending on client behaviour.
        ok = (auth is not None and bool(user) and bool(password)
              and secrets.compare_digest(auth.username or "", user)
              and secrets.compare_digest(auth.password or "", password))
        if not ok:
            return Response("Unauthorized", 401, {"WWW-Authenticate": 'Basic realm="Login Required"'})
        return f(*args, **kwargs)
    return decorated


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        # config.json is gitignored (it holds Google account passwords), so a
        # fresh clone has none. Seed it from the committed template instead of
        # idling forever with no businesses.
        logger.warning("config.json not found - creating it from config.example.json")
        try:
            shutil.copyfile(CONFIG_EXAMPLE_PATH, CONFIG_PATH)
            logger.warning(f"Created {CONFIG_PATH} from the template. Add your Google accounts at /settings")
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            logger.error(
                "FATAL: neither config.json nor config.example.json exists. "
                "Run: cp config.example.json config.json"
            )
            return {"active_business": "business1", "businesses": {}}
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f"FATAL: could not create config.json from template: {e}")
            return {"active_business": "business1", "businesses": {}}
    except json.JSONDecodeError as e:
        logger.error(f"config.json is corrupted: {e}")
        return {"active_business": "business1", "businesses": {}}


def save_config(config):
    try:
        with open(CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Failed to save config: {e}")
        return False


def add_log(message):
    timestamp = time.strftime("%H:%M:%S")
    entry = f"[{timestamp}] {message}"
    bot_status["logs"].append(entry)
    if len(bot_status["logs"]) > 100:
        bot_status["logs"] = bot_status["logs"][-100:]
    logger.info(message)


def run_bot_cycle(business_key):
    config = load_config()
    biz = config["businesses"][business_key]

    bot_status["running"] = True
    bot_status["business"] = business_key
    bot_status["progress"] = {"current": 0, "total": 0, "success": 0, "failed": 0}

    # Daily limit is tracked per business, not globally.
    daily_limit = biz.get("daily_limit", 30)
    already_posted = business_count_today(business_key)
    bot_status["today_comments"] = already_posted
    remaining = daily_limit - already_posted

    if remaining <= 0:
        add_log(f"Daily limit reached for {biz['name']} ({already_posted}/{daily_limit}). Skipping.")
        return

    accounts = [a for a in biz.get("accounts", []) if a.get("email") and a.get("password")]
    if not accounts:
        add_log("ERROR: No Google accounts configured for this business. Add them in Settings first.")
        return

    add_log(f"Starting bot for: {biz['name']}")
    add_log(f"Daily limit: {already_posted}/{daily_limit} used, {remaining} left today")

    youtube_api_key = os.environ.get("YOUTUBE_API_KEY", "") or config.get("youtube_api_key", "")

    add_log("Finding videos...")
    videos = find_videos(
        api_key=youtube_api_key,
        keywords=biz["keywords"],
        max_results=biz.get("max_videos", 30),
    )

    if not videos:
        add_log("ERROR: No videos found!")
        return

    add_log(f"Found {len(videos)} videos")

    comments_per_account = biz.get("comments_per_account", 15)
    max_to_post = min(comments_per_account * len(accounts), remaining)

    business_info = biz.get("business_info", {}) or {}
    prompt_rules = biz.get("prompt_rules", "")
    forbidden_terms = biz.get("forbidden_terms", []) or []
    required_suffix = biz.get("required_suffix", "")
    max_words = biz.get("max_comment_words", 25)

    if biz.get("relevance_check", True) and biz.get("topic"):
        add_log("Filtering videos for topic relevance...")
        relevant = []
        skipped = 0
        for video in videos:
            if is_video_relevant(video, biz["topic"], biz.get("name", "")):
                relevant.append(video)
            else:
                skipped += 1
                add_log(f"Skipped irrelevant video: {video.get('title','')[:60]}...")
            if shutdown_flag:
                break
        videos = relevant
        add_log(f"Relevance check kept {len(videos)} video(s), skipped {skipped}")
        if not videos:
            add_log("ERROR: every video was filtered out as irrelevant.")
            return

    add_log(f"Generating comments (max {max_to_post} to post today)...")
    video_comments = []
    generated_comments = []
    for i, video in enumerate(videos[:max_to_post]):
        if shutdown_flag:
            add_log("Shutdown requested. Stopping comment generation.")
            break

        comment = generate_comment(
            video=video,
            business_name=biz.get("name", ""),
            business_info=business_info,
            prompt_rules=prompt_rules,
            forbidden_terms=forbidden_terms,
            required_suffix=required_suffix,
            previous_comments=generated_comments,
            max_words=max_words,
        )

        if comment:
            generated_comments.append(comment)
            video_comments.append({"video": video, "comment": comment})
            add_log(f"[{i+1}/{min(len(videos), max_to_post)}] {video.get('title','')[:40]}... -> {comment[:70]}...")
        else:
            add_log(f"[{i+1}/{min(len(videos), max_to_post)}] {video.get('title','')[:40]}... -> skipped (no compliant comment)")
        time.sleep(1)

    if not video_comments:
        add_log("ERROR: No compliant comments generated!")
        return

    total_comments = min(len(video_comments), max_to_post)
    bot_status["progress"]["total"] = total_comments

    add_log(f"Posting {total_comments} comments (today: {bot_status['today_comments']}/{daily_limit})...")

    comment_index = 0
    for account in accounts:
        if comment_index >= total_comments or shutdown_flag:
            break

        account_comments = video_comments[comment_index:comment_index + comments_per_account]
        if not account_comments:
            break

        add_log(f"Account: {account['email']} - {len(account_comments)} comments")

        results = process_comments(
            email=account["email"],
            password=account["password"],
            video_comments=account_comments,
            # Per-comment spacing. Falls back to the generic delay_* keys (and
            # then a safe 150-300s) so a config that only defines delay_*
            # cannot silently collapse to the 10-20s default that triggered
            # YouTube's ghost/velocity filter.
            delay_min_seconds=biz.get(
                "comment_delay_min_seconds",
                biz.get("delay_min_seconds", 150),
            ),
            delay_max_seconds=biz.get(
                "comment_delay_max_seconds",
                biz.get("delay_max_seconds", 300),
            ),
        )

        bot_status["progress"]["current"] += results["success"] + results["failed"]
        bot_status["progress"]["success"] += results["success"]
        bot_status["progress"]["failed"] += results["failed"]

        # Only successful comments count toward this business's daily limit.
        bot_status["daily_counts"][business_key] = business_count_today(business_key) + results["success"]
        bot_status["today_comments"] = bot_status["daily_counts"][business_key]
        save_state(bot_status["daily_counts"])

        add_log(f"Account done: {results['success']} OK, {results['failed']} FAILED")
        add_log(
            f"{biz['name']} today: {bot_status['daily_counts'][business_key]}/{daily_limit}"
        )

        comment_index += len(account_comments)

        if comment_index < total_comments and not shutdown_flag:
            delay = random.uniform(
                biz.get("delay_min_seconds", 60),
                biz.get("delay_max_seconds", 120),
            )
            add_log(f"Switching accounts in {int(delay)}s...")
            _wait_with_shutdown(delay)

    add_log(
        f"Cycle complete. {biz['name']} today: "
        f"{business_count_today(business_key)}/{daily_limit} comments"
    )


def bot_loop():
    global shutdown_flag

    # Restore today's per-business counters, then make sure the cycle starts at business1.
    bot_status["daily_counts"] = load_state()
    ensure_new_day()

    add_log("Bot loop started. Running 24/7.")
    add_log(
        "Daily counters: "
        + (", ".join(f"{k}={v}" for k, v in bot_status["daily_counts"].items()) or "all zero")
    )

    while not shutdown_flag:
        try:
            # Midnight: clear every counter and restart the cycle at business1.
            ensure_new_day()

            config = load_config()
            biz_keys = list(config.get("businesses", {}).keys())

            if not biz_keys:
                add_log("ERROR: No businesses configured. Waiting 5 min...")
                _wait_with_shutdown(300)
                continue

            # Cycle through businesses: 1 -> 2 -> 3 -> 1
            if bot_status["current_biz_index"] >= len(biz_keys):
                bot_status["current_biz_index"] = 0

            biz_key = biz_keys[bot_status["current_biz_index"]]

            if biz_key not in config.get("businesses", {}):
                add_log(f"ERROR: Business '{biz_key}' not found! Waiting 5 min...")
                _wait_with_shutdown(300)
                continue

            # Each business has its own daily limit.
            daily_limit = config["businesses"][biz_key].get("daily_limit", 30)
            if business_count_today(biz_key) >= daily_limit:
                add_log(
                    f"{config['businesses'][biz_key]['name']} daily limit reached "
                    f"({business_count_today(biz_key)}/{daily_limit}). Moving to next business."
                )
                bot_status["current_biz_index"] = (bot_status["current_biz_index"] + 1) % len(biz_keys)
                _wait_with_shutdown(60)
                continue

            run_bot_cycle(biz_key)
            bot_status["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            bot_status["business"] = biz_key

            # Stop cleanly instead of looping through empty cycles once everything is done.
            all_done = all(
                business_count_today(key) >= config["businesses"][key].get("daily_limit", 30)
                for key in biz_keys
                if key in config.get("businesses", {})
            )
            bot_status["current_biz_index"] = (bot_status["current_biz_index"] + 1) % len(biz_keys)

            if all_done:
                add_log("All businesses have reached their daily limit. Sleeping until tomorrow.")
                _wait_with_shutdown(3600)
            else:
                add_log("Waiting 5 minutes before the next business cycle...")
                _wait_with_shutdown(300)

        except Exception as e:
            logger.error(f"Bot loop error: {e}", exc_info=True)
            add_log(f"ERROR: {e}")
            _wait_with_shutdown(60)

    add_log("Bot loop stopped.")
    bot_status["running"] = False


def _wait_with_shutdown(seconds):
    for _ in range(seconds):
        if shutdown_flag:
            break
        time.sleep(1)


def signal_handler(sig, frame):
    """Graceful stop: let the bot finish its current step, then exit the process.

    Without the forced exit Flask would keep serving and systemd would have to
    SIGKILL the process on every stop/restart.
    """
    global shutdown_flag
    try:
        signal_name = signal.Signals(sig).name
    except (ValueError, AttributeError):
        signal_name = str(sig)

    add_log(f"Shutdown signal received ({signal_name}). Stopping bot loop...")
    shutdown_flag = True
    threading.Thread(target=_exit_after_grace, daemon=True).start()


def _exit_after_grace():
    time.sleep(GRACEFUL_EXIT_SECONDS)
    add_log(f"Exiting after {GRACEFUL_EXIT_SECONDS}s grace period.")
    os._exit(0)


signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)


@app.route("/")
@require_auth
def index():
    config = load_config()
    return render_template(
        "index.html",
        businesses=config["businesses"],
        active=config["active_business"],
        status=bot_status,
    )


@app.route("/settings", methods=["GET", "POST"])
@require_auth
def settings():
    config = load_config()

    if request.method == "POST":
        biz_key = request.form.get("business_key", "business1")

        config["youtube_api_key"] = request.form.get("youtube_api_key", config.get("youtube_api_key", ""))

        if biz_key not in config["businesses"]:
            config["businesses"][biz_key] = {
                "name": request.form.get("biz_name", ""),
                "keywords": [],
                "business_info": {},
                "prompt_rules": "",
                "forbidden_terms": [],
                "required_suffix": "",
                "max_comment_words": 25,
                "max_videos": 30,
                "comments_per_account": 15,
                "daily_limit": 30,
                "delay_min_seconds": 300,
                "delay_max_seconds": 600,
                "accounts": [],
            }

        biz = config["businesses"][biz_key]
        biz["name"] = request.form.get("biz_name", biz["name"])
        biz["keywords"] = [k.strip() for k in request.form.get("keywords", "").split(",") if k.strip()]

        if "prompt_rules" in request.form:
            biz["prompt_rules"] = request.form.get("prompt_rules", biz.get("prompt_rules", ""))
        if "required_suffix" in request.form:
            biz["required_suffix"] = request.form.get("required_suffix", "").strip()
        if "forbidden_terms" in request.form:
            biz["forbidden_terms"] = [
                t.strip() for t in request.form.get("forbidden_terms", "").split(",") if t.strip()
            ]

        try:
            biz["max_videos"] = int(request.form.get("max_videos", 30))
            biz["comments_per_account"] = int(request.form.get("comments_per_account", 15))
            biz["delay_min_seconds"] = int(request.form.get("delay_min_seconds", 300))
            biz["delay_max_seconds"] = int(request.form.get("delay_max_seconds", 600))
            biz["daily_limit"] = int(request.form.get("daily_limit", 30))
        except (ValueError, TypeError) as e:
            logger.warning(f"Invalid number in settings: {e}")

        biz["business_info"]["Business Name"] = request.form.get("biz_name", "")
        biz["business_info"]["Business Type"] = request.form.get("biz_type", "")
        biz["business_info"]["Website"] = request.form.get("biz_website", "")
        biz["business_info"]["Phone"] = request.form.get("biz_phone", "")
        biz["business_info"]["Location"] = request.form.get("biz_location", "")
        biz["business_info"]["Services"] = request.form.get("biz_services", "")
        biz["business_info"]["Unique Selling Point"] = request.form.get("biz_usp", "")

        accounts = []
        emails = request.form.getlist("account_email[]")
        passwords = request.form.getlist("account_password[]")
        for email, password in zip(emails, passwords):
            if email.strip():
                accounts.append({"email": email.strip(), "password": password.strip()})
        biz["accounts"] = accounts

        save_config(config)
        return jsonify({"success": True, "message": "Settings saved!"})

    return render_template("settings.html", config=config, llm_model=os.environ.get("LLM_MODEL", "openrouter/auto"))


@app.route("/api/switch_business", methods=["POST"])
@require_auth
def switch_business():
    config = load_config()
    data = request.json
    if not data:
        return jsonify({"success": False, "error": "No data provided"})
    biz_key = data.get("business")
    if biz_key in config["businesses"]:
        config["active_business"] = biz_key
        save_config(config)
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Invalid business"})


@app.route("/api/status")
@require_auth
def get_status():
    config = load_config()
    biz_key = bot_status.get("business") or config.get("active_business", "")
    daily_limit = 30
    if biz_key and biz_key in config.get("businesses", {}):
        daily_limit = config["businesses"][biz_key].get("daily_limit", 30)

    # The displayed counter always belongs to the business currently running.
    today_comments = business_count_today(biz_key) if biz_key else 0
    bot_status["today_comments"] = today_comments

    return jsonify({
        "running": bot_status["running"],
        "business": bot_status["business"],
        "progress": bot_status["progress"],
        "logs": bot_status["logs"][-20:],
        "last_run": bot_status["last_run"],
        "today_comments": today_comments,
        "daily_limit": daily_limit,
        "daily_counts": {
            key: business_count_today(key) for key in config.get("businesses", {})
        },
        "cycle_date": bot_status.get("cycle_date"),
        "current_biz_index": bot_status.get("current_biz_index", 0),
        "total_businesses": len(config.get("businesses", {})),
    })


if __name__ == "__main__":
    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    app.run(host="0.0.0.0", port=5000, debug=False)
