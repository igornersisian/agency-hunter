"""
Phase 4 prep — export enriched agencies to per-agency markdown files for
sub-agent classification (Claude Max via Task tool).

For each agency in `status='enriched'` (and not country-blacklisted at
preview time), writes `.tmp/classify_input/{agency_id}.md` containing:
  - basic facts (name, country, city)
  - the structured `enriched_data` JSON
  - the first 12000 chars of `raw_website_text` (the LLM's primary
    evidence source — see [tools/classify_agency.py:271-282])

Sub-agents then write their classification verdict to
`.tmp/classify_output/{agency_id}.json`, which
[tools/ingest_classification_json.py](ingest_classification_json.py)
later folds back into Supabase.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from common.supabase_client import get_supabase

logger = logging.getLogger(__name__)

INPUT_DIR = Path(".tmp/classify_input")
OUTPUT_DIR = Path(".tmp/classify_output")
RAW_TEXT_CAP = 12000


def _write_input(agency_id: str, name: str | None, country: str | None,
                 city: str | None, enriched_data: dict, raw_text: str) -> None:
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    body = (
        f"# Agency: {agency_id}\n\n"
        f"- name: {name or ''}\n"
        f"- country: {country or ''}\n"
        f"- city: {city or ''}\n\n"
        f"## Enriched data (structured extract)\n\n"
        f"```json\n{json.dumps(enriched_data or {}, ensure_ascii=False, indent=2)}\n```\n\n"
        f"## Raw website text (first {RAW_TEXT_CAP} chars)\n\n"
        f"{(raw_text or '')[:RAW_TEXT_CAP]}\n"
    )
    (INPUT_DIR / f"{agency_id}.md").write_text(body, encoding="utf-8")


def run(limit: int) -> dict:
    sb = get_supabase()
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    rows = (
        sb.table("agency_agencies")
        .select("id,name,country,city,enriched_data,raw_website_text")
        .eq("status", "enriched")
        .limit(limit)
        .execute()
        .data
        or []
    )
    logger.info(f"Exporting {len(rows)} enriched agencies to {INPUT_DIR}")

    written = 0
    for r in rows:
        _write_input(
            r["id"], r.get("name"), r.get("country"), r.get("city"),
            r.get("enriched_data") or {}, r.get("raw_website_text") or "",
        )
        written += 1

    logger.info(f"Wrote {written} input files")
    return {"written": written, "rows": [r["id"] for r in rows]}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="Export enriched agencies for sub-agent classification.")
    ap.add_argument("--limit", type=int, required=True, help="Max agencies to export")
    args = ap.parse_args()
    res = run(limit=args.limit)
    print(f"Wrote: {res['written']}")
