"""
Phase 4c — ingest the JSON files produced by sub-agent classification
workers into Supabase.

Mirrors the structure built by [tools/classify_agency.py](classify_agency.py)
so downstream consumers (Telegram review card) see the same shape:
    fit_score      = clamped sum of sub-scores
    fit_reasoning  = the LLM's `fit_summary`
    fit_breakdown  = {pros, cons, sub_scores: {...}, total}
    flagged_issues = the cons list

Country blacklist (`{"IN"}`) is applied here so we don't waste a sub-agent
slot on guaranteed rejections — same rule as
[classify_agency.py:65-67](classify_agency.py#L65-L67).
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from common.supabase_client import get_supabase

logger = logging.getLogger(__name__)

OUTPUT_DIR = Path(".tmp/classify_output")
THRESHOLD_DEFAULT = 70
COUNTRY_BLACKLIST = {"IN"}

CAPS = {
    "tool_alignment": 40,
    "service_match":  30,
    "market_fit":     15,
    "engagement_fit": 15,
}


def _clamp(val, lo: int, hi: int) -> int:
    try:
        v = int(val)
    except (TypeError, ValueError):
        v = 0
    return max(lo, min(hi, v))


def _build_update(data: dict) -> dict:
    pros = [str(p) for p in (data.get("pros") or []) if p]
    cons = [str(c) for c in (data.get("cons") or []) if c]
    fit_summary = str(data.get("fit_summary") or "")

    tool_alignment = _clamp(data.get("tool_alignment"), 0, CAPS["tool_alignment"])
    service_match  = _clamp(data.get("service_match"),  0, CAPS["service_match"])
    market_fit     = _clamp(data.get("market_fit"),     0, CAPS["market_fit"])
    engagement_fit = _clamp(data.get("engagement_fit"), 0, CAPS["engagement_fit"])

    total = min(max(tool_alignment + service_match + market_fit + engagement_fit, 0), 100)

    return {
        "fit_score": total,
        "fit_reasoning": fit_summary,
        "fit_breakdown": {
            "pros": pros,
            "cons": cons,
            "sub_scores": {
                "tool_alignment": tool_alignment,
                "service_match":  service_match,
                "market_fit":     market_fit,
                "engagement_fit": engagement_fit,
            },
            "total": total,
        },
        "flagged_issues": cons,
    }


def run(threshold: int = THRESHOLD_DEFAULT, dry_run: bool = False) -> dict:
    sb = get_supabase()
    if not OUTPUT_DIR.exists():
        logger.warning(f"{OUTPUT_DIR} does not exist — nothing to ingest")
        return {"ingested": 0, "qualified": 0, "rejected": 0, "errors": 0, "skipped": 0}

    files = sorted(OUTPUT_DIR.glob("*.json"))
    logger.info(f"Found {len(files)} classification JSONs (threshold={threshold})")

    ingested = qualified = rejected = errors = skipped = 0

    for jf in files:
        agency_id = jf.stem
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Bad JSON for {agency_id}: {e}")
            errors += 1
            continue
        if not isinstance(data, dict):
            logger.warning(f"JSON for {agency_id} is not an object")
            errors += 1
            continue

        # Look up the row to get country (for blacklist check) and to
        # confirm the agency exists.
        try:
            row = (
                sb.table("agency_agencies")
                .select("id,country,status")
                .eq("id", agency_id)
                .single()
                .execute()
                .data
            )
        except Exception as e:
            logger.warning(f"Lookup failed for {agency_id}: {e}")
            errors += 1
            continue

        country = (row or {}).get("country")
        now = datetime.now(timezone.utc).isoformat()

        if country and country.upper() in COUNTRY_BLACKLIST:
            update = {
                "status": "rejected",
                "fit_score": 0,
                "fit_reasoning": f"Auto-rejected: country {country} is blacklisted.",
                "fit_breakdown": {"auto_reject": "country_blacklist", "country": country},
                "flagged_issues": [],
                "last_classified_at": now,
                "updated_at": now,
            }
        else:
            update = _build_update(data)
            new_status = "qualified" if update["fit_score"] >= threshold else "rejected"
            update["status"] = new_status
            update["last_classified_at"] = now
            update["updated_at"] = now

        if dry_run:
            logger.info(f"[dry] {agency_id}: score={update.get('fit_score')} -> {update['status']}")
            skipped += 1
            continue

        try:
            sb.table("agency_agencies").update(update).eq("id", agency_id).execute()
            ingested += 1
            if update["status"] == "qualified":
                qualified += 1
            else:
                rejected += 1
        except Exception as e:
            logger.error(f"DB update failed for {agency_id}: {e}")
            errors += 1

        if (ingested + errors) % 50 == 0:
            logger.info(f"Progress: ingested={ingested} (qualified={qualified}, rejected={rejected}) errors={errors}")

    logger.info(f"Done: ingested={ingested} (qualified={qualified}, rejected={rejected}) skipped={skipped} errors={errors}")
    return {
        "ingested": ingested,
        "qualified": qualified,
        "rejected": rejected,
        "errors": errors,
        "skipped": skipped,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="Ingest sub-agent classification JSONs into Supabase.")
    ap.add_argument("--threshold", type=int, default=THRESHOLD_DEFAULT, help=f"Qualified cutoff (default: {THRESHOLD_DEFAULT})")
    ap.add_argument("--dry-run", action="store_true", help="List what would be ingested without writing")
    args = ap.parse_args()
    res = run(threshold=args.threshold, dry_run=args.dry_run)
    print(f"Ingested: {res['ingested']} qualified={res['qualified']} rejected={res['rejected']} errors={res['errors']}")
