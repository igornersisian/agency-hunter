"""
Shared Apify runner — trigger an actor, wait for completion, fetch dataset.

Supports multiple tokens (APIFY_API_TOKEN, APIFY_API_TOKEN2, ...,
APIFY_API_TOKENn) with automatic rotation when a token runs out of credits
or hits a quota / auth error.
"""

import os
import re
import time
import logging

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_TOKENS: list[str] = []
_current_idx: int = 0

# Substrings that indicate the token's account is out of credits / over quota.
# Apify uses several different error types depending on plan/state.
_CREDIT_ERROR_PATTERNS = (
    "monthly-usage-hard-limit",
    "monthly usage hard limit",
    "usage-hard-limit",
    "insufficient credits",
    "not enough usage",
    "out of credit",
    "user-trial-expired",
    "trial-expired",
    "subscription-required",
    "plan-limit",
)


def _load_tokens() -> list[str]:
    """Discover APIFY_API_TOKEN and APIFY_API_TOKEN2..N from env (in order)."""
    global _TOKENS
    if _TOKENS:
        return _TOKENS

    tokens: list[str] = []
    primary = os.environ.get("APIFY_API_TOKEN", "").strip()
    if primary:
        tokens.append(primary)

    numbered: list[tuple[int, str]] = []
    for k, v in os.environ.items():
        m = re.fullmatch(r"APIFY_API_TOKEN(\d+)", k)
        if m:
            v = v.strip()
            if v:
                numbered.append((int(m.group(1)), v))
    for _, v in sorted(numbered):
        if v not in tokens:
            tokens.append(v)

    if not tokens:
        raise RuntimeError(
            "No Apify tokens configured (set APIFY_API_TOKEN or APIFY_API_TOKEN2..N)"
        )

    _TOKENS = tokens
    logger.info(f"Loaded {len(tokens)} Apify token(s) for rotation")
    return _TOKENS


def _token() -> str:
    return _load_tokens()[_current_idx]


def _rotate_token(reason: str) -> bool:
    """Advance to next token. Returns False if no more tokens left."""
    global _current_idx
    tokens = _load_tokens()
    if _current_idx + 1 >= len(tokens):
        logger.error(f"All {len(tokens)} Apify tokens exhausted (reason: {reason})")
        return False
    old_idx = _current_idx
    _current_idx += 1
    logger.warning(
        f"Apify token #{old_idx + 1} unusable ({reason}); "
        f"switching to token #{_current_idx + 1} of {len(tokens)}"
    )
    return True


_TRANSIENT_ERRORS = (httpx.ConnectError, httpx.ReadError, httpx.ReadTimeout,
                     httpx.ConnectTimeout, httpx.RemoteProtocolError)


def _request_with_retry(method: str, url: str, **kwargs) -> httpx.Response:
    """One-shot httpx request with backoff on transient network errors.

    Local DNS intermittently fails (getaddrinfo) — without this, a single
    blip on the actor-start POST kills the whole (otherwise free-to-retry)
    language batch."""
    backoffs = (0, 2, 5, 15)
    for attempt, wait in enumerate(backoffs, 1):
        if wait:
            time.sleep(wait)
        try:
            return httpx.request(method, url, **kwargs)
        except _TRANSIENT_ERRORS as e:
            logger.warning(f"Transient {method} error for {url.split('?')[0]} "
                           f"(attempt {attempt}/{len(backoffs)}): {e}")
            if attempt == len(backoffs):
                raise
    raise AssertionError("unreachable")


def _is_credit_error(resp: httpx.Response) -> bool:
    """True if the response indicates the current token can't be used."""
    if resp.status_code not in (401, 402, 403, 429):
        return False
    body = (resp.text or "").lower()
    if any(p in body for p in _CREDIT_ERROR_PATTERNS):
        return True
    # 401 = bad/expired token; 402/403 from Apify is almost always quota-related.
    if resp.status_code in (401, 402, 403):
        return True
    return False


def run_actor(actor_id: str, actor_input: dict) -> str:
    """Start an Apify actor run and return the run_id.

    Rotates through configured tokens if the current one is out of credits.
    """
    url = f"https://api.apify.com/v2/acts/{actor_id}/runs"

    while True:
        params = {"token": _token()}
        logger.info(
            f"Starting Apify actor {actor_id} (token #{_current_idx + 1})..."
        )
        resp = _request_with_retry("POST", url, json=actor_input, params=params, timeout=30)

        if resp.status_code >= 400:
            if _is_credit_error(resp):
                if _rotate_token(f"actor start HTTP {resp.status_code}"):
                    continue
                raise RuntimeError(
                    f"Apify actor start failed: all tokens exhausted "
                    f"(last status {resp.status_code})"
                )
            logger.error(
                f"Apify actor start failed ({resp.status_code}): {resp.text[:500]}"
            )
            resp.raise_for_status()

        run_id = resp.json()["data"]["id"]
        logger.info(f"Apify run started: {run_id}")
        return run_id


