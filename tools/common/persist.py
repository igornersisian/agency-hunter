"""
Shared Supabase persist path for discovery channels.

Extracted from discover_google_search so every channel (SERP, partner
directories, future sources) writes rows through one code path:

  - insert-only guard on `agency_agencies` — an existing row is NEVER
    overwritten, even if it's already classified
  - always-append provenance row to `agency_sources`
  - httpx HTTP/2 recovery: after ~300-400 sequential inserts a long-lived
    client can die with LocalProtocolError (RECV_WINDOW_UPDATE in CLOSED);
    we rebuild the client and retry the failed candidate once

Candidate dict shape (see discover_google_search.CandidateRow):
    id, name, domain, website_url, country, short_description,
    source_channel, source_url, raw_payload,
    specialization (optional list — written on INSERT only; enrichment
    later overwrites the column, the durable channel signal lives in
    agency_sources.channel)
"""

from __future__ import annotations

import time
import logging
from datetime import datetime, timezone

import httpx

from .supabase_client import get_supabase, reset_supabase

logger = logging.getLogger(__name__)


def persist_candidates(candidates: list[dict]) -> tuple[int, int]:
    """Insert new agencies + append source rows.

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
        if c.get("specialization"):
            agency_row["specialization"] = c["specialization"]
        # Insert only if missing — never overwrite an already-classified row
        existing = client.table("agency_agencies").select("id").eq("id", c["id"]).limit(1).execute()
        if not existing.data:
            client.table("agency_agencies").insert(agency_row).execute()
            new_count += 1

        source_row = {
            "agency_id": c["id"],
            "channel": c["source_channel"],
            "source_url": c["source_url"],
            "raw_payload": c["raw_payload"],
        }
        client.table("agency_sources").insert(source_row).execute()
        source_count += 1

    # Local DNS intermittently fails (getaddrinfo) and HTTP/2 clients can
    # die mid-loop — retry each candidate with backoff, rebuilding the
    # client between attempts. A still-failing candidate after the last
    # attempt aborts the run (datasets are replayable via resume tools).
    backoffs = (0, 2, 5, 15)
    for c in candidates:
        for attempt, wait in enumerate(backoffs, 1):
            if wait:
                time.sleep(wait)
                sb = reset_supabase()
            try:
                _persist_one(sb, c)
                break
            except (httpx.HTTPError, httpx.LocalProtocolError) as e:
                logger.warning(f"httpx error on {c['id']} (attempt {attempt}/{len(backoffs)}): {e}")
                if attempt == len(backoffs):
                    raise

    return new_count, source_count


def record_discovery_run(channel: str, status: str, candidates_found: int,
                         new_agencies: int, error: str | None = None,
                         metadata: dict | None = None) -> None:
    try:
        get_supabase().table("agency_discovery_runs").insert({
            "channel": channel,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "status": status,
            "candidates_found": candidates_found,
            "new_agencies": new_agencies,
            "error_message": error,
            "metadata": metadata or {},
        }).execute()
    except Exception as e:
        logger.warning(f"Failed to record discovery run: {e}")
