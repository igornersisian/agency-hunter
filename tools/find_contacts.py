"""
Phase 5 — contact discovery.

**Visible, scraped-only.** We trust exactly what the agency published
on their own site — email addresses the enrichment phase extracted
from `/`, `/about`, `/contact`, `/careers`, etc. into
`enriched_data.visible_emails`.

Rules:
    - If the agency lists `hello@domain` or `info@domain` in their
      footer, that IS their intended contact channel. We do NOT block
      role-addresses (Igor explicitly rejected this — a role address
      on a live site is a real contact point, not a blackhole).
    - The only hard filter is non-sendable system addresses:
      `noreply@`, `no-reply@`, `donotreply@`, `do-not-reply@`,
      `mailer-daemon@`.
    - **No pattern guessing.** We do NOT fabricate `{first}.{last}@`
      addresses from team member names. An MX record on the domain
      only proves the domain accepts mail — it says NOTHING about
      whether a specific local-part exists. Guessing at a human's
      email is hallucination; Igor wants silence over fiction.
    - If no usable visible emails exist, the agency flips to
      `no_contact` and is dropped from the draft phase.

No paid APIs. No Hunter.io. No Apollo.
"""

from __future__ import annotations

import re
import logging
from datetime import datetime, timezone

from common.supabase_client import get_supabase

logger = logging.getLogger(__name__)

# System addresses that literally cannot receive mail. Everything else
# (hello@, info@, contact@, sales@, ...) is allowed if the agency
# published it on their own site.
_NON_SENDABLE_LOCALPARTS = {
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster",
}

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _is_non_sendable(email: str) -> bool:
    local = email.split("@", 1)[0].lower()
    return local in _NON_SENDABLE_LOCALPARTS


def _existing_emails(sb, agency_id: str) -> set[str]:
    existing = sb.table("agency_contacts").select("email").eq("agency_id", agency_id).execute()
    return {row["email"].lower() for row in (existing.data or []) if row.get("email")}


def find_for_agency(agency_id: str, enriched: dict) -> int:
    """Discover + persist contacts for a single agency from scraped
    visible emails only. Returns the number of new contact rows inserted.
    """
    sb = get_supabase()
    already = _existing_emails(sb, agency_id)
    inserted = 0

    for email in enriched.get("visible_emails") or []:
        email_l = email.strip().lower()
        if not _EMAIL_RE.match(email_l) or email_l in already:
            continue
        if _is_non_sendable(email_l):
            logger.info(f"Skipping non-sendable address {email_l}")
            continue
        sb.table("agency_contacts").insert({
            "agency_id": agency_id,
            "email": email_l,
            "email_status": "scraped_visible",
            "email_confidence": 90,
            "source": "site_scrape",
            "is_primary": True,  # scraped emails are the only kind we trust
        }).execute()
        already.add(email_l)
        inserted += 1

    # Status transition: if we have ANY scraped email (new or already there),
    # the agency is contactable.
    new_status = "contact_found" if already else "no_contact"
    sb.table("agency_agencies").update({
        "status": new_status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", agency_id).execute()

    return inserted


def run_batch(limit: int = 20) -> int:
    """Find contacts for agencies in `status='qualified'`."""
    sb = get_supabase()
    rows = (
        sb.table("agency_agencies")
        .select("id,enriched_data")
        .eq("status", "qualified")
        .limit(limit)
        .execute()
        .data
        or []
    )
    total = 0
    for row in rows:
        try:
            total += find_for_agency(row["id"], row.get("enriched_data") or {})
        except Exception as e:
            logger.error(f"find_contacts failed for {row['id']}: {e}")
    return total


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    n = run_batch()
    print(f"Inserted {n} new contacts.")
