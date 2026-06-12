# Workflow: Draft outreach

> **STATUS (since 2026-06-12): outreach is fully manual — AI does NOT
> generate messages.** The pipeline stops at classification. The Telegram
> `/review` command surfaces qualified agencies (`status IN ('qualified',
> 'no_contact')`, best fit first) as info cards: site link, fit breakdown,
> tools/services, any known emails. Igor researches the site, writes and
> sends the email himself from Gmail, then presses **✉️ Sent** (→
> `status='sent'`) or **⏭ Skip** (→ `'disqualified'`). Manual sends create
> no `agency_outreach_messages` row, so bot reply-tracking doesn't apply —
> Igor watches his own inbox.
>
> Everything below documents the LLM drafting path, which survives ONLY
> as an opt-in CLI (`python tools/draft_outreach.py`). It is not wired
> into `run_pipeline.py` or the bot; CLI-created `ready_to_send` drafts
> still surface in `/review` with Approve/Reject/Edit.

## Objective (legacy CLI path)
Produce a personalized cold-email draft for each qualified agency,
ready for Igor's Telegram review.

## The template contract (strict)
`templates/cold_v1.md` is Igor's hand-written email, stored verbatim.
The file is gitignored — `templates/cold_v1.example.md` is the public
placeholder; copy it to `cold_v1.md` and personalize.
The LLM ONLY generates two fields:
- `subject_line` (≤ 60 chars, no clickbait, no emoji, no fake `Re:`)
- `personalized_opener` (1-2 sentences referencing one specific concrete
  thing from `enriched_data`)

Everything from "I build production systems..." down to the soft
opt-out line at the bottom is substituted **byte-for-byte** from the
template. No rewrites, no "tone tweaks", no "cleanup" — Igor explicitly
said so.

**The full template is passed to the LLM as a READ-ONLY reference.**
This is safe because `_assemble_body()` does a pure
`tpl.replace("{personalized_opener}", opener)` — the body is always
loaded from disk and the LLM's output never replaces it. Showing the
template gives the LLM two practical advantages:
1. The opener flows naturally into the first body line ("I build
   production systems...") instead of hanging in a vacuum.
2. The LLM can avoid repeating facts already in the body (n8n,
   Supabase, the quick-examples list, the availability line, etc.).

## Hard rules
- **Concrete hook required.** The opener must reference one specific
  thing the agency actually said about themselves — a case study title,
  a named service, a listed tool, a team member's background. Never
  generic ("I love your work", "your agency is impressive").
- **No hallucination.** Only facts present in `enriched_data`.
- **No buzzwords.** No synergy, leverage, disrupt, revolutionize.
- **Skip rather than weaken.** If no concrete hook exists, the LLM
  returns `null` for both fields and the agency flips to
  `no_hook_skip`. Igor prefers silence to weak cold mail.
- **Match Igor's voice.** Direct, casual lowercase where natural,
  ends with a segue into his background.

## Compliance strings

1. **Soft opt-out line** lives verbatim at the bottom of
   `templates/cold_v1.md`. Since the LLM never outputs the body (only
   opener + subject) and `_assemble_body` is a pure string replace,
   the line ships byte-for-byte without any chance of mangling.

## Tool
`tools/draft_outreach.py`

Entry points:
- `draft_for_agency(agency_id)` — initial draft
- `regenerate(draft_id, feedback_text)` — Telegram `/edit` flow; increments
  `revision`, persists `edit_feedback`, overwrites subject/body/personalization
  on the same row

## Output row shape
`agency_outreach_messages`:
- `subject`, `body`, `template_id='cold_v1'`
- `personalization = {hook_type, hook_reference, personalized_opener}`
- `revision = 0` on initial, bumped by each `/edit` regeneration
- `status = 'ready_to_send'`

## Review card
The Telegram `/review` command renders: agency + country, fit_score,
bulleted pros and cons from `fit_breakdown`, draft subject, body
preview (first ~900 chars), and three inline buttons:
`[Approve] [Reject] [Edit]`. The Edit button prompts for free-text
feedback in the next message, then calls `regenerate()`.
