"""
Phase 3 — enrichment.

For each agency in `status='discovered'`, fetch a handful of pages
(`/`, `/about`, `/services`, `/work`, `/case-studies`, `/team`, `/contact`),
stop after 3 successful fetches, concatenate the cleaned markdown via
`common.http_fetch`, and send it to the LLM with a strict JSON-mode
extraction prompt. The result lands in `enriched_data`.

Extraction target schema:
    {
      "name":           str,
      "tagline":        str | None,
      "short_description": str,
      "services":       list[str],
      "tools":          list[str],    # n8n, make.com, zapier, openai, supabase, ...
      "industries":     list[str],
      "team_size":      str | None,   # "2-10", "11-50", ...
      "founded_year":   int | None,
      "city":           str | None,
      "country":        str | None,   # ISO alpha-2 when explicit
      "case_studies":   [{"title","url","summary"}, ...],
      "visible_emails": list[str],
      "team_members":   [{"name","role","linkedin"}],
      "red_flag_notes": list[str]     # e.g. "site looks abandoned", "no case studies since 2022"
    }

**Nothing raw ever reaches the LLM** — `common.http_fetch` guarantees
cleaned and truncated text.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse

from common.supabase_client import get_supabase
from common.llm import chat_completion
from common.http_fetch import fetch_clean_text

logger = logging.getLogger(__name__)

# Try these paths in order. Stop after MAX_FETCHES succeed.
# `/careers` and `/jobs` are included because many agencies expose their
# real contact email (and sometimes a live mailto:) only on the hiring
# page — not in the main footer. Worth the extra fetch.
_PATHS = ["/", "/about", "/about-us", "/services", "/what-we-do",
          "/work", "/case-studies", "/projects", "/team", "/contact",
          "/careers", "/jobs"]
MAX_FETCHES = 4
MAX_COMBINED_CHARS = 30_000  # hard cap before we send to LLM
STALE_ENRICHING_MINUTES = 15  # rows stuck in status='enriching' longer than this get rescued
DEFAULT_WORKERS = 4  # parallel threads for run_batch

_SYSTEM_PROMPT = (
    "You are a precise information extractor. Read the cleaned text of "
    "an agency's public website and return a strict JSON object with the "
    "fields listed by the user. "
    "Rules:\n"
    "- Only report what the text EXPLICITLY states. Never infer or guess.\n"
    "- Missing fields → null for scalars, [] for lists.\n"
    "- `tools` must be lowercased canonical names: n8n, make.com, zapier, "
    "openai, anthropic, supabase, retool, bubble, webflow, weweb, airtable, "
    "langchain, pinecone, clickup, notion, ... If a tool is not in any of "
    "those families, lowercase it as-is. Only include tools the agency "
    "explicitly says they USE or BUILD WITH.\n"
    "- `services` are short phrases (max 5 words each), e.g. "
    "'RAG chatbot development', 'n8n workflow automation'.\n"
    "- `country` must be ISO-3166 alpha-2 when stated explicitly "
    "(US, GB, DE, NZ, ...). If only city/region, leave country as null.\n"
    "- `case_studies` requires a title. URL/summary are optional.\n"
    "- `visible_emails` only contains addresses literally present in the "
    "text. No pattern guessing.\n"
    "- `best_contact_email`: from visible_emails, pick the SINGLE best "
    "address for a cold outreach email about contract work. Priority:\n"
    "  1. Personal email of founder/owner/CEO (e.g. john@agency.com)\n"
    "  2. contact@ or enquiries@ on the agency's own domain\n"
    "  3. hello@ on the agency's own domain\n"
    "  4. info@ on the agency's own domain\n"
    "  5. Any other non-system address on the agency's domain\n"
    "  Skip noreply@, no-reply@, careers@, jobs@, support@ (wrong audience). "
    "  Skip emails on third-party domains (gmail.com, outlook.com) unless "
    "  it's clearly the founder's personal email. Return null if no usable "
    "  email exists.\n"
    "- `red_flag_notes` is where you can note concerns for the classifier "
    "later (e.g. 'no case studies since 2022', 'team page shows one "
    "person', 'enterprise-only language — says \"Fortune 500\"')."
)

_JSON_SCHEMA_HINT = (
    "Return ONLY this JSON object:\n"
    "{\n"
    '  "name": "<agency name>",\n'
    '  "tagline": "<one-line tagline or null>",\n'
    '  "short_description": "<2-3 sentence description>",\n'
    '  "services": ["..."],\n'
    '  "tools": ["..."],\n'
    '  "industries": ["..."],\n'
    '  "team_size": "<2-10|11-50|51-200|null>",\n'
    '  "founded_year": <int|null>,\n'
    '  "city": "<city|null>",\n'
    '  "country": "<ISO-3166 alpha-2|null>",\n'
    '  "case_studies": [{"title":"","url":"","summary":""}],\n'
    '  "visible_emails": ["..."],\n'
    '  "best_contact_email": "<single best email for cold outreach or null>",\n'
    '  "team_members": [{"name":"","role":"","linkedin":""}],\n'
    '  "red_flag_notes": ["..."]\n'
    "}"
)


def _fetch_pages(base_url: str, backend: str = "jina") -> str:
    """Try each path in order; return concatenated cleaned text from the
    first MAX_FETCHES that returned anything.

    backend="crw" uses self-hosted CRW (free, fast, best Cloudflare bypass).
    backend="jina" uses r.jina.ai (free, slower).
    backend="scraper" uses ScraperAPI (10 credits/req, paid).
    """
    parsed = urlparse(base_url if "://" in base_url else f"https://{base_url}")
    root = f"{parsed.scheme or 'https'}://{parsed.netloc or parsed.path}"

    paths = _PATHS
    max_fetches = MAX_FETCHES

    chunks: list[str] = []
    fetched_urls: list[str] = []
    for path in paths:
        if len(chunks) >= max_fetches:
            break
        url = urljoin(root + "/", path.lstrip("/"))
        text = fetch_clean_text(url, backend=backend)
        if text:
            chunks.append(f"--- URL: {url} ---\n{text}")
            fetched_urls.append(url)

    combined = "\n\n".join(chunks)
    if len(combined) > MAX_COMBINED_CHARS:
        combined = combined[:MAX_COMBINED_CHARS] + "\n\n[…combined truncated…]"
    logger.info(f"Fetched {len(fetched_urls)} pages for {base_url} [backend={backend}]")
    return combined


def _extract_via_llm(text: str) -> dict:
    """Send cleaned text to OpenAI and parse the JSON extraction."""
    response = chat_completion(
        model="gpt-4.1-mini",
        response_format={"type": "json_object"},
        service_tier="flex",
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"{_JSON_SCHEMA_HINT}\n\nWEBSITE TEXT:\n{text}"},
        ],
    )
    return json.loads(response.choices[0].message.content)


def enrich_one(agency_id: str, website_url: str, backend: str = "jina") -> dict | None:
    """Enrich a single agency. Writes `enriched_data` + bumps status.

    Returns the extracted dict, or None if nothing usable was fetched.
    """
    text = _fetch_pages(website_url, backend=backend)
    if not text.strip():
        logger.warning(f"No content fetched for {agency_id}")
        get_supabase().table("agency_agencies").update({
            "status": "enrich_failed",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", agency_id).execute()
        return None

    data = _extract_via_llm(text)

    # Persist the enriched data + raw text + pull selected fields up to dedicated columns
    update: dict = {
        "enriched_data": data,
        "raw_website_text": text,
        "status": "enriched",
        "last_enriched_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if data.get("name"):
        update["name"] = data["name"]
    if data.get("short_description"):
        update["short_description"] = data["short_description"]
    if data.get("country"):
        update["country"] = data["country"]
    if data.get("city"):
        update["city"] = data["city"]
    if data.get("team_size"):
        update["team_size"] = data["team_size"]
    if data.get("founded_year"):
        update["founded_year"] = data["founded_year"]
    if data.get("tools"):
        update["specialization"] = data["tools"]

    get_supabase().table("agency_agencies").update(update).eq("id", agency_id).execute()
    return data


def _rescue_stale_enriching(sb) -> int:
    """Flip rows that got stuck in status='enriching' back to 'discovered'.

    If a previous run crashed between the in-progress flip and the final
    update, the row is orphaned: not discovered (won't be picked up) and
    not enriched (no data). This resets any `enriching` row whose
    `updated_at` is older than STALE_ENRICHING_MINUTES.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=STALE_ENRICHING_MINUTES)).isoformat()
    res = (
        sb.table("agency_agencies")
        .update({"status": "discovered", "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("status", "enriching")
        .lt("updated_at", cutoff)
        .execute()
    )
    n = len(res.data or [])
    if n:
        logger.warning(f"Rescued {n} stale 'enriching' rows back to 'discovered'")
    return n


def _enrich_worker(row: dict, backend: str = "jina") -> bool:
    """Enrich a single row — designed to run inside a thread pool."""
    sb = get_supabase()
    sb.table("agency_agencies").update({"status": "enriching"}).eq("id", row["id"]).execute()
    try:
        if enrich_one(row["id"], row["website_url"], backend=backend):
            return True
        return False
    except Exception as e:
        logger.error(f"Enrichment failed for {row['id']}: {e}")
        sb.table("agency_agencies").update({
            "status": "enrich_failed",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", row["id"]).execute()
        return False


def run_batch(limit: int = 20, dry_run: bool = False, workers: int = DEFAULT_WORKERS) -> int:
    """Enrich up to `limit` agencies currently in `status='discovered'`.
    Uses `workers` parallel threads for ~4x speedup.
    Returns the number of rows successfully enriched.
    """
    sb = get_supabase()
    _rescue_stale_enriching(sb)
    rows = (
        sb.table("agency_agencies")
        .select("id,website_url")
        .eq("status", "discovered")
        .limit(limit)
        .execute()
        .data
        or []
    )

    if dry_run:
        logger.info(f"[dry-run] would enrich {len(rows)} rows")
        for r in rows[:10]:
            logger.info(f"  - {r['id']} ({r['website_url']})")
        if len(rows) > 10:
            logger.info(f"  ... and {len(rows) - 10} more")
        return 0

    # Backend preference: CRW (self-hosted, best Cloudflare bypass) > ScraperAPI
    # (paid, decent) > jina (free fallback). CRW is free and local-ish, so
    # prefer it whenever CRW_API_URL is set.
    if os.environ.get("CRW_API_URL"):
        backend = "crw"
    elif os.environ.get("SCRAPERAPI_KEY"):
        backend = "scraper"
    else:
        backend = "jina"
    logger.info(f"Enriching {len(rows)} agencies via {backend}, {workers} workers")

    success = 0
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}
        for row in rows:
            futures[pool.submit(_enrich_worker, row, backend)] = row
        for future in as_completed(futures):
            row = futures[future]
            done += 1
            try:
                if future.result():
                    success += 1
            except Exception as e:
                logger.error(f"Worker exception for {row['id']}: {e}")
            if done % 20 == 0:
                logger.info(f"Progress: {done}/{len(rows)} done, {success} enriched")
    return success


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="Enrich discovered agencies via web fetch + LLM.")
    ap.add_argument("--limit", type=int, default=20, help="Max rows to process in this batch (default: 20)")
    ap.add_argument("--dry-run", action="store_true", help="List the rows that would be enriched and exit")
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help=f"Parallel threads (default: {DEFAULT_WORKERS})")
    args = ap.parse_args()
    n = run_batch(limit=args.limit, dry_run=args.dry_run, workers=args.workers)
    if not args.dry_run:
        print(f"Enriched {n} agencies.")
