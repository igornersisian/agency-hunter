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
MD_DIR = Path(".tmp/enrich_md")
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


def _fetch_all_discovered(sb) -> list[dict]:
    """Paginated fetch — PostgREST caps single SELECTs at 1000 rows."""
    page_size = 1000
    offset = 0
    out: list[dict] = []
    while True:
        chunk = (
            sb.table("agency_agencies")
            .select("id,name,country,city")
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


def run_from_md(limit: int | None = None) -> dict:
    """Sub-agent scoring straight from raw site text (no LLM enrichment).

    Builds classify inputs for `status='discovered'` rows whose markdown
    was fetched by fetch_markdown_batch.py into `.tmp/enrich_md/`.
    `enriched_data` is left empty — the rubric treats raw text as the
    primary evidence anyway. Skips rows already scored (output JSON
    exists) or already exported. Re-runnable while the fetch is still
    in progress."""
    sb = get_supabase()
    INPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    rows = _fetch_all_discovered(sb)
    logger.info(f"{len(rows)} discovered rows; checking {MD_DIR} for fetched markdown")

    written = no_md = already = 0
    written_ids: list[str] = []
    for r in rows:
        if limit and written >= limit:
            break
        md_path = MD_DIR / f"{r['id']}.md"
        if not md_path.exists() or md_path.stat().st_size == 0:
            no_md += 1
            continue
        if (OUTPUT_DIR / f"{r['id']}.json").exists() or (INPUT_DIR / f"{r['id']}.md").exists():
            already += 1
            continue
        raw = md_path.read_text(encoding="utf-8", errors="ignore")
        _write_input(r["id"], r.get("name"), r.get("country"), r.get("city"), {}, raw)
        written += 1
        written_ids.append(r["id"])

    logger.info(f"Wrote {written} inputs (no_md={no_md}, already_exported_or_scored={already})")
    return {"written": written, "no_md": no_md, "already": already, "rows": written_ids}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ap = argparse.ArgumentParser(description="Export agencies for sub-agent classification.")
    ap.add_argument("--limit", type=int, help="Max agencies to export")
    ap.add_argument("--from-md", action="store_true",
                    help="Build inputs from .tmp/enrich_md for discovered rows "
                         "(raw-text scoring, no enrichment step)")
    args = ap.parse_args()
    if args.from_md:
        res = run_from_md(limit=args.limit)
        print(f"Wrote: {res['written']} (no_md={res['no_md']}, already={res['already']})")
    else:
        if not args.limit:
            ap.error("--limit is required without --from-md")
        res = run(limit=args.limit)
        print(f"Wrote: {res['written']}")
