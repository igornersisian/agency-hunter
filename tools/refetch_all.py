"""
Re-fetch website text for all agencies using Playwright (local Chromium,
free, JS-rendered), save raw_website_text, then re-classify.

This fixes the original pipeline flaw where classification was based on
a tiny LLM-compressed JSON instead of the actual website content.

Usage:
    python refetch_all.py                    # refetch + reclassify all
    python refetch_all.py --limit 50         # process first 50
    python refetch_all.py --reclassify-only  # skip fetch, just reclassify
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

sys.path.insert(0, ".")

from common.supabase_client import get_supabase
from common.http_fetch import _html_to_markdown, _truncate, MAX_CHARS
from common.llm import chat_completion
from common.profile import get_profile, get_agency_config

logger = logging.getLogger(__name__)

# Paths to try for each agency site
_PATHS = ["/", "/about", "/about-us", "/services", "/what-we-do",
          "/work", "/case-studies", "/projects", "/team", "/contact"]
MAX_PAGES_PER_SITE = 4
MAX_COMBINED_CHARS = 30_000
CONCURRENT_PAGES = 8  # max simultaneous browser tabs


def _build_urls_for_agency(website_url: str) -> list[str]:
    """Build candidate URLs from a base website URL."""
    parsed = urlparse(website_url if "://" in website_url else f"https://{website_url}")
    root = f"{parsed.scheme or 'https'}://{parsed.netloc or parsed.path}"
    return [urljoin(root + "/", path.lstrip("/")) for path in _PATHS]


async def _fetch_all_pages(agencies: list[dict], timeout: int = 20) -> dict[str, str]:
    """Fetch pages for all agencies using a single Playwright browser.

    Returns {agency_id: combined_markdown_text}.
    """
    from playwright.async_api import async_playwright

    # Build URL -> agency_id mapping
    url_to_agency: dict[str, str] = {}
    agency_urls: dict[str, list[str]] = {}
    for agency in agencies:
        aid = agency["id"]
        urls = _build_urls_for_agency(agency["website_url"])
        agency_urls[aid] = urls
        for u in urls:
            url_to_agency[u] = aid

    all_urls = list(url_to_agency.keys())
    logger.info(f"Fetching {len(all_urls)} URLs for {len(agencies)} agencies")

    # Fetch all URLs with Playwright
    url_results: dict[str, str] = {}
    sem = asyncio.Semaphore(CONCURRENT_PAGES)

    async def _fetch_page(browser, url: str):
        async with sem:
            page = await browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0 Safari/537.36"
                ),
                ignore_https_errors=True,
            )
            try:
                await page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                html = await page.content()
                md = _html_to_markdown(html)
                if md and len(md.strip()) > 100:  # skip near-empty pages
                    url_results[url] = md
            except Exception as e:
                logger.debug(f"Failed: {url}: {e}")
            finally:
                await page.close()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            # Process in batches to avoid overwhelming the browser
            batch_size = 50
            for i in range(0, len(all_urls), batch_size):
                batch = all_urls[i:i + batch_size]
                tasks = [_fetch_page(browser, url) for url in batch]
                await asyncio.gather(*tasks)
                logger.info(f"  Fetched {min(i + batch_size, len(all_urls))}/{len(all_urls)} URLs")
        finally:
            await browser.close()

    # Combine per-agency: take first MAX_PAGES_PER_SITE successful pages
    agency_texts: dict[str, str] = {}
    for aid, urls in agency_urls.items():
        chunks = []
        for url in urls:
            if len(chunks) >= MAX_PAGES_PER_SITE:
                break
            if url in url_results:
                text = _truncate(url_results[url])
                chunks.append(f"--- URL: {url} ---\n{text}")
        combined = "\n\n".join(chunks)
        if len(combined) > MAX_COMBINED_CHARS:
            combined = combined[:MAX_COMBINED_CHARS] + "\n\n[…combined truncated…]"
        if combined.strip():
            agency_texts[aid] = combined

    logger.info(f"Got text for {len(agency_texts)}/{len(agencies)} agencies")
    return agency_texts


def _save_raw_texts(agency_texts: dict[str, str]):
    """Save raw_website_text to the database."""
    sb = get_supabase()
    for aid, text in agency_texts.items():
        sb.table("agency_agencies").update({
            "raw_website_text": text,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", aid).execute()
    logger.info(f"Saved raw_website_text for {len(agency_texts)} agencies")


def reclassify_all(limit: int = 0, workers: int = 4):
    """Re-classify all agencies that have raw_website_text."""
    from classify_agency import classify_one, is_country_blacklisted
    from concurrent.futures import ThreadPoolExecutor, as_completed

    sb = get_supabase()
    profile = get_profile() or {}
    cfg = get_agency_config()
    threshold = cfg["agency_fit_threshold"]
    target_countries = cfg["agency_target_countries"]

    # Get all agencies with raw_website_text
    query = (
        sb.table("agency_agencies")
        .select("id,enriched_data,country,raw_website_text")
        .not_.is_("raw_website_text", "null")
    )
    if limit:
        query = query.limit(limit)
    rows = query.execute().data or []

    logger.info(f"Re-classifying {len(rows)} agencies with raw website text")

    def _worker(row):
        if is_country_blacklisted(row.get("country")):
            sb.table("agency_agencies").update({
                "status": "rejected", "fit_score": 0,
                "fit_reasoning": f"Auto-rejected: country {row.get('country')} is blacklisted.",
                "fit_breakdown": {"auto_reject": "country_blacklist", "country": row.get("country")},
                "last_classified_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", row["id"]).execute()
            return True

        try:
            result = classify_one(
                row["id"],
                row.get("enriched_data") or {},
                profile,
                target_countries,
                raw_website_text=row.get("raw_website_text") or "",
            )
            new_status = "qualified" if result["fit_score"] >= threshold else "rejected"
            sb.table("agency_agencies").update({
                **result,
                "status": new_status,
                "last_classified_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("id", row["id"]).execute()
            logger.info(f"Classified {row['id']}: {result['fit_score']} -> {new_status}")
            return True
        except Exception as e:
            logger.error(f"Classification failed for {row['id']}: {e}")
            return False

    success = 0
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_worker, row): row for row in rows}
        for future in as_completed(futures):
            done += 1
            try:
                if future.result():
                    success += 1
            except Exception as e:
                logger.error(f"Worker exception: {e}")
            if done % 20 == 0:
                logger.info(f"Progress: {done}/{len(rows)} done, {success} classified")

    logger.info(f"Re-classified {success}/{len(rows)} agencies")
    return success


def run(limit: int = 0, reclassify_only: bool = False,
        fetch_only: bool = False, workers: int = 4):
    sb = get_supabase()

    if not reclassify_only:
        # Get all agencies (regardless of status) to refetch
        query = sb.table("agency_agencies").select("id,website_url")
        if limit:
            query = query.limit(limit)
        agencies = query.execute().data or []
        logger.info(f"Will refetch {len(agencies)} agencies via Playwright")

        # Batch fetch with Playwright
        agency_texts = asyncio.run(_fetch_all_pages(agencies))

        # Save to DB
        _save_raw_texts(agency_texts)
        print(f"Fetched raw text for {len(agency_texts)}/{len(agencies)} agencies")

    if fetch_only:
        return

    # Re-classify
    n = reclassify_all(limit=limit, workers=workers)
    print(f"Re-classified {n} agencies")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    ap = argparse.ArgumentParser(description="Re-fetch and re-classify all agencies via Playwright.")
    ap.add_argument("--limit", type=int, default=0, help="Max agencies to process (0 = all)")
    ap.add_argument("--reclassify-only", action="store_true", help="Skip fetch, just re-classify")
    ap.add_argument("--fetch-only", action="store_true", help="Fetch and save raw text, skip classification")
    ap.add_argument("--workers", type=int, default=4, help="Parallel threads for classification")
    args = ap.parse_args()
    run(limit=args.limit, reclassify_only=args.reclassify_only,
        fetch_only=args.fetch_only, workers=args.workers)