def wait_for_run(run_id: str, timeout_seconds: int = 600) -> str:
    """Poll until the run finishes. Returns dataset_id on SUCCEEDED.

    Raises RuntimeError on FAILED/ABORTED/TIMED-OUT, TimeoutError on local
    deadline expiry.
    """
    url = f"https://api.apify.com/v2/actor-runs/{run_id}"
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        # Retry transient network errors — Apify runs take 10-20 min for
        # big batches, and a single DNS/TCP blip during polling used to
        # kill the whole process and orphan the in-flight (paid) run.
        params = {"token": _token()}
        try:
            resp = httpx.get(url, params=params, timeout=15)
            resp.raise_for_status()
        except (httpx.ConnectError, httpx.ReadError, httpx.ReadTimeout,
                httpx.ConnectTimeout, httpx.RemoteProtocolError) as e:
            logger.warning(f"Transient poll error for {run_id}: {e}. Backing off.")
            for backoff in (5, 15, 30, 60, 120):
                time.sleep(backoff)
                try:
                    resp = httpx.get(url, params={"token": _token()}, timeout=15)
                    resp.raise_for_status()
                    break
                except (httpx.ConnectError, httpx.ReadError, httpx.ReadTimeout,
                        httpx.ConnectTimeout, httpx.RemoteProtocolError) as e2:
                    logger.warning(f"Retry failed ({backoff}s): {e2}")
                    continue
            else:
                raise RuntimeError(
                    f"Apify run {run_id} poll failed after 5 retries. "
                    f"Run may still be executing on Apify — "
                    f"recover with wait_for_run('{run_id}')."
                )
        data = resp.json()["data"]
        status = data["status"]

        if status == "SUCCEEDED":
            dataset_id = data["defaultDatasetId"]
            logger.info(f"Apify run {run_id} succeeded. Dataset: {dataset_id}")
            return dataset_id
        if status in ("FAILED", "ABORTED", "TIMED-OUT"):
            status_msg = (data.get("statusMessage") or "")[:300]
            raise RuntimeError(
                f"Apify actor run {run_id} ended with status: {status} "
                f"({status_msg})"
            )

        logger.info(f"Apify run {run_id} status: {status}. Waiting...")
        time.sleep(10)

    raise TimeoutError(f"Apify actor run {run_id} did not finish within {timeout_seconds}s")


def fetch_dataset(dataset_id: str) -> list[dict]:
    """Download all items from an Apify dataset."""
    url = f"https://api.apify.com/v2/datasets/{dataset_id}/items"

    while True:
        params = {"token": _token(), "format": "json", "clean": "true"}
        logger.info(f"Downloading Apify dataset {dataset_id}...")
        resp = _request_with_retry("GET", url, params=params, timeout=120)

        if resp.status_code >= 400:
            if _is_credit_error(resp):
                if _rotate_token(f"dataset fetch HTTP {resp.status_code}"):
                    continue
                raise RuntimeError(
                    f"Apify dataset fetch failed: all tokens exhausted "
                    f"(last status {resp.status_code})"
                )
            resp.raise_for_status()

        items = resp.json()
        logger.info(f"Downloaded {len(items)} items from dataset {dataset_id}")
        return items


def run_and_collect(actor_id: str, actor_input: dict, timeout_seconds: int = 600) -> list[dict]:
    """Full flow: run actor → wait → fetch dataset.

    If the actor run itself fails (FAILED status) AND the failure looks
    credit-related, rotate to the next token and re-run from scratch.
    Other failures bubble up unchanged.
    """
    while True:
        run_id = run_actor(actor_id, actor_input)
        try:
            dataset_id = wait_for_run(run_id, timeout_seconds=timeout_seconds)
        except RuntimeError as e:
            msg = str(e).lower()
            looks_credit_related = (
                "ended with status: failed" in msg
                and any(p in msg for p in _CREDIT_ERROR_PATTERNS)
            )
            if looks_credit_related and _rotate_token("run failed mid-execution"):
                logger.warning("Re-running actor from scratch on next token")
                continue
            raise
        return fetch_dataset(dataset_id)
