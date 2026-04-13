# Workflow: Discover agencies

## Objective
Populate `agency_agencies` with new `status='discovered'` rows by running
Google search queries through Apify.

## Inputs
- `tools/config/serp_queries.json` — query templates + target countries
- `APIFY_API_TOKEN` in `.env`

## Steps
1. Read the query config from `tools/config/serp_queries.json`.
2. Expand templates against enabled countries → list of (query, country).
3. Submit the batch to `apify/google-search-scraper` via `common.apify_runner.run_and_collect`.
4. Flatten organic results; drop directory/platform domains via
   `common.domain_utils.is_directory_domain`.
5. Canonicalise each URL to its root domain; in-batch dedup.
6. Upsert into `agency_agencies` (insert-only — never overwrite a classified row).
7. Always append a row to `agency_sources` for provenance.
8. Log a row in `agency_discovery_runs`.

## Tool
`tools/discover_google_search.py`

```bash
python tools/discover_google_search.py                      # all countries
python tools/discover_google_search.py --country NZ         # one country
python tools/discover_google_search.py --dry-run            # don't write
python tools/discover_google_search.py --country NZ --max 5 # smoke test
```

## Schedule
**On-demand only.** Same queries on the same day return the same SERP —
a cron would just burn Apify credits. Trigger a run manually via the
Telegram `/fetch` command after adding new countries or query templates
to the config.

## Cost notes
- Each query ≈ 1 Apify "pageload" credit. Current config: 10 templates
  × 29 countries = **290 credits per full run** (~$1.45 on the
  pay-per-result plan).
- Stay within the existing paid Apify quota — no other paid APIs.
- **Paid-run recovery**: if the script crashes *after* the Apify run has
  started, DO NOT re-run `discover_google_search.py` — that double-bills.
  Use `.tmp/recover_apify_run.py <run_id>` which reattaches to the
  existing run, caches the dataset to `.tmp/candidates.json`, and
  resumably persists row-by-row with a `.tmp/persisted.txt` progress
  file. Safe to re-invoke any number of times.

## Troubleshooting
- **Zero candidates returned**: check that `apify/google-search-scraper`
  is still the correct actor ID; Apify occasionally deprecates actors.
- **All results filtered**: the `is_directory_domain` blocklist may be
  too aggressive. Inspect the raw dataset in the Apify console.
- **Rate limit from Apify**: batch all queries into a single actor run
  (already the default) instead of one run per query.
- **DNS blip during long Apify poll**: `wait_for_run` now retries 5×
  with exponential backoff (5→15→30→60→120s). A hard failure after
  exhausting retries leaves the run ID in the error message so you can
  recover via `.tmp/recover_apify_run.py`.
- **HTTP/2 LocalProtocolError (`RECV_WINDOW_UPDATE in state CLOSED`)**:
  known httpx bug on long-lived Supabase clients doing hundreds of
  sequential requests. The recovery script handles this by rebuilding
  the client between retries. If `_persist` in the main script hits it,
  resume via the recovery path.

## Run history & learnings

### 2026-04-12 — first real multi-country run
First full 29-country × 10-template run. Apify run `gycxe6yphhmcLtOJ6`,
dataset `6hIbsDcGwsG3cTGHJ`.

**Numbers**:
- 290 SERP pages → 2,501 raw organic hits → 1,500-ish after directory
  filter → **763 unique domains** → **732 inserted** as new agencies
  (31 duplicate domains were same agency found via a different country's
  query; first-country-wins per in-batch dedup).
- Even split across 29 countries (range 4–57 per country).

**Key finding — SERP depth, not width, is the bottleneck**:
Google returned an **average of 8.6 organic results per query** (max 11,
min 0, with 29/290 queries completely empty). We had requested 50.
Cause: the templates use exact-match quotes (`"ai automation agency"
"Luxembourg"`), which Google interprets as a hard AND-of-phrases. For
small countries there are often fewer than 10 pages on the entire web
where both phrases appear verbatim. The realistic ceiling for the
current config is ~2.5k raw hits, not 14.5k.

**If you want thousands of agencies**, in order of ROI:
1. **Broaden templates** — drop or loosen the quotes on 2-3 templates,
   add 10-15 more variations and local-language keys (`KI Beratung` for
   DE/AT/CH, `automatisation IA` for FR-speaking Belgium/Luxembourg).
2. **Pagination**: bump `maxPagesPerQuery` to 2 in `_run_apify` — only
   helps for queries where Google has more than 10 results (not the
   tiny-country ones).
3. **Relax first-country-wins dedup**: currently a domain keeps only
   its first-seen country. Allow one row per (domain, country) if you
   want per-country coverage of multi-national agencies.

**Do not "fix" the number by removing the directory filter** — the
filtered domains (clutch.co, linkedin.com, medium.com...) are listings,
not agencies-for-hire, and would poison enrichment.

**Review noise to watch for**: SERP also surfaced non-agencies that
passed the blocklist — `apple.com`, `bcg.com`, `pwc.com`,
`entrepreneur.com`, `udemy.com`, `glean.com`, `replit.com`. These need
either additions to `_NON_AGENCY_DOMAINS` or trust in the downstream
classifier to drop them. Prefer the classifier unless a specific
non-agency keeps reappearing.

## Phase 2 channels (not in MVP)
Vendor partner directories (n8n, Make, Zapier, Airtable, Bubble, Webflow,
Retool), Clutch, Sortlist, GoodFirms, LinkedIn company search, GitHub
organisation search. Build these only after the MVP pipeline proves its
signal-to-noise ratio on Google search alone.
