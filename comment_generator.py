import logging
import os
import re
import time

from openai import OpenAI

logger = logging.getLogger("bot")

MAX_RETRIES = 3

FREE_FALLBACK_MODELS = [
    "google/gemini-2.0-flash-exp:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "google/gemma-3-27b-it:free",
    "moonshotai/kimi-k2:free",
    "deepseek/deepseek-chat-v3-0324:free",
    "qwen/qwen2.5-72b-instruct:free",
]


def _get_client(base_url, api_key):
    return OpenAI(base_url=base_url, api_key=api_key)


def _clean_text(text):
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
    # Try CROC (OpenRouter) first
    croc_key = os.getenv("CROC_API_KEY") or os.getenv("OPENROUTER_API_KEY", "").strip()
    croc_base = os.getenv("CROC_API_BASE", "https://openrouter.ai/api/v1").strip()
    croc_model = os.getenv("CROC_MODEL", "openai/gpt-4o-mini").strip()
    croc_temp = float(os.getenv("CROC_TEMPERATURE", "0.85"))
    croc_max = int(os.getenv("CROC_MAX_TOKENS", str(max_tokens or 200)))
    try:
        if croc_key:
            client = _get_client(croc_base, croc_key)
            resp = client.chat.completions.create(
                model=croc_model,
                messages=messages,
                temperature=croc_temp,
                top_p=0.95,
                max_tokens=croc_max,
            )
            return resp.choices[0].message.content
    except Exception as exc:
        logger.warning(f"[LLM] CROC failed: {exc}")

    # Try Groq
    groq_key = os.getenv("GROQ_API_KEY", "").strip()
    groq_base = os.getenv("GROQ_API_BASE", "https://api.groq.com/openai/v1").strip()
    groq_model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()
    groq_temp = float(os.getenv("GROQ_TEMPERATURE", "0.8"))
    groq_max = int(os.getenv("GROQ_MAX_TOKENS", str(max_tokens or 100)))
    try:
        if groq_key:
            client = _get_client(groq_base, groq_key)
            resp = client.chat.completions.create(
                model=groq_model,
                messages=messages,
                temperature=groq_temp,
                top_p=0.95,
                max_tokens=groq_max,
            )
            return resp.choices[0].message.content
    except Exception as exc:
        logger.warning(f"[LLM] Groq failed: {exc}")
    return None


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
        return None
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
