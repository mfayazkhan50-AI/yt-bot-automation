import logging
import os
import random
import re
import time
from datetime import date

from openai import OpenAI

logger = logging.getLogger("bot")

MAX_RETRIES = 3

# Groq free tier: 200,000 tokens/day. One short comment costs roughly
# 300-400 tokens (prompt + completion) with a non-reasoning model, i.e.
# ~500 comments/day. Reasoning models (openai/gpt-oss-*) burn hundreds of
# extra tokens on hidden reasoning and need a much larger GROQ_MAX_TOKENS,
# so prefer a non-reasoning model for short comments.
DAILY_TOKEN_BUDGET = 200_000


class LLMLimitReached(Exception):
    """The Groq daily token quota is exhausted - stop generating for today.

    Raised instead of crashing or silently falling back to templates, so the
    caller (app.py) can show a clear message and pause the bot until tomorrow.
    """


def _is_rate_limit_error(exc):
    """True for a Groq/OpenAI 429 rate-limit or quota error."""
    if exc.__class__.__name__ == "RateLimitError":
        return True
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429:
        return True
    text = str(exc).lower()
    return "rate limit" in text or "too many requests" in text or "429" in text


def _looks_like_daily_limit(exc):
    """Distinguish the per-day token quota from a transient per-minute limit."""
    text = str(exc).lower()
    if any(marker in text for marker in ("per day", "daily", "tokens per day", "tpd", "quota")):
        return True
    _reset_usage_if_new_day()
    return _usage_today["total"] >= DAILY_TOKEN_BUDGET


def _daily_budget_exhausted():
    _reset_usage_if_new_day()
    return _usage_today["total"] >= DAILY_TOKEN_BUDGET

_usage_today = {"date": None, "prompt": 0, "completion": 0, "total": 0}


def _reset_usage_if_new_day():
    today = date.today().isoformat()
    if _usage_today["date"] != today:
        _usage_today.update(date=today, prompt=0, completion=0, total=0)


def _usage_field(usage, name):
    value = getattr(usage, name, None)
    if value is None and isinstance(usage, dict):
        value = usage.get(name)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _record_usage(usage):
    """Track today's token spend against the Groq daily quota."""
    if usage is None:
        return
    _reset_usage_if_new_day()
    prompt = _usage_field(usage, "prompt_tokens")
    completion = _usage_field(usage, "completion_tokens")
    _usage_today["prompt"] += prompt
    _usage_today["completion"] += completion
    _usage_today["total"] += prompt + completion
    logger.info(
        "[LLM] tokens +%d prompt / +%d completion | today: %d/%d",
        prompt, completion, _usage_today["total"], DAILY_TOKEN_BUDGET,
    )


def _get_client(base_url, api_key):
    return OpenAI(base_url=base_url, api_key=api_key)


def _clean_text(text):
    text = (text or "").strip()
    text = re.sub(r"\s+", " ", text.replace("\n", " ").replace("\r", " "))
    text = re.sub(r"^(comment|reply)\s*:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^[-*•]\s*", "", text)
    text = text.strip().strip("`").strip()
    if len(text) > 1 and text[0] in "\"'“" and text[-1] in "\"'”":
        text = text[1:-1].strip()
    return re.sub(r"\s+", " ", text).strip()


def _truncate_words(text, max_words):
    words = text.split()
    if not max_words or len(words) <= max_words:
        return text
    clipped = " ".join(words[:max_words])
    for stop in (". ", "! ", "? ", "; ", ", "):
        index = clipped.rfind(stop)
        if index >= max(1, int(len(clipped) * 0.4)):
            return clipped[: index + len(stop)].rstrip()
    return clipped.rstrip(" ,.;:-")


def _join_contact(body, required_suffix):
    body = _clean_text(body).rstrip(" ,.;:-")
    # The LLM often keeps the "Contact" prefix from the prompt; drop it
    # so the line is not doubled ("Contact Contact 0312-...").
    body = re.sub(r"\s+contact\s*$", "", body, flags=re.IGNORECASE).rstrip(" ,.;:-")
    if not body:
        return None
    return f"{body} Contact {required_suffix}"


