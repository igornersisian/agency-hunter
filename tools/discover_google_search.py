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

from urllib.parse import urlparse

import httpx

from common.apify_runner import run_and_collect
from common.domain_utils import canonical_domain, is_directory_domain
from common.supabase_client import get_supabase, reset_supabase

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


def _build_queries(cfg: dict, mode: str = "countries",
                   country_filter: str | None = None) -> list[tuple[str, str]]:
    """Expand templates into (query_string, country_code) pairs.

    Modes:
      countries  — cfg["templates"] × cfg["countries"]; country_code from country
      worldwide  — cfg["worldwide_templates"] as-is; country_code = "" (→ NULL in DB)
      cities     — cfg["city_templates"] × cfg["cities"]; country_code from city's hub
      v2         — cfg["v2_worldwide_templates"] as-is + cfg["v2_country_templates"]
                   × cfg["countries"]; round-3 vocabulary, never re-runs old keys
      (local mode has per-language batches — see _build_local_batches)
    """
    pairs: list[tuple[str, str]] = []

    if mode == "countries":
        for country in cfg["countries"]:
            if country_filter and country["code"] != country_filter.upper():
                continue
            for tpl in cfg["templates"]:
                q = tpl.format(country_name=country["name"], country_code=country["code"])
                pairs.append((q, country["code"]))

    elif mode == "worldwide":
        for tpl in cfg.get("worldwide_templates", []):
            pairs.append((tpl, ""))

    elif mode == "cities":
        for city in cfg.get("cities", []):
            if country_filter and city["country_code"] != country_filter.upper():
                continue
            for tpl in cfg.get("city_templates", []):
                q = tpl.format(city=city["city"])
                pairs.append((q, city["country_code"]))

    elif mode == "v2":
        if not country_filter:
            for tpl in cfg.get("v2_worldwide_templates", []):
                pairs.append((tpl, ""))
        for country in cfg["countries"]:
            if country_filter and country["code"] != country_filter.upper():
                continue
            for tpl in cfg.get("v2_country_templates", []):
                q = tpl.format(country_name=country["name"], country_code=country["code"])
                pairs.append((q, country["code"]))

    else:
        raise ValueError(f"Unknown mode: {mode!r} (expected countries|worldwide|cities|v2|local)")

    return pairs


def _build_local_batches(cfg: dict, language_filter: str | None = None,
                         country_filter: str | None = None) -> list[tuple[str, list[tuple[str, str]]]]:
    """Expand cfg["local_groups"] into [(language, [(query, country_code), ...]), ...].

    The Apify actor takes ONE languageCode per run, so local-language
    queries are grouped by language and each group becomes its own actor
    run. A --country filter applies within groups (e.g. CH hits both the
    de and fr groups)."""
    batches: list[tuple[str, list[tuple[str, str]]]] = []
    for group in cfg.get("local_groups", []):
        lang = group["language"]
        if language_filter and lang != language_filter.lower():
            continue
        pairs: list[tuple[str, str]] = []
        for country in group["countries"]:
            if country_filter and country["code"] != country_filter.upper():
                continue
            for tpl in group["templates"]:
                q = tpl.format(country_name=country["name"], country_code=country["code"])
                pairs.append((q, country["code"]))
        if pairs:
            batches.append((lang, pairs))
    return batches


