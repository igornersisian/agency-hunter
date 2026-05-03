# agency-hunter

An autonomous outreach pipeline that finds, qualifies, and contacts
AI-automation agencies — end to end — from a single Telegram bot.

I built this to run my own contract job hunt without burning hours a day
in spreadsheets. It does what a full-time SDR would: discovers thousands
of agencies, throws out the bad fits with an LLM scorer, drafts
personalized cold-emails grounded in each agency's actual website, and
queues them for one-tap approval in Telegram.

Currently used in production for my hunt — ~3,400 agencies discovered
across 29 countries, ~360 qualified after classification.

---

## What it actually does

- **Discovers** agencies via Google SERP scraping (29 countries × narrow query templates)
- **Enriches** each one by crawling their site (services, case studies, stack, team) — uses a self-hosted Cloudflare-bypass scraper before falling back to ScraperAPI
- **Classifies** stack/service fit with `gpt-5-mini` against my resume profile (cheap enough to score thousands of rows)
- **Finds contacts** — extracts personal emails from the site and verifies via DNS. Never role addresses (`info@`, `hello@`); skips rather than guesses
- **Drafts cold emails** that reference one specific concrete thing about the agency (a case study, a tool, a service). The LLM only writes the opener; the body is a strict template that ships byte-for-byte
- **Reviews in Telegram** — fit score, pros/cons, draft preview, `[Approve] [Reject] [Edit]` inline buttons
- **Sends** via Gmail SMTP with a respect-the-recipient send window, daily caps, 60-day duplicate guard, and a CAN-SPAM footer

## Why the architecture matters (WAT)

Three layers, so probabilistic AI handles reasoning and deterministic
Python handles execution:

- **Workflows** (`workflows/*.md`) — plain-language SOPs. Objective, inputs, tools, edge cases for each phase.
- **Agent** (Claude Code) — reads the workflow, picks the right tool, recovers from errors, asks when ambiguous.
- **Tools** (`tools/*.py`) — deterministic Python. API calls, transformations, DB writes.

If each LLM step is 90% reliable, five chained steps land at 59%. Keeping
execution out of the LLM and only using it for narrow decisions
(scoring, opener generation) is what lets this survive at scale.

## Pipeline

```
discover → fetch markdown → enrich → classify → find contacts → draft → review → send
   │            │              │         │            │            │        │       │
   SERP       site HTML       LLM     gpt-4.1      regex +      strict   human   Gmail
  scrape    (CRW / SAPI)    extract   -mini       MX check    template   approve  SMTP
```

Each stage is idempotent and writes status back to Postgres, so the bot
can be restarted at any phase without losing or duplicating work.

## Quick start

1. **Clone + install**
   ```bash
   git clone https://github.com/igornersisian/agency-hunter
   cd agency-hunter
   pip install -r requirements.txt
   ```

2. **Database** — Supabase project, then run the migrations:
   ```bash
   psql $DATABASE_URL < migrations/001_init_agency.sql
   psql $DATABASE_URL < migrations/002_scheduler.sql
   ```

3. **Env** — copy `.env.example` → `.env` and fill in:
   - Supabase URL + service role key
   - OpenAI key (used for classification, opener drafting, enrichment extraction)
   - Telegram bot token + your chat id
   - Gmail address + App Password (or OAuth credentials base64-encoded for server deploy)
   - ScraperAPI / Apify keys (optional — page fetching and SERP scraping)
   - CRW URL (optional — self-hosted Firecrawl-compatible scraper)

4. **Outreach template** — copy the example and rewrite it in your own voice:
   ```bash
   cp templates/cold_v1.example.md templates/cold_v1.md
   ```
   The LLM never edits this file; it ships byte-for-byte.

5. **Profile** — your resume parsed into the shared `profile` table (stack, target countries, fit threshold, sender details). See `tools/common/profile.py` for the row shape.

6. **Run the bot**
   ```bash
   python tools/telegram_bot.py
   ```
   Then talk to it: `/discover`, `/enrich`, `/classify`, `/draft`, `/review`, `/send`.

## Layout

| Path | What's there |
|------|---|
| `tools/` | Python tools — one per pipeline phase, plus the Telegram bot |
| `tools/common/` | Shared utilities (Supabase client, LLM wrapper, send-window policy, domain helpers) |
| `tools/config/` | Static config — SERP query templates, target countries |
| `workflows/` | Markdown SOPs the agent reads to execute each phase |
| `templates/` | Outreach templates (the real `cold_v1.md` is gitignored; only the example is tracked) |
| `migrations/` | Postgres schema — agencies, outreach messages, scheduler |
| `Dockerfile` | Production container (deployed to Dokploy) |

## Stack

Python 3.12 · Supabase (Postgres) · OpenAI / OpenRouter · Apify (Google SERP) · ScraperAPI + self-hosted CRW (page fetching) · Gmail SMTP · python-telegram-bot

## Notes

- **No paid API costs beyond what's already running.** Classification uses `gpt-5-mini` on OpenAI's flex tier; enrichment uses self-hosted CRW first, ScraperAPI as fallback.
- **Compliance.** Soft opt-out line lives verbatim in the template; the CAN-SPAM physical-address footer is appended at send time from an env var (per-sender). Both ship on every email.
- **No silent decisions.** The agent will ask before reducing scope, skipping work, or anything else that affects results — this is enforced via `CLAUDE.md`.
- **Sibling project** — the `profile` table is shared with my [job-search-automation](https://github.com/igornersisian) repo. One resume powers both pipelines.
