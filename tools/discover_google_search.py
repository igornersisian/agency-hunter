"""
Phase 1 discovery — run Google Search via Apify.

Actor: apify/google-search-scraper
Docs:  https://apify.com/apify/google-search-scraper

Reads query templates from `tools/config/serp_queries.json`, expands them
against target countries (config or `--country`), runs them through Apify
in ONE batch, then flattens the SERP output to normalized candidate rows.

Directory/platform domains (clutch.co, linkedin.com, wikipedia.org, ...)
are filtered out via `common.domain_utils.is_directory_domain` because
we want the agencies themselves, not listings of them.

A `CandidateRow` looks like:
    {
      "id":          "acme-automation.com",          # canonical root domain
      "name":        "Acme Automation",              # SERP title best-effort
      "website_url": "https://www.acme-automation.com/",
      "country":     "NZ",                            # ISO alpha-2 from query
      "source_channel": "apify_google_search",
      "source_url":  "https://www.google.com/search?q=...",
      "raw_payload": {...}                            # full SERP item
    }

Usage:
    python tools/discover_google_search.py                     # all configured countries
    python tools/discover_google_search.py --country NZ        # one country
    python tools/discover_google_search.py --country NZ --max 20
    python tools/discover_google_search.py --dry-run           # print, don't write DB
"""

from __future__ import annotations

import json
import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from common.apify_runner import run_and_collect
from common.domain_utils import canonical_domain, is_directory_domain
from common.supabase_client import get_supabase

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

ACTOR_ID = "apify~google-search-scraper"
CHANNEL = "apify_google_search"

CONFIG_PATH = Path(__file__).resolve().parent / "config" / "serp_queries.json"


def _load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _build_queries(cfg: dict, country_filter: str | None = None) -> list[tuple[str, str]]:
    """Expand templates × countries → list of (query_string, country_code)."""
    pairs: list[tuple[str, str]] = []
    for country in cfg["countries"]:
        if country_filter and country["code"] != country_filter.upper():
            continue
        for tpl in cfg["templates"]:
            q = tpl.format(country_name=country["name"], country_code=country["code"])
            pairs.append((q, country["code"]))
    return pairs


def _run_apify(queries: list[str], results_per_query: int, language: str) -> list[dict]:
    """Call the Apify Google search scraper with a batch of queries."""
    actor_input = {
        # apify/google-search-scraper accepts a newline-delimited string of queries
        "queries": "\n".join(queries),
        "maxPagesPerQuery": 1,
        "resultsPerPage": results_per_query,
        "mobileResults": False,
        "languageCode": language,
        "saveHtml": False,
        "saveHtmlToKeyValueStore": False,
    }
    # 30 min — large multi-country runs (~290 queries) typically take
    # 5-15 min through apify/google-search-scraper; 600s was too tight
    # after the country-list expansion.
    return run_and_collect(ACTOR_ID, actor_input, timeout_seconds=1800)


def _flatten_serp(raw_items: list[dict], query_country_map: dict[str, str]) -> list[dict]:
    """Turn Apify's per-query results into a flat list of CandidateRow dicts.

    The apify/google-search-scraper returns one object per search query,
    each with an `organicResults` list. We keep organic hits only — no
    ads, no "People also ask", no map packs.
    """
    candidates: list[dict] = []
    for item in raw_items:
        query = item.get("searchQuery", {}).get("term") or item.get("query", "")
        country_code = query_country_map.get(query, "")
        organic = item.get("organicResults") or []
        for hit in organic:
            url = hit.get("url") or hit.get("link") or ""
            title = (hit.get("title") or "").strip()
            description = (hit.get("description") or "").strip()
            domain = canonical_domain(url)
            if not domain:
                continue
            if is_directory_domain(domain):
                continue

            candidates.append({
                "id": domain,
                "name": title or domain,
                "domain": domain,
                "website_url": url,
                "country": country_code,
                "short_description": description[:500],
                "source_channel": CHANNEL,
                "source_url": url,
                "raw_payload": hit,
            })
    return candidates