def _build_system_message(business_name, business_info, prompt_rules, forbidden_terms, required_suffix):
    parts = [
        "You write short, natural comments that real people post under YouTube videos.",
        "Hard requirements:",
        "- Reply to what the specific video is actually about.",
        "- One or two sentences, single line, no line breaks.",
        "- Never use markdown, hashtags, links or emoji.",
        "- Never invent facts, statistics, prices or personal experiences.",
        "- Vary sentence structure and wording so no two comments sound alike.",
    ]
    if business_name:
        parts.append(f"- This comment is written on behalf of the business: {business_name}.")
    if business_info:
        profile = [f"{k}: {v}" for k, v in business_info.items() if v]
        if profile:
            parts.append("Business profile (use only when relevant, never dump it):\n" + "\n".join(profile))
    if prompt_rules:
        parts.append("Business specific rules (these override everything else):\n" + prompt_rules.strip())
    if forbidden_terms:
        terms = ", ".join(f'"{t}"' for t in forbidden_terms)
        parts.append(f"These terms must NEVER appear in your comment: {terms}.")
    if required_suffix:
        parts.append(
            "Finish the comment with the contact numbers and nothing after them, like this:\n"
            '"...ready for construction plots are available now. Contact 0312-9090995 / 0311-9559494"\n'
            "The comment must be a complete, grammatical sentence before the contact line."
        )
    return "\n".join(parts)


def _build_user_message(video, prompt_rules, required_suffix, previous_comments):
    title = (video or {}).get("title", "").strip()
    description = (video or {}).get("description", "").strip()
    channel = (video or {}).get("channel", "").strip()
    keyword = (video or {}).get("matched_keyword", "").strip()
    lines = [f'Video title: "{title}"']
    if channel:
        lines.append(f"Channel: {channel}")
    if keyword:
        lines.append(f"Matched search keyword: {keyword}")
    if description:
        lines.append(f'Video description (context): "{description[:500]}"')
    lines.append("")
    lines.append("Write ONE comment for this video.")
    if previous_comments:
        # Keep the dedup context short: every extra comment costs input
        # tokens, and the Groq free tier is capped at 200k tokens/day.
        recent = [c for c in previous_comments[-4:] if c]
        if recent:
            lines.append("Your own previous comments (do NOT repeat their wording or opening):")
            for c in recent:
                lines.append(f"- {c}")
    if required_suffix:
        lines.append(
            f'Finish it with this exact contact line: "Contact {required_suffix}". '
            "Nothing may come after the numbers."
        )
    return "\n".join(line for line in lines if line != "")


def _violates(text, forbidden_terms):
    lowered = text.lower()
    return [term for term in (forbidden_terms or []) if term and term.lower() in lowered]


def _enforce(text, forbidden_terms, required_suffix, max_words):
    text = _clean_text(text)
    if not text:
        return None
    if required_suffix:
        suffix_words = len(required_suffix.split()) + 1
        body = re.sub(re.escape(required_suffix), "", text, flags=re.IGNORECASE)
        body = _truncate_words(_clean_text(body), max(4, (max_words or 25) - suffix_words))
        return _join_contact(body, required_suffix)
    return _truncate_words(text, max_words or 25) or None


def _call_llm(messages, max_tokens):
    """Generate a comment via Groq (OpenAI-compatible API). Sole provider."""
    if _daily_budget_exhausted():
        raise LLMLimitReached(
            f"local daily budget hit: {_usage_today['total']}/{DAILY_TOKEN_BUDGET} tokens today"
        )
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    if not groq_key:
        logger.warning("[LLM] GROQ_API_KEY is empty - cannot call Groq")
        return None
    groq_base = os.getenv("GROQ_API_BASE", "https://api.groq.com/openai/v1").strip()
    groq_model = os.getenv("GROQ_MODEL", "qwen/qwen3.8-27b").strip()
    groq_temp = float(os.getenv("GROQ_TEMPERATURE", "0.8"))
    groq_max = int(os.getenv("GROQ_MAX_TOKENS", str(max_tokens or 100)))
    try:
        client = _get_client(groq_base, groq_key)
        resp = client.chat.completions.create(
            model=groq_model,
            messages=messages,
            temperature=groq_temp,
            top_p=0.95,
            max_tokens=groq_max,
        )
        _record_usage(resp.usage)
        return resp.choices[0].message.content
    except Exception as exc:
        if _is_rate_limit_error(exc):
            if _looks_like_daily_limit(exc):
                raise LLMLimitReached(
                    f"Groq daily token limit reached: {exc}"
                ) from exc
            # Transient per-minute rate limit: back off, then let the caller retry.
            logger.warning(f"[LLM] Transient rate limit, backing off 10s: {exc}")
            time.sleep(10)
            return None
        logger.warning(f"[LLM] Groq failed: {exc}")
        return None