def _run_apify(queries: list[str], language: str, max_pages: int = 50) -> list[dict]:
    """Call the Apify Google search scraper with a batch of queries.

    Google's 2024 SERP change capped organic results at 10/page, and the
    actor IGNORES `resultsPerPage` since then — only `maxPagesPerQuery`
    controls volume. Apify auto-stops on `hasNextPage: false` (Google's
    "We've omitted some results" wall — typically page 30-40 for broad
    queries, much earlier for narrow ones).
    """
    actor_input = {
        "queries": "\n".join(queries),
        "maxPagesPerQuery": max_pages,
        "mobileResults": False,
        "languageCode": language,
        "saveHtml": False,
        "saveHtmlToKeyValueStore": False,
    }
    # Deep pagination: 50 pages × ~3s/page × N queries can take hours.
    # 3h ceiling covers worldwide (15 q × 35 pages) and large city batches.
    return run_and_collect(ACTOR_ID, actor_input, timeout_seconds=10800)


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

            # Normalize website_url to the homepage. Google often returns
            # a deep link (blog post, case study page) because that's the
            # page that matched the query — but the agency's actual
            # website_url should be their root. The deep link is kept in
            # source_url for trace-back.
            parsed = urlparse(url)
            netloc = parsed.netloc or domain
            scheme = parsed.scheme or "https"
            homepage_url = f"{scheme}://{netloc}/"

            candidates.append({
                "id": domain,
                "name": title or domain,
                "domain": domain,
                "website_url": homepage_url,
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
    source_count = 0

    def _persist_one(client, c):
        nonlocal new_count, source_count
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
        existing = client.table("agency_agencies").select("id").eq("id", c["id"]).limit(1).execute()
        if not existing.data:
            client.table("agency_agencies").insert(agency_row).execute()
            new_count += 1

        source_row = {
            "agency_id": c["id"],
            "channel": CHANNEL,
            "source_url": c["source_url"],
            "raw_payload": c["raw_payload"],
        }
        client.table("agency_sources").insert(source_row).execute()
        source_count += 1

    for c in candidates:
        try:
            _persist_one(sb, c)
        except (httpx.HTTPError, httpx.LocalProtocolError) as e:
            logger.warning(f"httpx error on {c['id']}: {e}. Resetting client and retrying.")
            sb = reset_supabase()
            _persist_one(sb, c)

    return new_count, source_count


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
             dry_run: bool = False, mode: str = "countries",
             language: str | None = None) -> list[dict]:
    """Run one discovery pass. Returns the list of candidate dicts found."""
    cfg = _load_config()

    if mode == "local":
        return _discover_local(cfg, country=country, max_queries=max_queries,
                               dry_run=dry_run, language=language)

    pairs = _build_queries(cfg, mode=mode, country_filter=country)
    if max_queries is not None:
        pairs = pairs[:max_queries]
    if not pairs:
        logger.warning("No queries built — check serp_queries.json and --country filter")
        return []

    queries = [p[0] for p in pairs]
    query_country_map = {q: c for q, c in pairs}

    # v2 mixes in unquoted templates that would otherwise paginate to
    # Google's page-30-40 "omitted results" wall — cap depth for cost.
    max_pages = 10 if mode == "v2" else 50

    logger.info(f"Running {len(queries)} queries through Apify...")
    try:
        raw = _run_apify(
            queries,
            language=cfg.get("language", "en"),
            max_pages=max_pages,
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
        _record_run("success", len(candidates), 0, metadata={"dry_run": True, "mode": mode})
        return candidates

    new_count, total_sources = _persist(candidates)
    logger.info(f"Persisted: {new_count} new agencies, {total_sources} source rows")

    _record_run(
        "success",
        candidates_found=len(candidates),
        new_agencies=new_count,
        metadata={"queries": len(queries), "country": country, "mode": mode},
    )
    return candidates


def _discover_local(cfg: dict, country: str | None = None,
                    max_queries: int | None = None, dry_run: bool = False,
                    language: str | None = None) -> list[dict]:
    """Local-language discovery: one Apify run per language batch.

    Failures are isolated per language (error run row + continue) so one
    bad batch doesn't lose the others; replay a single language with
    `resume_persist_dataset.py --mode local --language XX`."""
    batches = _build_local_batches(cfg, language_filter=language, country_filter=country)
    if not batches:
        logger.warning("No local batches built — check local_groups and filters")
        return []

    all_candidates: list[dict] = []
    for lang, pairs in batches:
        if max_queries is not None:
            pairs = pairs[:max_queries]
        queries = [p[0] for p in pairs]
        query_country_map = {q: c for q, c in pairs}

        logger.info(f"[local/{lang}] Running {len(queries)} queries through Apify...")
        try:
            raw = _run_apify(queries, language=lang, max_pages=10)
        except Exception as e:
            logger.error(f"[local/{lang}] Apify run failed: {e}")
            _record_run("error", 0, 0, error=str(e),
                        metadata={"mode": "local", "language": lang, "queries": len(queries)})
            continue

        candidates = _dedup_by_domain(_flatten_serp(raw, query_country_map))
        logger.info(f"[local/{lang}] → {len(candidates)} unique candidates after filter")

        if dry_run:
            print(json.dumps(candidates[:20], ensure_ascii=False, indent=2))
            _record_run("success", len(candidates), 0,
                        metadata={"dry_run": True, "mode": "local", "language": lang,
                                  "queries": len(queries)})
        else:
            new_count, total_sources = _persist(candidates)
            logger.info(f"[local/{lang}] Persisted: {new_count} new, {total_sources} source rows")
            _record_run("success", candidates_found=len(candidates), new_agencies=new_count,
                        metadata={"mode": "local", "language": lang, "queries": len(queries)})

        all_candidates.extend(candidates)

    return all_candidates


def main() -> None:
    parser = argparse.ArgumentParser(description="Google search discovery via Apify")
    parser.add_argument("--country", help="Restrict to one ISO-3166 alpha-2 code (e.g. NZ)")
    parser.add_argument("--max", type=int, help="Cap on total queries to run (for smoke tests)")
    parser.add_argument("--dry-run", action="store_true", help="Print candidates, don't write DB")
    parser.add_argument(
        "--mode",
        choices=["countries", "worldwide", "cities", "v2", "local"],
        default="countries",
        help="Query-building strategy (default: countries — original behavior)",
    )
    parser.add_argument(
        "--language",
        help="local mode only: run a single language group (e.g. de, es)",
    )
    args = parser.parse_args()

    results = discover(country=args.country, max_queries=args.max,
                       dry_run=args.dry_run, mode=args.mode,
                       language=args.language)
    logger.info(f"Done. {len(results)} candidates total.")


if __name__ == "__main__":
    main()
