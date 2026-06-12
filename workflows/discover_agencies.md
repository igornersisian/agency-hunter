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

### Round-3 modes: `v2` and `local` (added 2026-06-12)

```bash
python tools/discover_google_search.py --mode v2                    # 142 queries, 1 Apify run
python tools/discover_google_search.py --mode local                 # 121 queries, 10 Apify runs
python tools/discover_google_search.py --mode local --language de   # one language only
```

- `v2` reads `v2_worldwide_templates` (26, 2025-26 vocabulary: ai agents,
  agentic, mcp, voice ai, claude) + `v2_country_templates` (4 × 29).
  Never re-runs the original template keys.
- `local` reads `local_groups` — local-language templates (de/es/pt/fr/
  nl/pl/sv/da/no/fi) for already-covered countries. The Apify actor takes
  ONE `languageCode` per run, so each language group is its own actor run
  (still one run per language, not per query). Failures are isolated per
  language: an error run-row is recorded and the loop continues.
- Both new modes use `maxPagesPerQuery=10` (they mix in unquoted
  templates that would otherwise paginate to Google's page-30-40
  "omitted results" wall and burn credits).
- Recovery: `python tools/resume_persist_dataset.py <dataset_id> --mode
  v2` or `--mode local --language de` (one dataset = one language).
- Cost: v2 ≈ 142 credits, local ≈ 121 credits across 10 runs —
  **~$2.50-4.00 total** including deeper pagination on unquoted queries.
- Console note on Windows: run with `PYTHONUTF8=1` (or
  `PYTHONIOENCODING=utf-8`) — local-language queries hit cp1251 encode
  errors in `--dry-run` printing otherwise.

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

## Channel: vendor partner directories (implemented 2026-06-12)

`tools/discover_partner_directories.py` — free direct scraping, no Apify.
Agencies in a vendor's partner directory are pre-qualified on tool
alignment; the channel name itself is the signal (`agency_sources.channel`).
`specialization=[vendor]` is also set on insert as a classifier hint, but
note enrichment later **overwrites** that column from site content — the
durable signal is the sources row.

```bash
python tools/discover_partner_directories.py --source n8n --dry-run --max 5
python tools/discover_partner_directories.py --source all   # cheap-first order
```

| source | entry point | how it works | volume (2026-06) |
|---|---|---|---|
| n8n | experts.n8n.io | PartnerPage SaaS, SSR; `?page=N`; website = `a[data-test-website-button]` on profile | 45 |
| airtable | ecosystem.airtable.com/consultants | same PartnerPage markup | 64 |
| zapier | zapier.com/partnerdirectory | same PartnerPage markup; profiles expose service *Regions* only, so country stays NULL | 448 |
| make | make.com/en/partners-directory | Cloudflare rejects httpx at TLS level → every request is an in-page `fetch()` in headless Chromium; unfiltered hidden API paginates ALL tiers (`tiers` filter needs repeated params, comma-joined = 400); website only in profile RSC payload (`RSC: 1` header) | 533 |
| webflow | webflow.com/certified-partners/browse | SSR; seeded pagination `?<seed>_page=N` — seed parsed from page-1 links (rotates on republish); pagination shows only neighbours → walk until a page adds 0 new profiles; website in embedded `"website":"…"` JSON | ~1,770 |

**Dropped (verified infeasible 2026-06-12):**
- **retool** — no public directory (`/agencies`, `/partners` are marketing
  pages; partners.retool.com is an internal login). Covered by the
  `"retool agency"` / `"retool developers"` v2 SERP templates.
- **bubble** — bubble.io/agencies renders an experts-directory of opaque
  Bubble-app divs: no profile links, no external websites; leads route
  through Bubble's internal Hire/Contact broker. Covered by the
  `"bubble development agency"` v2 SERP template.

Operational learnings:
- PartnerPage (n8n/zapier/airtable) shares identical markup — one generic
  scraper, config-driven.
- Multi-location profiles list HQ first → `_country_from_text` takes the
  earliest country match, not the longest.
- Local DNS intermittently fails (`getaddrinfo failed`) — `_get` retries
  once after 2s; the shared persist path resets the Supabase client on
  httpx errors (same HTTP/2 bug as Apify runs).
- Fully re-runnable for free: insert guard skips existing agencies,
  re-runs only append duplicate `agency_sources` rows (accepted).

## Other Phase 2 channels (still not built)
Clutch, Sortlist, GoodFirms, LinkedIn company search, GitHub organisation
search.
