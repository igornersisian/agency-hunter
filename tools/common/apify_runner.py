"""
Shared Apify runner — trigger an actor, wait for completion, fetch dataset.

Lifted from Job-search-automation/tools/run_apify_search.py and generalised
(the sibling hardcodes one actor ID; this version takes it as an argument so
any agency-hunter discovery tool can reuse it).
"""

import os
import time
import logging

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _token() -> str:
    return os.environ["APIFY_API_TOKEN"]


def run_actor(actor_id: str, actor_input: dict) -> str:
    """Start an Apify actor run and return the run_id."""
    url = f"https://api.apify.com/v2/acts/{actor_id}/runs"
    params = {"token": _token()}

    logger.info(f"Starting Apify actor {actor_id}...")
    resp = httpx.post(url, json=actor_input, params=params, timeout=30)
    if resp.status_code >= 400:
        logger.error(f"Apify actor start failed ({resp.status_code}): {resp.text[:500]}")
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
    params = {"token": _token()}
    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        # Retry transient network errors — Apify runs take 10-20 min for
        # big batches, and a single DNS/TCP blip during polling used to
        # kill the whole process and orphan the in-flight (paid) run.
        # We retry up to 5 times with exponential backoff per poll.
        try:
            resp = httpx.get(url, params=params, timeout=15)
            resp.raise_for_status()
        except (httpx.ConnectError, httpx.ReadError, httpx.ReadTimeout,
                httpx.ConnectTimeout, httpx.RemoteProtocolError) as e:
            logger.warning(f"Transient poll error for {run_id}: {e}. Backing off.")
            for backoff in (5, 15, 30, 60, 120):
                time.sleep(backoff)
                try:
                    resp = httpx.get(url, params=params, timeout=15)
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
            raise RuntimeError(f"Apify actor run {run_id} ended with status: {status}")

        logger.info(f"Apify run {run_id} status: {status}. Waiting...")
        time.sleep(10)

    raise TimeoutError(f"Apify actor run {run_id} did not finish within {timeout_seconds}s")


def fetch_dataset(dataset_id: str) -> list[dict]:
    """Download all items from an Apify dataset."""
    url = f"https://api.apify.com/v2/datasets/{dataset_id}/items"
    params = {"token": _token(), "format": "json", "clean": "true"}

    logger.info(f"Downloading Apify dataset {dataset_id}...")
    resp = httpx.get(url, params=params, timeout=120)
    resp.raise_for_status()

    items = resp.json()
    logger.info(f"Downloaded {len(items)} items from dataset {dataset_id}")
    return items


def run_and_collect(actor_id: str, actor_input: dict, timeout_seconds: int = 600) -> list[dict]:
    """Full flow: run actor → wait → fetch dataset."""
    run_id = run_actor(actor_id, actor_input)
    dataset_id = wait_for_run(run_id, timeout_seconds=timeout_seconds)
    return fetch_dataset(dataset_id)
