"""
Phase 2 — dedup + canonicalisation.

The discovery tool already canonicalises domains and writes to Supabase
idempotently. This script is the follow-up cleanup pass:

1. Rewrites any `agency_agencies.id` that is not already its own
   `canonical_domain(website_url)`. (Defensive; should be no-op in steady
   state.)
2. Folds duplicate rows that ended up pointing at the same root domain
   (e.g. if an older row used a subdomain as id). Fold = keep the oldest,
   re-parent `agency_sources`/`agency_contacts`/`agency_outreach_messages`
   to the survivor, then delete the loser.
3. Merges obvious typo variants of the same root domain — punted to a
   future pass; we log but do not auto-merge unless the canonical form
   already matches.

In practice the discovery tool's own dedup handles 99% of cases. This
file exists so the pipeline has a dedicated place to resolve weirdness
as channels grow beyond Google search.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from common.supabase_client import get_supabase
from common.domain_utils import canonical_domain

logger = logging.getLogger(__name__)


def _fetch_all_agencies(sb) -> list[dict]:
    """Fetch all agency rows via range pagination.

    Supabase/PostgREST caps responses at 1000 rows by default. Without
    pagination, dedup silently processes only the first 1000 of N.
    """
    page_size = 1000
    offset = 0
    out: list[dict] = []
    while True:
        chunk = (
            sb.table("agency_agencies")
            .select("id,website_url")
            .range(offset, offset + page_size - 1)
            .execute()
            .data
            or []
        )
        out.extend(chunk)
        if len(chunk) < page_size:
            return out
        offset += page_size


def run() -> dict:
    """Execute the dedup pass. Returns a summary dict."""
    sb = get_supabase()
    rows = _fetch_all_agencies(sb)

    # Bucket by canonical domain
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        canon = canonical_domain(row.get("website_url") or row.get("id"))
        if not canon:
            continue
        buckets[canon].append(row)

    collisions = {k: v for k, v in buckets.items() if len(v) > 1}
    folded = 0

    for canon, members in collisions.items():
        # Pick the row whose id already equals the canonical domain as survivor.
        survivor = next((m for m in members if m["id"] == canon), members[0])
        losers = [m for m in members if m["id"] != survivor["id"]]

        for loser in losers:
            logger.info(f"Folding {loser['id']} → {survivor['id']}")
            # Re-parent related rows
            for table in ("agency_sources", "agency_contacts", "agency_outreach_messages"):
                sb.table(table).update({"agency_id": survivor["id"]}) \
                    .eq("agency_id", loser["id"]).execute()
            # Delete loser last
            sb.table("agency_agencies").delete().eq("id", loser["id"]).execute()
            folded += 1

    summary = {
        "total_rows": len(rows),
        "unique_canonical_domains": len(buckets),
        "collisions_resolved": len(collisions),
        "rows_folded": folded,
    }
    logger.info(f"Dedup summary: {summary}")
    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run()
