import math
import random
import re
import time

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

MAX_RESULTS_PER_CALL = 50
QUOTA_ERROR_CODES = {403, 429}


def _sanitize_keyword(keyword):
    """Trim whitespace/newlines and collapse inner spacing from a keyword."""
    if not isinstance(keyword, str):
        return ""
    cleaned = re.sub(r"\s+", " ", keyword.replace("\n", " ").replace("\r", " ").replace("\t", " "))
    return cleaned.strip().strip('"').strip("'").strip()


def sanitize_keywords(keywords):
    """Return a clean, de-duplicated list of keywords preserving original order."""
    if isinstance(keywords, str):
        keywords = keywords.split(",")

    cleaned = []
    seen = set()
    for raw in keywords or []:
        keyword = _sanitize_keyword(raw)
        if not keyword:
            continue
        key = keyword.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(keyword)
    return cleaned


def _describe_api_error(error):
    """Turn a googleapiclient HttpError into a short readable reason."""
    reason = getattr(error, "reason", "") or ""
    status = getattr(getattr(error, "resp", None), "status", None)
    text = str(error).lower()

    if status in QUOTA_ERROR_CODES or "quota" in text or "rate limit" in text or "daily limit" in text:
        return f"quota/rate limit exceeded (status {status})"
    if "apiKeyNotValid" in text or "api key" in text:
        return "invalid or missing API key"
    if "quotaExceeded" in text:
        return f"quota exceeded (status {status})"
    return reason or str(error).split("\n")[0][:200]


def find_videos(api_key, keywords, max_results=30, video_duration="medium", order="date", region_code=None):
    """
    Search YouTube for videos across all keywords.

    Never raises: a failed keyword is logged and the loop continues.
    De-duplicates videos so overlapping keywords never post twice.
    """
    keywords = sanitize_keywords(keywords)
    max_results = max(1, int(max_results or 1))

    if not api_key:
        print("[VIDEO FINDER] ERROR: No YouTube API key configured. Skipping search.")
        return []

    if not keywords:
        print("[VIDEO FINDER] ERROR: No valid keywords configured. Skipping search.")
        return []

    print(f"[VIDEO FINDER] Searching {len(keywords)} keyword(s) for up to {max_results} videos")

    try:
        youtube = build("youtube", "v3", developerKey=api_key)
    except Exception as exc:
        print(f"[VIDEO FINDER] ERROR: Could not build YouTube client: {exc}")
        return []

    per_keyword = max(1, min(MAX_RESULTS_PER_CALL, math.ceil(max_results / len(keywords))))
    seen_ids = set()
    found = []
    failed = 0

    for keyword in keywords:
        try:
            response = (
                youtube.search()
                .list(
                    part="snippet",
                    q=keyword,
                    type="video",
                    videoDuration=video_duration,
                    relevanceLanguage="en",
                    order=order,
                    maxResults=per_keyword,
                    **({"regionCode": region_code} if region_code else {})
                )
                .execute()
            )
        except HttpError as exc:
            failed += 1
            reason = _describe_api_error(exc)
            print(f"[VIDEO FINDER] API error for '{keyword}': {reason}")
            if "quota" in reason.lower() or "rate limit" in reason.lower():
                print("[VIDEO FINDER] Quota exhausted - skipping remaining keywords.")
                break
            continue
        except Exception as exc:
            failed += 1
            print(f"[VIDEO FINDER] Unexpected error for '{keyword}': {exc}")
            continue

        new_for_keyword = 0
        for item in response.get("items", []) or []:
            try:
                video_id = item["id"]["videoId"]
            except (KeyError, TypeError):
                continue

            if video_id in seen_ids:
                continue
            seen_ids.add(video_id)

            snippet = item.get("snippet", {}) or {}
            found.append(
                {
                    "id": video_id,
                    "title": _sanitize_keyword(snippet.get("title", "")),
                    "description": _sanitize_keyword(snippet.get("description", "")),
                    "channel": _sanitize_keyword(snippet.get("channelTitle", "")),
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "matched_keyword": keyword,
                    "published_at": snippet.get("publishedAt", ""),
                }
            )
            new_for_keyword += 1

        print(f"[VIDEO FINDER] '{keyword}' -> {new_for_keyword} new video(s)")

        if len(found) >= max_results * 2:
            break

        time.sleep(0.3)

    if failed:
        print(f"[VIDEO FINDER] {failed} keyword search(es) failed and were skipped")

    if not found:
        print("[VIDEO FINDER] No videos found.")
        return []

    random.shuffle(found)
    selected = found[:max_results]
    print(f"[VIDEO FINDER] Found {len(selected)} unique videos (from {len(found)} total)")
    return selected