def _dedup_by_domain(candidates: list[dict]) -> list[dict]:
    """Keep the first occurrence per canonical domain within this batch.

    Global dedup against Supabase happens in `dedup_canonicalize.py`.
    """
    seen: set[str] = set()
    unique: list[dict] = []
    for c in candidates:
        if c["id"] in seen:
            continue
        seen.add(c["id"])
        unique.append(c)
    return unique


def _persist(candidates: list[dict]) -> tuple[int, int]:
    """Upsert into agency_agencies + always append an agency_sources row.

    Returns (new_agencies, total_source_rows).
    """
    if not candidates:
        return 0, 0

    sb = get_supabase()
    new_count = 0

    for c in candidates:
        agency_row = {
            "id": c["id"],
            "name": c["name"],
            "domain": c["domain"],
            "website_url": c["website_url"],
            "country": c["country"] or None,
            "short_description": c["short_description"] or None,
            "status": "discovered",
            "discovered_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        # Insert only if missing — never overwrite an already-classified row
        existing = sb.table("agency_agencies").select("id").eq("id", c["id"]).limit(1).execute()
        if not existing.data:
            sb.table("agency_agencies").insert(agency_row).execute()
            new_count += 1

        source_row = {
            "agency_id": c["id"],
            "channel": CHANNEL,
            "source_url": c["source_url"],
            "raw_payload": c["raw_payload"],
        }
        sb.table("agency_sources").insert(source_row).execute()

    return new_count, len(candidates)


def _record_run(status: str, candidates_found: int, new_agencies: int,
                error: str | None = None, metadata: dict | None = None) -> None:
    try:
        get_supabase().table("agency_discovery_runs").insert({
            "channel": CHANNEL,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "candidates_found": candidates_found,
            "new_agencies": new_agencies,
            "error_message": error,
            "metadata": metadata or {},
        }).execute()
    except Exception as e:
        logger.warning(f"Failed to record discovery run: {e}")


def discover(country: str | None = None, max_queries: int | None = None,
             dry_run: bool = False) -> list[dict]:
    """Run one discovery pass. Returns the list of candidate dicts found."""
    cfg = _load_config()
    pairs = _build_queries(cfg, country_filter=country)
    if max_queries is not None:
        pairs = pairs[:max_queries]
    if not pairs:
        logger.warning("No queries built — check serp_queries.json and --country filter")
        return []

    queries = [p[0] for p in pairs]
    query_country_map = {q: c for q, c in pairs}

    logger.info(f"Running {len(queries)} queries through Apify...")
    try:
        raw = _run_apify(
            queries,
            results_per_query=cfg.get("results_per_query", 20),
            language=cfg.get("language", "en"),
        )
    except Exception as e:
        logger.error(f"Apify run failed: {e}")
        _record_run("error", 0, 0, error=str(e))
        raise

    candidates = _flatten_serp(raw, query_country_map)
    candidates = _dedup_by_domain(candidates)
    logger.info(f"Flattened → {len(candidates)} unique candidates after filter")

    if dry_run:
        print(json.dumps(candidates[:20], ensure_ascii=False, indent=2))
        _record_run("success", len(candidates), 0, metadata={"dry_run": True})
        return candidates

    new_count, total_sources = _persist(candidates)
    logger.info(f"Persisted: {new_count} new agencies, {total_sources} source rows")

    _record_run(
        "success",
        candidates_found=len(candidates),
        new_agencies=new_count,
        metadata={"queries": len(queries), "country": country},
    )
    return candidates


def main() -> None:
    parser = argparse.ArgumentParser(description="Google search discovery via Apify")
    parser.add_argument("--country", help="Restrict to one ISO-3166 alpha-2 code (e.g. NZ)")
    parser.add_argument("--max", type=int, help="Cap on total queries to run (for smoke tests)")
    parser.add_argument("--dry-run", action="store_true", help="Print candidates, don't write DB")
    args = parser.parse_args()

    results = discover(country=args.country, max_queries=args.max, dry_run=args.dry_run)
    logger.info(f"Done. {len(results)} candidates total.")


if __name__ == "__main__":
    main()
