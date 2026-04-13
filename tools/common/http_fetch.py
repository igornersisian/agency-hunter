"""
HTML → clean text for LLM consumption.

Primary path:   r.jina.ai/{url} returns pre-cleaned markdown (handles JS
                rendering, strips nav/footer/scripts/styles/menus). Free.
Fallback path:  httpx.get(url) + selectolax to parse HTML, remove
                script/style/nav/footer/header/aside/noscript/svg nodes,
                extract visible text from <main>/<article> if present,
                else <body>. Collapses whitespace.

Final output is truncated to MAX_CHARS characters (~3.5k tokens at
cl100k) with an ellipsis marker. **Nothing raw ever reaches the LLM.**
"""

from __future__ import annotations

import itertools
import os
import re
import threading
import time
import logging

import asyncio

import httpx
import html2text
from selectolax.parser import HTMLParser

logger = logging.getLogger(__name__)

MAX_CHARS = 15_000
DEFAULT_TIMEOUT = 20

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

_STRIP_TAGS = (
    "script", "style", "nav", "footer", "header", "aside",
    "noscript", "svg", "form", "iframe", "button",
)


def _truncate(text: str, limit: int = MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n\n[…truncated…]"


def _clean_html(html: str) -> str:
    """Fallback parser: strip chrome and extract main content as text."""
    try:
        tree = HTMLParser(html)
    except Exception as e:
        logger.warning(f"selectolax parse failed: {e}")
        return ""

    for tag in _STRIP_TAGS:
        for node in tree.css(tag):
            node.decompose()

    # Prefer <main> or <article> when available, else <body>.
    target = tree.css_first("main") or tree.css_first("article") or tree.body
    if target is None:
        return ""

    text = target.text(separator="\n", strip=True)
    # Collapse runs of whitespace while preserving paragraph breaks.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _fetch_jina(url: str, timeout: int) -> str | None:
    """Try r.jina.ai first — it returns pre-cleaned markdown.

    Retries on HTTP 429 (rate limit) with exponential backoff. r.jina.ai's
    free tier throttles at burst rates — a simple 5→15→30s backoff is
    usually enough to get unstuck. On 4xx/5xx other than 429 we give up
    immediately and let the direct-fetch fallback take over.
    """
    jina_url = f"https://r.jina.ai/{url}"
    backoffs = (5, 15, 30)
    for attempt, wait in enumerate((0,) + backoffs):
        if wait:
            logger.info(f"r.jina.ai 429 backoff {wait}s (attempt {attempt})")
            time.sleep(wait)
        try:
            resp = httpx.get(
                jina_url,
                timeout=timeout,
                headers={"User-Agent": _USER_AGENT, "Accept": "text/plain"},
                follow_redirects=True,
            )
            if resp.status_code == 200 and resp.text:
                return resp.text
            if resp.status_code == 429:
                continue
            logger.info(f"r.jina.ai returned {resp.status_code} for {url}")
            return None
        except Exception as e:
            logger.info(f"r.jina.ai failed for {url}: {e}")
            return None
    logger.warning(f"r.jina.ai exhausted 429 retries for {url}")
    return None


def _get_scraper_keys() -> list[str]:
    """Collect all SCRAPERAPI_KEY* env vars."""
    keys = []
    k1 = os.environ.get("SCRAPERAPI_KEY")
    if k1:
        keys.append(k1)
    for i in range(2, 10):
        k = os.environ.get(f"SCRAPERAPI_KEY_{i}")
        if k:
            keys.append(k)
    return keys

_scraper_key_cycle: itertools.cycle | None = None
_scraper_key_lock = threading.Lock()

def _next_scraper_key() -> str | None:
    """Thread-safe round-robin across all ScraperAPI keys."""
    global _scraper_key_cycle
    with _scraper_key_lock:
        if _scraper_key_cycle is None:
            keys = _get_scraper_keys()
            if not keys:
                return None
            _scraper_key_cycle = itertools.cycle(keys)
        return next(_scraper_key_cycle)


def _fetch_scraperapi(url: str, timeout: int) -> str | None:
    """Fetch via ScraperAPI with JS render + markdown output.

    Costs 10 credits per request (render=true). Returns clean markdown
    similar to r.jina.ai. Round-robins across all available keys.
    """
    api_key = _next_scraper_key()
    if not api_key:
        return None
    try:
        resp = httpx.get(
            "https://api.scraperapi.com",
            params={
                "api_key": api_key,
                "url": url,
                "render": "true",
                "output_format": "markdown",
            },
            timeout=timeout,
            follow_redirects=True,
        )
        if resp.status_code == 200 and resp.text:
            return resp.text
        if resp.status_code == 429:
            logger.info(f"ScraperAPI 429 for {url}")
        else:
            logger.info(f"ScraperAPI returned {resp.status_code} for {url}")
        return None
    except Exception as e:
        logger.info(f"ScraperAPI failed for {url}: {e}")
        return None


def _html_to_markdown(html: str) -> str:
    """Convert HTML to clean markdown using html2text."""
    h = html2text.HTML2Text()
    h.ignore_links = False
    h.ignore_images = True
    h.body_width = 0
    h.ignore_emphasis = False
    h.skip_internal_links = True
    return h.handle(html).strip()


async def _playwright_fetch_one(url: str, timeout: int) -> str | None:
    """Fetch a single URL with Playwright (headless Chromium + JS render)."""
    from playwright.async_api import async_playwright
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(
                user_agent=_USER_AGENT,
                ignore_https_errors=True,
            )
            try:
                await page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
                # Give JS a moment to render dynamic content
                await page.wait_for_timeout(2000)
                html = await page.content()
            except Exception as e:
                logger.info(f"Playwright navigation failed for {url}: {e}")
                return None
            finally:
                await browser.close()
        md = _html_to_markdown(html)
        return md if md else None
    except Exception as e:
        logger.info(f"Playwright failed for {url}: {e}")
        return None


async def playwright_fetch_many(urls: list[str], timeout: int = DEFAULT_TIMEOUT,
                                max_concurrent: int = 5) -> dict[str, str]:
    """Fetch multiple URLs with a single browser instance, limited concurrency.

    Returns {url: markdown_text} for successful fetches. Failed URLs are omitted.
    """
    from playwright.async_api import async_playwright
    results: dict[str, str] = {}
    sem = asyncio.Semaphore(max_concurrent)

    async def _fetch_page(browser, url: str):
        async with sem:
            page = await browser.new_page(
                user_agent=_USER_AGENT,
                ignore_https_errors=True,
            )
            try:
                await page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                html = await page.content()
                md = _html_to_markdown(html)
                if md:
                    results[url] = _truncate(md)
            except Exception as e:
                logger.info(f"Playwright failed for {url}: {e}")
            finally:
                await page.close()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            tasks = [_fetch_page(browser, url) for url in urls]
            await asyncio.gather(*tasks)
        finally:
            await browser.close()

    return results


def _fetch_playwright(url: str, timeout: int) -> str | None:
    """Sync wrapper around Playwright async fetch."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # Already inside an event loop (e.g. Jupyter) — use new thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(
                lambda: asyncio.run(_playwright_fetch_one(url, timeout))
            ).result()
    else:
        return asyncio.run(_playwright_fetch_one(url, timeout))


def _fetch_direct(url: str, timeout: int) -> str | None:
    """Fallback: fetch the page directly and clean HTML ourselves."""
    try:
        resp = httpx.get(
            url,
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            follow_redirects=True,
        )
        if resp.status_code != 200:
            logger.info(f"direct fetch returned {resp.status_code} for {url}")
            return None
        return _clean_html(resp.text)
    except Exception as e:
        logger.info(f"direct fetch failed for {url}: {e}")
        return None


def fetch_clean_text(url: str, timeout: int = DEFAULT_TIMEOUT,
                     backend: str = "jina") -> str:
    """Fetch a URL and return clean text ready for an LLM prompt.

    backend="jina"  → r.jina.ai first, then direct fetch (default)
    backend="scraper" → ScraperAPI first, then direct fetch

    Returns an empty string on total failure. Always truncated to
    `MAX_CHARS`.
    """
    if not url:
        return ""

    if backend == "playwright":
        cleaned = _fetch_playwright(url, timeout)
    elif backend == "scraper":
        cleaned = _fetch_scraperapi(url, timeout)
    else:
        cleaned = _fetch_jina(url, timeout)
        if not cleaned:
            cleaned = _fetch_direct(url, timeout)
    if not cleaned:
        return ""
    return _truncate(cleaned)
