"""
OpenAI chat wrapper with round-robin load balancing across OpenAI +
OpenRouter, plus 429 retry.

Adapted from Job-search-automation/tools/score_job.py so both projects
behave identically on rate limits.
"""

import itertools
import os
import threading
import time
import logging

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_openai_client: OpenAI | None = None
_openrouter_client: OpenAI | None = None


def get_openai() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
    return _openai_client


def _get_openrouter() -> OpenAI | None:
    global _openrouter_client
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None
    if _openrouter_client is None:
        _openrouter_client = OpenAI(
            api_key=key,
            base_url="https://openrouter.ai/api/v1",
        )
    return _openrouter_client


# --- Round-robin between OpenAI and OpenRouter ---
_provider_cycle: itertools.cycle | None = None
_provider_lock = threading.Lock()


def _next_provider() -> str:
    """Thread-safe round-robin: returns 'openai' or 'openrouter'."""
    global _provider_cycle
    with _provider_lock:
        if _provider_cycle is None:
            providers = ["openai"]
            if os.environ.get("OPENROUTER_API_KEY"):
                providers.append("openrouter")
            _provider_cycle = itertools.cycle(providers)
        return next(_provider_cycle)


def _call_provider(provider: str, messages: list, service_tier: str | None = None, **kwargs):
    """Make a single LLM call to the specified provider.

    `service_tier` is OpenAI-only (e.g. "flex"). OpenRouter ignores it.
    """
    if provider == "openrouter":
        client = _get_openrouter()
        model = kwargs.pop("model", "gpt-4.1-mini")
        kwargs["model"] = f"openai/{model}"
        return client.chat.completions.create(messages=messages, **kwargs)
    else:
        if service_tier:
            kwargs["service_tier"] = service_tier
        return get_openai().chat.completions.create(messages=messages, **kwargs)


def _is_flex_unavailable(err: Exception) -> bool:
    """Detect when OpenAI's flex tier has no capacity right now."""
    s = str(err).lower()
    return "429" in s and (
        "resource_unavailable" in s
        or "resource exhausted" in s
        or "no capacity" in s
        or "service tier" in s
    )


def chat_completion(messages: list, max_retries: int = 3,
                    service_tier: str | None = None, **kwargs):
    """LLM call with round-robin across OpenAI + OpenRouter and 429 retry.

    Pass `model`, `response_format`, `temperature`, etc. via kwargs.
    `service_tier="flex"` uses OpenAI's cheaper Flex tier; on unavailable,
    falls back to the default tier on the same provider without backoff.
    Returns the raw chat.completions.create response.
    """
    provider = _next_provider()
    current_tier = service_tier
    last_error = None

    for attempt in range(max_retries):
        try:
            return _call_provider(provider, messages, service_tier=current_tier, **dict(kwargs))
        except Exception as e:
            if current_tier == "flex" and _is_flex_unavailable(e):
                logger.warning(f"{provider} flex tier unavailable, retrying on default tier")
                current_tier = None
                continue
            if "429" in str(e):
                last_error = e
                wait = min(2 ** attempt, 8)
                logger.warning(f"{provider} rate limited, retry in {wait}s ({attempt+1}/{max_retries})")
                time.sleep(wait)
            else:
                raise

    # If primary provider exhausted retries, try the other one (default tier)
    fallback = "openrouter" if provider == "openai" else "openai"
    if fallback == "openrouter" and not _get_openrouter():
        raise last_error
    logger.info(f"Falling back from {provider} to {fallback}")
    try:
        return _call_provider(fallback, messages, service_tier=None, **dict(kwargs))
    except Exception:
        raise last_error
