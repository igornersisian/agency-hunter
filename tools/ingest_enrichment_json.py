"""
Phase 2c — ingest the JSON files produced by sub-agent enrichment workers
into Supabase.

Reads all `.tmp/enrich_json/{agency_id}.json` files, updates the
corresponding `agency_agencies` row with the same field projection as
[enrich_agency.py](enrich_agency.py) does, and flips status to
'enriched'. Also attaches the matching `raw_website_text` from
`.tmp/enrich_md/{agency_id}.md` so downstream classification has the
source text available.

Rows whose `.md` file exists but `.json` does not are left at
status='discovered' so they get retried on the next sub-agent wave.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from common.supabase_client import get_supabase

logger = logging.getLogger(__name__)

MD_DIR = Path(".tmp/enrich_md")
JSON_DIR = Path(".tmp/enrich_json")


def _apply(sb, agency_id: str, data: dict, raw_text: str) -> bool:
    update: dict = {
        "enriched_data": data,
        "raw_website_text": raw_text,
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

    try:
        sb.table("agency_agencies").update(update).eq("id", agency_id).execute()
        return True
    except Exception as e:
        logger.error(f"DB update failed for {agency_id}: {e}")
        return False


def run(dry_run: bool = False) -> dict:
    sb = get_supabase()
    if not JSON_DIR.exists():
        logger.warning(f"{JSON_DIR} does not exist — nothing to ingest")
        return {"ingested": 0, "skipped": 0, "errors": 0}

    json_files = sorted(JSON_DIR.glob("*.json"))
    logger.info(f"Found {len(json_files)} JSON files to ingest")

    ingested = 0
    skipped = 0
    errors = 0

    for jf in json_files:
        agency_id = jf.stem
        md_path = MD_DIR / f"{agency_id}.md"
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

        raw_text = md_path.read_text(encoding="utf-8") if md_path.exists() else ""

        if dry_run:
            logger.info(f"[dry] would ingest {agency_id}: name={data.get('name')}")
            skipped += 1
            continue

        if _apply(sb, agency_id, data, raw_text):
            ingested += 1
        else:
            errors += 1

        if (ingested + errors) % 50 == 0:
            logger.info(f"Progress: ingested={ingested} errors={errors}")

    logger.info(f"Done: ingested={ingested} skipped={skipped} errors={errors}")
    return {"ingested": ingested, "skipped": skipped, "errors": errors}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="Ingest sub-agent JSON files into Supabase.")
    ap.add_argument("--dry-run", action="store_true", help="List what would be ingested without writing")
    args = ap.parse_args()
    result = run(dry_run=args.dry_run)
    print(f"Ingested: {result['ingested']} (errors: {result['errors']})")
