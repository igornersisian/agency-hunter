"""
Agency Hunter pipeline orchestrator.

State machine on agency_agencies.status. Each phase is idempotent — it
only operates on rows in the status it expects, so re-running the
pipeline picks up wherever the last run stopped.

Phases:
    0. Discovery   — apify google search → insert `discovered` rows
    1. Dedup       — fold collisions
    2. Enrichment  — `discovered` → `enriched` (LLM extraction)
    3. Classify    — `enriched`   → `qualified` | `rejected`

Outreach is fully manual (since 2026-06): the pipeline stops at
classification. Qualified agencies surface as cards in the Telegram
/review queue; Igor researches each site, writes and sends the email
himself from Gmail, then marks the card Sent/Skip. No AI-generated
messages (LLM drafting survives only as an opt-in CLI:
`python tools/draft_outreach.py`).

Parallelism mirrors the sibling project: ThreadPoolExecutor batches
inside each phase but phases run sequentially so the state machine
stays coherent.
"""

from __future__ import annotations

import os
import sys
import logging
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(__file__))

from common.supabase_client import get_supabase
from common.profile import get_profile, get_agency_config
from common.domain_utils import is_directory_domain

import discover_google_search
import dedup_canonicalize
import enrich_agency
import classify_agency

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

ENRICH_WORKERS = 5
CLASSIFY_WORKERS = 5


def _enrich_phase() -> int:
    sb = get_supabase()
    rows = sb.table("agency_agencies").select("id,website_url").eq("status", "discovered").execute().data or []
    if not rows:
        return 0

    # Safety net: reject any row whose domain is on the blacklist before we
    # spend enrichment budget on it. The blacklist is normally checked at
    # discovery time, but (a) older rows predate newer blacklist additions
    # and (b) rows created through other paths (manual inserts, retries)
    # can still slip through. Catch them here.
    kept: list[dict] = []
    for row in rows:
        if is_directory_domain(row["id"]):
            logger.info(f"enrich safety net: rejecting blacklisted domain {row['id']}")
            sb.table("agency_agencies").update({"status": "rejected"}).eq("id", row["id"]).execute()
            continue
        kept.append(row)
    rows = kept
    if not rows:
        return 0

    # Flip to enriching first so re-runs don't double-process
    for row in rows:
        sb.table("agency_agencies").update({"status": "enriching"}).eq("id", row["id"]).execute()

    success = 0
    with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as pool:
        futures = {pool.submit(enrich_agency.enrich_one, row["id"], row["website_url"]): row for row in rows}
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                if fut.result():
                    success += 1
            except Exception as e:
                logger.error(f"enrich_one failed for {row['id']}: {e}")
                sb.table("agency_agencies").update({"status": "enrich_failed"}).eq("id", row["id"]).execute()
    return success


def _classify_phase() -> tuple[int, int]:
    sb = get_supabase()
    profile = get_profile() or {}
    cfg = get_agency_config()
    threshold = cfg["agency_fit_threshold"]
    target_countries = cfg["agency_target_countries"]

    rows = sb.table("agency_agencies").select("id,country,enriched_data").eq("status", "enriched").execute().data or []
    if not rows:
        return 0, 0

    for row in rows:
        sb.table("agency_agencies").update({"status": "classifying"}).eq("id", row["id"]).execute()

    classified = 0
    qualified = 0
    with ThreadPoolExecutor(max_workers=CLASSIFY_WORKERS) as pool:
        futures = {
            pool.submit(classify_agency.classify_one, row["id"], row.get("enriched_data") or {}, profile, target_countries): row
            for row in rows
        }
        from datetime import datetime, timezone
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                result = fut.result()
                # Country blacklist — override fit score, force reject.
                if classify_agency.is_country_blacklisted(row.get("country")):
                    new_status = "rejected"
                    result["flagged_issues"] = (result.get("flagged_issues") or []) + [
                        f"country {row.get('country')} is on the hard blacklist"
                    ]
                else:
                    new_status = "qualified" if result["fit_score"] >= threshold else "rejected"
                sb.table("agency_agencies").update({
                    **result,
                    "status": new_status,
                    "last_classified_at": datetime.now(timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }).eq("id", row["id"]).execute()
                classified += 1
                if new_status == "qualified":
                    qualified += 1
            except Exception as e:
                logger.error(f"classify_one failed for {row['id']}: {e}")
                sb.table("agency_agencies").update({"status": "classify_failed"}).eq("id", row["id"]).execute()
    return classified, qualified


def run_pipeline(skip_discovery: bool = False, country: str | None = None,
                 max_queries: int | None = None) -> dict:
    """Run the full pipeline end-to-end. Returns a summary dict."""
    summary = {"discovered": 0, "enriched": 0, "classified": 0, "qualified": 0}

    if not skip_discovery:
        logger.info("Phase 0/1: discovery")
        try:
            found = discover_google_search.discover(country=country, max_queries=max_queries)
            summary["discovered"] = len(found)
        except Exception as e:
            logger.error(f"Discovery failed: {e}")

    logger.info("Phase 1.5: dedup")
    try:
        dedup_canonicalize.run()
    except Exception as e:
        logger.warning(f"Dedup pass failed (non-fatal): {e}")

    logger.info("Phase 2: enrichment")
    summary["enriched"] = _enrich_phase()

    logger.info("Phase 3: classification")
    classified, qualified = _classify_phase()
    summary["classified"] = classified
    summary["qualified"] = qualified

    logger.info(f"Pipeline complete: {summary}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-discovery", action="store_true",
                        help="Skip Phase 0; only process existing rows")
    parser.add_argument("--country", help="Restrict discovery to one ISO alpha-2 code")
    parser.add_argument("--max-queries", type=int, help="Cap on discovery queries")
    parser.add_argument("--resume", action="store_true",
                        help="Alias for --skip-discovery (resume in-flight rows)")
    args = parser.parse_args()

    skip = args.skip_discovery or args.resume
    run_pipeline(skip_discovery=skip, country=args.country, max_queries=args.max_queries)


if __name__ == "__main__":
    main()