REAL_ESTATE_TEMPLATES = [
    'Great overview of this property! Thanks for sharing the detailed update.',
    'Very informative video regarding real estate trends in this location.',
    'Thanks for the detailed breakdown, helpful insights for buyers.',
    'Appreciate the clear updates on development progress!',
]


def generate_comment(
    prompt=None,
    video=None,
    business_name="",
    business_info=None,
    prompt_rules="",
    forbidden_terms=None,
    required_suffix="",
    previous_comments=None,
    max_words=25,
):
    rules = prompt_rules or prompt or ""
    if video is None and prompt is None:
        return _enforce(random.choice(REAL_ESTATE_TEMPLATES), forbidden_terms, required_suffix, max_words)
    system_message = _build_system_message(
        business_name, business_info, rules, forbidden_terms, required_suffix
    )
    user_message = _build_user_message(video, rules, required_suffix, previous_comments) if video else rules
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            raw = _call_llm([
                {"role": "system", "content": system_message},
                {"role": "user", "content": user_message},
            ], max_tokens=200)
            if not raw or not raw.strip():
                last_error = "no content from LLM"
                if attempt == 1:
                    last_error += (
                        " (if GROQ_MODEL is a reasoning model such as openai/gpt-oss-*, "
                        "raise GROQ_MAX_TOKENS - hidden reasoning eats the whole budget)"
                    )
                logger.warning(f"[GENERATOR] attempt {attempt}/{MAX_RETRIES}: {last_error}")
                time.sleep(1.5 * attempt)
                continue
        except LLMLimitReached:
            # Daily quota exhausted - bubble up so the bot stops gracefully
            # instead of burning retries or posting template comments.
            raise
        except Exception as exc:
            last_error = exc
            logger.warning(f"[GENERATOR] attempt {attempt}/{MAX_RETRIES} failed: {exc}")
            time.sleep(1.5 * attempt)
            continue
        violations = _violates(_clean_text(raw), forbidden_terms)
        if violations and attempt < MAX_RETRIES:
            last_error = f"forbidden terms present: {violations}"
            logger.warning(f"[GENERATOR] attempt {attempt}/{MAX_RETRIES} rejected ({last_error})")
            user_message = (
                user_message
                + "\n\nIMPORTANT: your previous attempt was rejected because it contained "
                + ", ".join(f'"{v}"' for v in violations)
                + ". Rewrite the comment without using those words."
            )
            continue
        final = _enforce(raw, forbidden_terms, required_suffix, max_words)
        if not final:
            last_error = "empty or unusable response"
            logger.warning(f"[GENERATOR] attempt {attempt}/{MAX_RETRIES} rejected ({last_error})")
            continue
        if violations:
            logger.warning(f"[GENERATOR] last resort: stripped forbidden terms {violations}")
        if required_suffix and not final.endswith(required_suffix):
            last_error = "required contact line missing"
            logger.warning(f"[GENERATOR] attempt {attempt}/{MAX_RETRIES} rejected ({last_error})")
            continue
        return final
    logger.error(f"[GENERATOR] Failed to generate a compliant comment. Last error: {last_error}")
    return _enforce(random.choice(REAL_ESTATE_TEMPLATES), forbidden_terms, required_suffix, max_words)


def is_video_relevant(v, t, b=""):
    return True
