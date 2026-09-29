import json
import threading
import time
import os
import random
import logging
import signal
import sys
from datetime import datetime, date
from logging.handlers import RotatingFileHandler
from functools import wraps
from flask import Flask, render_template, request, jsonify, Response
from dotenv import load_dotenv

from video_finder import find_videos
from comment_generator import generate_comment
from comment import process_comments

load_dotenv()

app = Flask(__name__)

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
LOG_FILE = os.path.join(os.path.dirname(__file__), "bot.log")

log_handler = RotatingFileHandler(LOG_FILE, maxBytes=10*1024*1024, backupCount=3, encoding="utf-8")
log_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

stream_handler = logging.StreamHandler()
stream_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))

logging.basicConfig(level=logging.INFO, handlers=[log_handler, stream_handler])
logger = logging.getLogger("bot")

bot_status = {
    "running": False,
    "business": None,
    "progress": {"current": 0, "total": 0, "success": 0, "failed": 0},
    "logs": [],
    "thread": None,
    "last_run": None,
    "today_comments": 0,
    "current_biz_index": 0,  # 0=business1, 1=business2, 2=business3
}

shutdown_flag = False


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        user = os.environ.get("DASHBOARD_USER", "")
        password = os.environ.get("DASHBOARD_PASS", "")
        auth = request.authorization
        if not auth or auth.username != user or auth.password != password:
            return Response("Unauthorized", 401, {"WWW-Authenticate": 'Basic realm="Login Required"'})
        return f(*args, **kwargs)
    return decorated


def load_config():
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.error("config.json not found!")
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

    add_log(f"Starting bot for: {biz['name']}")

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

    add_log("Generating comments...")
    video_comments = []
    for i, video in enumerate(videos):
        if shutdown_flag:
            add_log("Shutdown requested. Stopping comment generation.")
            return
        # Build prompt from video + business info
        prompt = f"Video: {video['title']}\n\nWrite a short comment (1-2 sentences, max 20 words) about this video. Mention the business naturally."
        comment = generate_comment(prompt)
        if comment:
            video_comments.append({"video": video, "comment": comment})
            add_log(f"[{i+1}/{len(videos)}] {video['title'][:40]}... -> {comment[:50]}...")
        time.sleep(1)

    if not video_comments:
        add_log("ERROR: No comments generated!")
        return

    daily_limit = biz.get("daily_limit", 30)
    remaining = daily_limit - bot_status["today_comments"]
    if remaining <= 0:
        add_log(f"Daily limit reached ({daily_limit}). Skipping.")
        return

    comments_per_account = biz.get("comments_per_account", 15)
    total_comments = min(comments_per_account * len(biz["accounts"]), len(video_comments), remaining)
    bot_status["progress"]["total"] = total_comments

    add_log(f"Posting {total_comments} comments (today: {bot_status['today_comments']}/{daily_limit})...")

    comment_index = 0
    for account in biz["accounts"]:
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
        )

        bot_status["progress"]["current"] += results["success"] + results["failed"]
        bot_status["progress"]["success"] += results["success"]
        bot_status["progress"]["failed"] += results["failed"]
        bot_status["today_comments"] += results["success"]

        add_log(f"Account done: {results['success']} OK, {results['failed']} FAILED")

        comment_index += len(account_comments)

        if comment_index < total_comments and not shutdown_flag:
            delay = random.uniform(60, 120)
            add_log(f"Switching accounts in {int(delay)}s...")
            time.sleep(delay)

    add_log(f"Cycle complete. Today: {bot_status['today_comments']}/{daily_limit} comments")


def bot_loop():
    global shutdown_flag
    last_date = date.today()

    add_log("Bot loop started. Running 24/7.")

    while not shutdown_flag:
        try:
            today = date.today()
            if today != last_date:
                bot_status["today_comments"] = 0
                last_date = today
                add_log("New day - daily counter reset.")

            config = load_config()
            biz_keys = list(config.get("businesses", {}).keys())
            
            # Cycle through businesses: 1 -> 2 -> 3 -> 1
            if bot_status["current_biz_index"] >= len(biz_keys):
                bot_status["current_biz_index"] = 0
            
            biz_key = biz_keys[bot_status["current_biz_index"]]

            if biz_key not in config.get("businesses", {}):
                add_log(f"ERROR: Business '{biz_key}' not found! Waiting 5 min...")
                time.sleep(300)
                continue

            daily_limit = config["businesses"][biz_key].get("daily_limit", 30)
            if bot_status["today_comments"] >= daily_limit:
                add_log(f"Daily limit reached ({daily_limit}). Waiting until tomorrow...")
                _wait_with_shutdown(3600)
                # Move to next business even if limit reached
                bot_status["current_biz_index"] = (bot_status["current_biz_index"] + 1) % len(biz_keys)
                continue

            run_bot_cycle(biz_key)
            bot_status["last_run"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            bot_status["business"] = biz_key

            add_log(f"Cycle complete for {biz_key}. Waiting 5 minutes before next cycle...")
            bot_status["current_biz_index"] = (bot_status["current_biz_index"] + 1) % len(biz_keys)
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
    global shutdown_flag
    add_log("Shutdown signal received. Stopping...")
    shutdown_flag = True


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
        config["groq_api_key"] = request.form.get("groq_api_key", config.get("groq_api_key", ""))
        config["groq_model"] = request.form.get("groq_model", config.get("groq_model", "openai/gpt-oss-120b"))

        if biz_key not in config["businesses"]:
            config["businesses"][biz_key] = {
                "name": request.form.get("biz_name", ""),
                "keywords": [],
                "business_info": {},
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

    return render_template("settings.html", config=config)


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

    return jsonify({
        "running": bot_status["running"],
        "business": bot_status["business"],
        "progress": bot_status["progress"],
        "logs": bot_status["logs"][-20:],
        "last_run": bot_status["last_run"],
        "today_comments": bot_status["today_comments"],
        "daily_limit": daily_limit,
        "current_biz_index": bot_status.get("current_biz_index", 0),
        "total_businesses": len(config.get("businesses", {})),
    })


if __name__ == "__main__":
    bot_thread = threading.Thread(target=bot_loop, daemon=True)
    bot_thread.start()
    app.run(host="0.0.0.0", port=5000, debug=False)
