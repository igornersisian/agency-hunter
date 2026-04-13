# Workflow: Enrich agency

## Objective
Turn a freshly-discovered agency row into a structured
`enriched_data` JSONB blob by scraping its public site and extracting
signals via LLM.

## Inputs
- `agency_agencies` row in `status='discovered'`
- `OPENAI_API_KEY` (+ `OPENROUTER_API_KEY` as fallback)

## Steps
1. Try to fetch these paths in order, stop after 4 successful hits:
   `/`, `/about`, `/about-us`, `/services`, `/what-we-do`, `/work`,
   `/case-studies`, `/projects`, `/team`, `/contact`, `/careers`, `/jobs`.

   `/careers` and `/jobs` are included because many agencies expose
   their real contact email (and sometimes a live `mailto:` button)
   only on the hiring page — not in the main footer. `find_contacts`
   depends on these visible emails being captured here, since
   pattern-guessing was removed.
2. Each fetch goes through `common.http_fetch.fetch_clean_text`:
   - Primary: `r.jina.ai/{url}` → pre-cleaned markdown (handles JS render).
   - Fallback: direct `httpx.get` + `selectolax`, stripping
     `<script>/<style>/<nav>/<footer>/<header>/<aside>/<noscript>/<svg>/<form>`.
   - Output truncated to 15k chars.
3. **Nothing raw ever reaches the LLM.** Concatenate the cleaned chunks,
   truncate combined text to 30k chars.
4. Call `gpt-4.1-mini` in JSON mode with the strict extraction schema
   (services, tools, team, case studies, visible emails, red flag notes).
5. Persist to `enriched_data`; lift selected fields (name, country,
   city, team_size, specialization) to dedicated columns.
6. Flip the row to `status='enriched'`.

## Tool
`tools/enrich_agency.py`

## Rubric — what the extractor must capture
| Field | Source | Notes |
|---|---|---|
| `tools` | services, case studies, stack pages | Canonicalise to lowercase family names (n8n, make.com, zapier, openai, supabase, …) |
| `services` | services/what-we-do page | Short noun phrases, max 5 words |
| `team_members` | /team, /about | Only with full name. Skip generic "we are". |
| `visible_emails` | /contact, footers | Only literal addresses. No pattern guessing here — that's find_contacts. |
| `case_studies` | /work, /case-studies | Title required; URL/summary optional. |
| `red_flag_notes` | anywhere | Concrete concerns (stale copyright, one-person shop, enterprise-only language). |

## JS-heavy sites
r.jina.ai handles most JS-rendered pages. If a specific site returns
nothing useful, add it to a skip list — don't build a headless-browser
fallback for MVP.

## Failure handling
No content fetched → `status='enrich_failed'`. Re-run picks these up
only after manual status reset (to avoid retry loops on dead sites).
