import logging
import os
import re
import time

from openai import OpenAI

logger = logging.getLogger("bot")

DEFAULT_MODEL = "openrouter/auto"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
MAX_RETRIES = 3

_client = None


def _get_client():
    global _client
    if _client is None:
        api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set in .env")
        _client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=api_key)
    return _client


def _clean_text(text):
    """Flatten a model response into one single-line comment."""
    text = (text or "").strip()
    text = re.sub(r"\s+", " ", text.replace("\n", " ").replace("\r", " "))
    text = re.sub(r"^(comment|reply)\s*:\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^[-*\u2022]\s*", "", text)
    text = text.strip().strip("`").strip()
    if len(text) > 1 and text[0] in "\"'“" and text[-1] in "\"'”":
        text = text[1:-1].strip()
    return re.sub(r"\s+", " ", text).strip()


def _truncate_words(text, max_words):
    words = text.split()
    if not max_words or len(words) <= max_words:
        return text

    clipped = " ".join(words[:max_words])
    # Prefer cutting at a sentence boundary, then at a comma, so the comment stays readable.
    for stop in (". ", "! ", "? ", "; ", ", "):
        index = clipped.rfind(stop)
        if index >= max(1, int(len(clipped) * 0.4)):
            return clipped[: index + len(stop)].rstrip()
    return clipped.rstrip(" ,.;:-")


def _join_contact(body, required_suffix):
    body = _clean_text(body).rstrip(" ,.;:-")
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
        recent = [c for c in previous_comments[-8:] if c]
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
    """Make the model output comply with the hard rules before it is posted."""
    text = _clean_text(text)

    for term in forbidden_terms or []:
        if term and re.search(re.escape(term), text, flags=re.IGNORECASE):
            text = re.sub(re.escape(term), "", text, flags=re.IGNORECASE)

    text = _clean_text(text)
    if not text:
        return None

    if required_suffix:
        suffix_words = len(required_suffix.split()) + 1  # + "Contact"
        body = re.sub(re.escape(required_suffix), "", text, flags=re.IGNORECASE)
        body = _truncate_words(_clean_text(body), max(4, (max_words or 25) - suffix_words))
        return _join_contact(body, required_suffix)

    return _truncate_words(text, max_words or 25) or None


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
    """
    Generate one YouTube comment for a video using OpenRouter.

    `prompt` (plain string) is still supported for backwards compatibility.
    Returns the final comment string, or None if a compliant comment could not be produced.
    """
    rules = prompt_rules or prompt or ""

    if video is None and prompt is None:
        return None

    system_message = _build_system_message(
        business_name, business_info, rules, forbidden_terms, required_suffix
    )
    user_message = _build_user_message(video, rules, required_suffix, previous_comments) if video else rules

    model = os.getenv("LLM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = _get_client().chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": user_message},
                ],
                temperature=1.0,
                top_p=0.95,
                # openrouter/auto often picks a reasoning model, which spends tokens on
                # internal reasoning. Too small a budget returns finish_reason="length"
                # with empty content, so keep this comfortably above the reply size.
                max_tokens=800,
            )
            raw = response.choices[0].message.content
            if not raw or not raw.strip():
                last_error = f"model returned no content (finish_reason={response.choices[0].finish_reason})"
                logger.warning(f"[GENERATOR] attempt {attempt}/{MAX_RETRIES}: {last_error}")
                time.sleep(1.5 * attempt)
                continue
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
    return None


# ---------------------------------------------------------------------------
# Relevance gate (prevents commenting on unrelated videos = spam + bans)
# ---------------------------------------------------------------------------

_STOPWORDS = {
    "and", "or", "the", "for", "with", "from", "that", "this", "about", "video",
    "videos", "youtube", "near", "into", "your", "you", "are", "was", "have",
    "consultant", "consultancy", "services", "service",
}


def _heuristic_relevant(video, topic):
    """Offline fallback used only if the LLM check errors out."""
    text = " ".join(
        [
            (video or {}).get("title", ""),
            (video or {}).get("channel", ""),
            (video or {}).get("description", "")[:300],
        ]
    ).lower()

    keywords = [
        word
        for word in re.findall(r"[a-z0-9]+", (topic or "").lower())
        if len(word) >= 4 and word not in _STOPWORDS
    ]
    if len(keywords) < 2:
        return True
    return any(word in text for word in keywords)


def is_video_relevant(video, topic, business_name=""):
    """
    Decide whether a video genuinely matches the business topic.

    Broad keywords produce junk (a "Hinge" hardware video, a "TopCity" church
    video). Posting on those looks like spam and risks the account, so they are
    filtered out here. Fails open (returns True) if the check itself errors.
    """
    if not topic:
        return True

    title = (video or {}).get("title", "").strip()
    description = (video or {}).get("description", "").strip()[:400]
    channel = (video or {}).get("channel", "").strip()

    user_message = "\n".join(
        [
            f"Business: {business_name}" if business_name else "",
            f"Allowed topic: {topic}",
            f'Video title: "{title}"',
            f"Channel: {channel}",
            f'Description: "{description}"',
            "",
            "Question: Is this video genuinely about the allowed topic, so that a comment from "
            "this business would be relevant and would not look like spam on an unrelated video?",
            "Answer with exactly one word: YES or NO.",
        ]
    )

    model = os.getenv("LLM_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL
    try:
        response = _get_client().chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": "You are a strict content filter. You answer with one word only: YES or NO.",
                },
                {"role": "user", "content": user_message},
            ],
            temperature=0,
            max_tokens=800,
        )
        raw = (response.choices[0].message.content or "").strip().upper()
    except Exception as exc:
        logger.warning(f"[FILTER] Relevance check failed ({exc}); using keyword fallback")
        return _heuristic_relevant(video, topic)

    if not raw:
        return _heuristic_relevant(video, topic)

    if raw.startswith("YES"):
        return True
    if raw.startswith("NO"):
        return False

    return _heuristic_relevant(video, topic)
