"""
Phase 2a — batch markdown fetch for the sub-agent enrichment pipeline.

Fetches cleaned markdown for all rows with `status='discovered'`, saves to
`.tmp/enrich_md/{agency_id}.md`. Does NOT touch the DB — status changes
happen at ingest time. Idempotent: skips any agency_id that already has
a non-empty `.md` file.

The sub-agent pipeline then reads these files, extracts JSON, and a
separate ingest script updates Supabase.
"""

from __future__ import annotations

import argparse
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from common.supabase_client import get_supabase
from enrich_agency import _fetch_pages

logger = logging.getLogger(__name__)

MD_DIR = Path(".tmp/enrich_md")


def _fetch_all_discovered(sb) -> list[dict]:
    """Paginated fetch of all rows with status='discovered'."""
    page_size = 1000
    offset = 0
    out: list[dict] = []
    while True:
        chunk = (
            sb.table("agency_agencies")
            .select("id,website_url")
            .eq("status", "discovered")
            .range(offset, offset + page_size - 1)
            .execute()
            .data
            or []
        )
        out.extend(chunk)
        if len(chunk) < page_size:
            return out
        offset += page_size


def _fetch_one(row: dict, backend: str) -> tuple[str, bool]:
    """Fetch markdown for one agency, save to file. Returns (id, success)."""
    agency_id = row["id"]
    out_path = MD_DIR / f"{agency_id}.md"
    if out_path.exists() and out_path.stat().st_size > 0:
        return agency_id, True
    try:
        text = _fetch_pages(row["website_url"], backend=backend)
        if not text.strip():
            return agency_id, False
        out_path.write_text(text, encoding="utf-8")
        return agency_id, True
    except Exception as e:
        logger.warning(f"Fetch failed for {agency_id}: {e}")
        return agency_id, False


def run(limit: int | None = None, workers: int = 10, fast: bool = False) -> dict:
    if fast:
        # Scoring needs the marketing core, not the full crawl: homepage +
        # about + services, 2 successful pages max. ~3-4x faster per agency
        # than the default 12-path / 4-fetch enrichment crawl.
        import enrich_agency
        enrich_agency._PATHS = ["/", "/about", "/services"]
        enrich_agency.MAX_FETCHES = 2
        logger.info("FAST mode: paths=/,/about,/services max_fetches=2")

    sb = get_supabase()
    rows = _fetch_all_discovered(sb)
    if limit:
        rows = rows[:limit]
    MD_DIR.mkdir(parents=True, exist_ok=True)

    backend = "crw" if os.environ.get("CRW_API_URL") else "jina"
    logger.info(f"Fetching markdown for {len(rows)} agencies via {backend}, {workers} workers")

    success = 0
    failed = 0
    done = 0
    failed_ids: list[str] = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, row, backend): row for row in rows}
        for future in as_completed(futures):
            agency_id, ok = future.result()
            done += 1
            if ok:
                success += 1
            else:
                failed += 1
                failed_ids.append(agency_id)
            if done % 50 == 0:
                logger.info(f"Progress: {done}/{len(rows)} (success={success} failed={failed})")

    logger.info(f"Done: {success} success, {failed} failed out of {len(rows)}")
    return {"success": success, "failed": failed, "total": len(rows), "failed_ids": failed_ids}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="Batch-fetch cleaned markdown for discovered agencies.")
    ap.add_argument("--limit", type=int, default=None, help="Cap the number of agencies to process")
    ap.add_argument("--workers", type=int, default=10, help="Parallel fetch threads (default: 10)")
    ap.add_argument("--fast", action="store_true",
                    help="Scoring mode: homepage+about+services only, 2 pages max")
    args = ap.parse_args()
    result = run(limit=args.limit, workers=args.workers, fast=args.fast)
    print(f"Fetched: {result['success']}/{result['total']} (failed: {result['failed']})")
