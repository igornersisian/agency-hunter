# Workflow: Classify agency

## Objective
Score whether Igor — a solo REMOTE contractor, no-code/AI-assisted
builder — could realistically get paid contract work from this agency.
Chain-of-thought pros/cons ground every score.

## Inputs
- `agency_agencies` row in `status='enriched'` (`enriched_data` populated)
- Shared profile row (Igor's parsed resume + `agency_target_countries`)

## Who Igor is (for the classifier)
- Solo remote contractor, "vibecoder" — builds with n8n, make.com,
  zapier, OpenAI/Anthropic/Claude, Supabase, WeWeb, Webflow, Retool,
  Bubble, Airtable, LangChain, Claude Code, Replit.
- Ships: multi-agent AI, RAG systems, workflow automation, full-stack
  no-code web apps, self-hosted infra.
- **Not** a hand-written-code engineer. Does not compete for C++/Java/
  Rust/native mobile/enterprise backend work.

## Steps (chain-of-thought enforced)
1. **Pros first** — concrete reasons Igor fits, each referencing specific
   evidence from `enriched_data` (a service name, a tool mention, a case
   study title, a location statement).
2. **Cons second** — **HARD DISQUALIFIERS ONLY**, with specific evidence.
   If none apply, cons is an empty list. No padding with soft concerns.
3. **fit_summary** — 1-2 sentence synthesis, addressed to Igor.
4. **Sub-scores last** — the single source of truth for the final score.

Sub-score dimensions and caps (sum to 100, no penalties):
| Dimension | Max | Meaning |
|---|---|---|
| tool_alignment | 40 | Overlap with Igor's n8n/make/zapier/openai/anthropic/supabase/weweb stack. 10 per clear match. |
| service_match  | 30 | Services map to what Igor has shipped (RAG, multi-agent, automation, no-code full-stack) |
| market_fit     | 15 | Agency in one of Igor's target countries |
| engagement_fit | 15 | **Default 15 — assume remote-friendly.** Drop to 0 ONLY on explicit offline-only language |

## What counts as a con (HARD DISQUALIFIERS)
Only these, and only with specific evidence:
1. **Stack mismatch** — agency builds on hand-written code (C++/Java/
   Rust/native iOS/Android/enterprise .NET) with traditional SDLC.
2. **Not an AI/automation agency** — pure design shop, SEO-only, PR,
   staffing/recruiting, cybersecurity, pure devops.
3. **Enterprise procurement only** — explicit Fortune 500 language,
   RFP-driven, named F500 logos only.
4. **Explicitly offline-only** — agency literally states on-site work,
   "come to our studio", "in-person collaboration required". **Absence
   of remote-friendly language is NOT a con** — remote is the default
   assumption.
5. **Dead / abandoned** — last case study or blog post from 2021 or
   earlier, "coming soon" placeholder, broken site.
6. **Off-target geography** — primary market outside Igor's countries
   AND site is region-locked (non-English).

## What is NOT a con (banned from the cons list)
These are red herrings. The previous rubric wrongly penalized agencies
for them and rejected real matches:
- **Team size** of any kind — 5-person or 200-person are both fine.
  Igor targets project-level overflow, not headcount. Prior rubric
  wrongly docked scale_fit for 51-200 teams; fixed 2026-04.
- **Missing case studies** — many agencies keep work under NDA.
- **Missing LinkedIn profiles** — privacy preference.
- **Missing founded year, missing founder bios.**
- **Generic corporate marketing language** — everyone has it.
- **Absence of "we hire freelancers" language** — most agencies never
  say it even when they do subcontract. Look for OFFLINE-ONLY signals,
  not the absence of remote-friendly signals.

## Scoring math
```
total = tool_alignment + service_match + market_fit + engagement_fit
      = clamp [0, 100]
```
**No server-side penalty from cons.** The LLM already reflects cons in
the sub-scores (stack mismatch → tool_alignment=0, offline-only →
engagement_fit=0). Double-penalty was removed after we observed it
crushed borderline-good agencies like Spruik (50-person AU n8n shop
with 4 pros → rejected with score 30).

## Threshold
Default 70 (configurable via `/threshold` in Telegram, persisted to
`profile.parsed.agency_fit_threshold`).

## Tool
`tools/classify_agency.py`

## Review output
Pros, cons, and fit_summary are persisted to `fit_breakdown` so the
Telegram review card can render them as bullet lists above the email
preview. This is how Igor sees *why* the classifier approved a lead.

## Tuning
Run on 20 hand-picked known-good and known-bad agencies, adjust the
prompt until ~70% of `qualified` rows pass Igor's gut check. Pros/cons
output must be concrete, not generic. If agencies get cons that are
NOT on the hard-disqualifiers list, tighten the prompt's "NOT CONS"
section.
