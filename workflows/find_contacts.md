# Workflow: Find contacts

## Objective
Discover at least one human contact per qualified agency by using ONLY
the email addresses the agency has published on their own site. No
paid APIs. No pattern guessing. No fabrication.

## Inputs
- `agency_agencies` row in `status='qualified'`
- Its `enriched_data.visible_emails` (populated by Phase 3 enrichment)

## The one hard rule
**Scraped-visible only.** If the agency lists `hello@`, `info@`,
`contact@`, etc. in their footer or on `/contact`/`/careers`, that IS
their intended contact channel — it was put there by humans so people
could reach them. We do NOT block role addresses.

If the agency published zero usable emails, the row flips to
`no_contact` and is dropped from the draft phase. **Silence over
fiction.**

## What counts as non-sendable
Only system addresses that literally cannot receive mail:
- `noreply@`, `no-reply@`, `donotreply@`, `do-not-reply@`
- `mailer-daemon@`, `postmaster@`

Everything else (`hello@`, `info@`, `sales@`, `careers@`, `contact@`,
named humans, whatever) is accepted at confidence 90.

## Why no pattern guessing
Earlier versions of this workflow generated `{first}.{last}@domain`
addresses from `team_members` names and MX-verified the domain. This
was removed 2026-04 because:

1. **It is hallucination.** MX-verifying `spruik.co` only proves the
   domain accepts mail. It proves NOTHING about whether
   `rye.smith@spruik.co` is a real mailbox. The confidence score of 55
   was fiction.
2. **Igor explicitly rejected it.** He would rather skip an agency
   than send a drafted email to a fabricated address. Bounces hurt
   sender reputation, wrong-person delivery is worse (catch-all
   domains route to random inboxes).
3. **Real contact emails are usually on the site.** The /careers,
   /contact, and footer scrapes (now including `/careers` and `/jobs`
   in `enrich_agency._PATHS`) catch the overwhelming majority of
   legitimate contact points. If the site doesn't publish one, that's
   a signal — not a problem to "solve" by inventing one.

Pattern guessing is NOT an opt-in flag. Git history preserves the old
code if the calculus ever changes.

## Steps
1. Read `enriched_data.visible_emails` from the agency row.
2. For each email: lowercase, regex-validate, drop if already in
   `agency_contacts` for this agency, drop if in the non-sendable list.
3. Insert survivors into `agency_contacts` with:
   - `email_status='scraped_visible'`
   - `email_confidence=90`
   - `source='site_scrape'`
   - `is_primary=True` (all scraped emails are treated as primary —
     the draft phase picks the first one deterministically)
4. If any contact now exists for the agency → status `contact_found`.
   Otherwise → `no_contact`.

## Tool
`tools/find_contacts.py`

## What changed from MVP v1
- **Removed** `_ROLE_LOCALPARTS` blacklist that blocked `hello@`,
  `info@`, etc. from scraped emails. Prior logic wrongly skipped
  `hello@spruik.co` (the real published contact) and then fabricated
  four `{first}.{last}@spruik.co` addresses.
- **Removed** `_generate_guesses()`, `_mx_exists()`, the entire pattern
  guess code path, and the `dnspython` dependency for this file.
- **Added** `is_primary=True` to scraped rows so `_pick_primary_contact`
  has a deterministic winner.

## Future upgrades (phase 2, not MVP)
- Expand /careers scraping to follow `mailto:` buttons that may not
  render as plain text (requires a JS-aware fetcher).
- Role-based priority when multiple scraped emails exist (prefer
  `hello@` > `contact@` > `sales@` > `careers@` for outreach). For now
  the first scraped email wins.
- SMTP RCPT probe to verify specific mailboxes (free but can damage
  sender reputation — risky).
