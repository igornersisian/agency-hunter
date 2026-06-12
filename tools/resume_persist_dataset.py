"""
One-shot resume: re-download an Apify dataset and finish persisting.

Used after a discover run's Apify phase succeeded but the persist loop
died mid-way (see 2026-04-15 incident: httpx HTTP/2 RECV_WINDOW_UPDATE
in CLOSED state after ~380 sequential inserts on dataset G7mkP0kx4XvURngl8).

Apify datasets persist on Apify for 7 days, so we can replay them for
free. `_persist` is now resilient to connection-reset mid-loop, and the
`agency_agencies` insert guard prevents duplicates. `agency_sources`
WILL gain one extra row per candidate — that's acceptable traceback.

Usage:
    python tools/resume_persist_dataset.py <dataset_id> --mode worldwide
    python tools/resume_persist_dataset.py G7mkP0kx4XvURngl8 --mode worldwide
"""

from __future__ import annotations

import argparse
import logging

from dotenv import load_dotenv

from common.apify_runner import fetch_dataset
from discover_google_search import (
    _flatten_serp,
    _dedup_by_domain,
    _persist,
    _build_queries,
    _build_local_batches,
    _load_config,
    _record_run,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description="Resume persist from an Apify dataset")
    parser.add_argument("dataset_id", help="Apify dataset ID (e.g. G7mkP0kx4XvURngl8)")
    parser.add_argument(
        "--mode",
        choices=["countries", "worldwide", "cities", "v2", "local"],
        required=True,
        help="Query mode used for the original run (for country_code map)",
    )
    parser.add_argument("--country", help="Country filter used for the original run")
    parser.add_argument(
        "--language",
        help="local mode: language of the original run (one dataset = one language)",
    )
    args = parser.parse_args()

    cfg = _load_config()
    if args.mode == "local":
        if not args.language:
            parser.error("--mode local requires --language (one Apify run = one language)")
        batches = _build_local_batches(cfg, language_filter=args.language,
                                       country_filter=args.country)
        pairs = [p for _, batch in batches for p in batch]
    else:
        pairs = _build_queries(cfg, mode=args.mode, country_filter=args.country)
    query_country_map = {q: c for q, c in pairs}

    raw = fetch_dataset(args.dataset_id)
    candidates = _flatten_serp(raw, query_country_map)
    candidates = _dedup_by_domain(candidates)
    logger.info(f"Flattened → {len(candidates)} unique candidates after filter")

    new_count, total_sources = _persist(candidates)
    logger.info(f"Persisted: {new_count} new agencies, {total_sources} source rows")

    _record_run(
        "success",
        candidates_found=len(candidates),
        new_agencies=new_count,
        metadata={"resume": True, "dataset_id": args.dataset_id, "mode": args.mode},
    )


if __name__ == "__main__":
    main()
