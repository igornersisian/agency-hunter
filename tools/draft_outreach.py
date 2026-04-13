"""
Phase 6 — outreach drafting.

Loads `templates/cold_v1.md` and the agency's `enriched_data`, then calls
OpenAI in JSON mode to produce ONLY:
    {subject_line, personalized_opener, hook_type, hook_reference}

The final email body is assembled by substituting the opener into the
template verbatim — **no other part of the template is touched**, not
even whitespace. Igor explicitly wants the template shipped byte-for-byte
below the opener.

The LLM contract requires the opener to reference one concrete thing the
agency said about themselves (a service name, a case study title, a tool
they mention using). If no such concrete hook exists, the tool returns
None and flips the agency to `no_hook_skip` instead of drafting a weak
email.

Regeneration:
    regenerate(draft_id, feedback_text) re-runs drafting with the prior
    opener and Igor's free-text feedback in the prompt, overwrites the
    row, increments `revision`, and persists `edit_feedback`.
    Used by the Telegram /edit flow.

The full template is passed to the LLM as a READ-ONLY reference so the
opener can flow naturally into the body's first line and avoid repeating
facts already stated below. The LLM NEVER outputs the body — body is
always assembled from disk via a pure `.replace()` in `_assemble_body`.

The CAN-SPAM physical address footer is appended at send time in
`send_email_gmail.py` (env-var driven, per-sender). The soft opt-out
line lives verbatim inside `templates/cold_v1.md`.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from common.supabase_client import get_supabase
from common.llm import chat_completion
from common.profile import get_profile

logger = logging.getLogger(__name__)

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "templates" / "cold_v1.md"
TEMPLATE_ID = "cold_v1"

_HOOK_TYPES = {"case_study", "service", "blog", "tool_match", "other"}


def _load_template() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def _assemble_body(opener: str) -> str:
    """Substitute ONLY `{personalized_opener}` — everything else verbatim."""
    tpl = _load_template()
    return tpl.replace("{personalized_opener}", opener.strip())


def _system_prompt(profile_text: str, template_text: str) -> str:
    return (
        "You write the OPENING ADDRESS of a cold email from Igor to one "
        "specific AI-automation agency. Your opener is addressed TO the "
        "agency and is ONLY about the agency — one concrete thing they "
        "said about themselves on their own site.\n\n"

        "Everything below your opener is a fixed template (shown to you "
        "in full further down). It is the second part of the same email "
        "and ships verbatim. Your opener is the FIRST part; the template "
        "is the CONTINUATION. Read the template carefully — your opener "
        "MUST NOT repeat anything that already appears in it.\n\n"

        "HARD RULES:\n"
        "- HOOK SELECTION is the most important decision you make. "
        "Before writing a single word, pick the hook that will anchor "
        "the opener using this STRICT priority order. Always pick the "
        "highest-priority hook that exists in enriched_data — do not "
        "skip a higher-priority hook just because a lower one feels "
        "more colourful.\n"
        "    1. tool_match — the agency lists a specific tool in "
        "enriched_data.tools (or mentions it in services/case "
        "studies) that ALSO appears in IGOR'S PROFILE below. This is "
        "the STRONGEST hook because it's a real skill match. "
        "Example: agency lists n8n, Igor's profile lists n8n → n8n "
        "tool_match. Use this whenever it exists.\n"
        "    2. service_match — a named TECHNICAL service the agency "
        "offers that matches what Igor actually builds (workflow "
        "automation, RAG, multi-agent AI, no-code web apps, Supabase "
        "backends, AI integrations). Must be a GENERAL technical "
        "service, NOT a vertical/industry specialisation.\n"
        "    3. case_study — ONLY ALLOWED when the case study is "
        "framed around a STACK or TECHNICAL approach that Igor "
        "actually knows (\"we built an n8n workflow for X\", \"our "
        "Supabase RAG implementation\"). STRICTLY FORBIDDEN when the "
        "case study is framed around a vertical, industry, or "
        "client-type (\"law firm document processing\", \"e-commerce "
        "customer service\", \"manufacturing real-time visibility\", "
        "\"healthcare patient intake\"). Igor has NO vertical "
        "experience in legal, healthcare, finance, e-commerce, or "
        "manufacturing — anchoring on such a case study would "
        "misrepresent him as an industry specialist he is not.\n"
        "    4. blog — a technical blog post whose topic is in "
        "Igor's stack.\n"
        "    5. other — last resort only.\n"
        "  If the agency has n8n in their tools AND a legal case "
        "study, pick the n8n tool_match — NEVER the legal case "
        "study.\n"
        "- CREDIBILITY CHECK (INVIOLABLE). Before finalising, verify "
        "the hook represents Igor HONESTLY against IGOR'S PROFILE "
        "below:\n"
        "    • tool_match: the tool must appear in Igor's profile "
        "(check his stack/tools/projects in the PROFILE section). "
        "Anchoring on a tool Igor doesn't know is an automatic "
        "fail.\n"
        "    • service_match: the service must map to something "
        "Igor actually builds according to his profile. No "
        "anchoring on services Igor hasn't demonstrated.\n"
        "    • case_study: only technical/stack case studies whose "
        "stack overlaps Igor's profile are allowed. Vertical case "
        "studies are banned full stop — even with a pointer idiom "
        "like \"— that's exactly my lane\", framing Igor next to a "
        "legal or healthcare case study IS dishonest.\n"
        "  If no honest, profile-matching hook exists in "
        "enriched_data, return `null` for both `personalized_opener` "
        "and `subject_line` and set `hook_type` to `other`. Igor "
        "would rather skip this agency than send an opener that "
        "pretends he does work he doesn't do. A dishonest hook is "
        "an AUTOMATIC FAIL regardless of how good the prose is.\n"
        "- No generic openers ('I love your work', 'your agency is "
        "impressive'). Never invent details that are not in "
        "enriched_data.\n"
        "- Voice: direct, conversational, never salesy, never buzzwords "
        "like 'synergy', 'leverage', 'disrupt', 'revolutionize'. Use "
        "natural capitalization — sentence-initial caps, proper nouns "
        "(company names, person names, product names like n8n/Supabase) "
        "capitalized as they normally are. Casual tone is about "
        "directness, not about lowercasing everything.\n"
        "- Igor's INTENT must be visible in the very first sentence: he "
        "is reaching out about contract work. Pure praise without an "
        "ask reads like a fan letter or a questionnaire answer — the "
        "reader should know WHY you're writing from the first few "
        "words.\n"
        "- The opener POINTS at Igor's relevance; the template body "
        "DESCRIBES it. Think of them as two halves of one handoff. The "
        "opener teases — it cues the reader that Igor is in the same "
        "space as the agency, without telling them what that space is. "
        "The template below does the telling: stack, examples, links. "
        "You have Igor's full profile AND the verbatim template in your "
        "context — use both to pick a pointer that makes the reader "
        "want to read the next line, without restating what the next "
        "line already says.\n"
        "- Addressing the agency. DEFAULT form is SALUTATION + "
        "second person: start with a short vocative greeting that "
        "includes the agency name, then continue with 'your' / "
        "'you' for the rest of the opener. Vary the exact greeting "
        "across generations so it never feels templated — pick any "
        "of these natural shapes (or a close paraphrase):\n"
        "    • 'Hi Spruik team —'\n"
        "    • 'Spruik team —'\n"
        "    • 'Hey Spruik,'\n"
        "    • 'Hi Spruik —'\n"
        "    • 'Hello Spruik team,'\n"
        "  Never 'Dear …'. Occasionally (≈1 in 4 generations) drop "
        "the salutation entirely and open directly with a "
        "second-person fragment — 'Saw your n8n consulting work …' "
        "or 'your n8n consulting, exactly my lane …' — for "
        "variety. The salutation form is the baseline; the "
        "name-less form is the occasional break, not the norm.\n"
        "  NEVER use the third-person possessive ('Spruik's X', "
        "'Acme's X') — that reads as if you're describing them to "
        "someone else instead of writing TO them. 'Hi Spruik team "
        "—' IS a direct address (valid); 'Spruik's X' is NOT.\n"
        "- Pointer-idiom format. The pointer MUST appear either as a "
        "fragment after a dash or comma, or as a clause starting with "
        "'that's' / 'exactly'. It MUST NOT appear as a subject "
        "complement via 'is' — equating the agency's product with a "
        "work-category reads as strained grammar ('your consulting IS "
        "my kind of work' parses the same as 'your product IS my "
        "beverage'). Pick one idiom per generation, vary across "
        "generations:\n"
        "    • \"— that's my lane\"\n"
        "    • \"— that's exactly my kind of work\"\n"
        "    • \"— exactly what I do\"\n"
        "    • \"— same space I build in\"\n"
        "    • \"— that's my wheelhouse\"\n"
        "    • \"— the kind of work I'm looking for\"\n"
        "    • \", which is where I'd want to contribute\"\n"
        "    • \"— that's the kind of thing I work on\"\n"
        "  Feel free to paraphrase — just keep the fragment-or-"
        "\"that's\" structure and the pointer-not-description "
        "character.\n"
        "- The template's very first body line is 'I build production "
        "systems using no-code (n8n, WeWeb, Supabase) and AI-assisted "
        "development…'. Your opener must NOT paraphrase this in any "
        "form. Side-by-side contrast:\n"
        "    GOOD: \"Saw your n8n consulting work — that's exactly my "
        "lane. Reaching out about contract work.\"\n"
        "    GOOD: \"n8n consulting is exactly what I do — reaching "
        "out about contract work.\"\n"
        "    GOOD: \"Hi Spruik team — your n8n consulting and "
        "marketing automation, exactly the kind of work I do. "
        "Reaching out about contract work.\" (salutation form — "
        "valid way to include the agency name without using a "
        "possessive; pointer idiom attached as a fragment after "
        "comma, NOT via 'is')\n"
        "    BAD:  \"Spruik's n8n consulting and marketing automation "
        "— the kind of work I'm looking for.\" (two failures: (1) "
        "third-person possessive 'Spruik's' — when the agency name "
        "needs to appear in the opener, use salutation form 'Hi "
        "Spruik team —' instead; (2) the intent clause 'Reaching out "
        "about contract work' is MISSING — the intent MUST always be "
        "present in the opener, regardless of any other feedback or "
        "constraints)\n"
        "    BAD:  \"Spruik's n8n consulting service is my kind of "
        "work. Reaching out about contract work.\" (two failures: "
        "third-person 'Spruik's' instead of 'your' or salutation; "
        "AND 'is my kind of work' equates the product with a work "
        "category — use '— my kind of work' as a fragment, or "
        "\"that's my kind of work\" with an explicit 'that's', never "
        "'X is my kind of work')\n"
        "    BAD:  \"Saw Spruik's n8n consulting work since I have "
        "experience with production automation systems.\" (echoes the "
        "template body + third person)\n"
        "    BAD:  \"Spruik's n8n consulting caught my eye as a match "
        "for my expertise in no-code workflow building.\" (echoes + "
        "third person)\n"
        "  Failure modes across BAD examples: (1) third-person "
        "'Spruik's' instead of second-person 'your' or dropping the "
        "name; (2) the opener's second clause describes Igor's "
        "experience/expertise/focus/work, echoing what the template "
        "already states below; (3) pointer idiom attached via 'is' "
        "(equation) instead of via a dash fragment or 'that's' "
        "clause.\n"
        "- Vary structure every generation. Do not anchor on a single "
        "opening verb ('saw…', 'reaching out…', \"Spruik's … caught my "
        "eye\") or pointer idiom across consecutive calls. Different "
        "agencies deserve different framings depending on which "
        "concrete hook you pick from their enriched_data. Producing "
        "multiple openers that start with the same 2-3 words is a "
        "failure.\n"
        "- Subject line ≤ 60 characters, no clickbait, no emoji, no fake "
        "'Re:'. The subject should reference the same concrete hook the "
        "opener uses, so the reader sees continuity.\n\n"

        f"IGOR'S PROFILE (what he's actually built):\n{profile_text}\n\n"

        "FULL EMAIL TEMPLATE (READ-ONLY — what will actually be sent):\n"
        "Below is the exact body that will ship. The `{personalized_opener}` "
        "placeholder is substituted with your opener via a deterministic "
        "string.replace on the server. You NEVER output the body — you only "
        "output opener + subject. Use this purely as context so your opener "
        "flows naturally into the first body line and so you don't repeat "
        "anything already written below.\n"
        f"---BEGIN TEMPLATE---\n{template_text}\n---END TEMPLATE---\n\n"

        "Return ONLY this JSON:\n"
        "{\n"
        '  "subject_line": "<≤ 60 chars or null>",\n'
        '  "personalized_opener": "<1-2 sentences or null>",\n'
        '  "hook_type": "case_study|service|blog|tool_match|other",\n'
        '  "hook_reference": "<the exact string from enriched_data you used, '
        'or null if no concrete hook was found>"\n'
        "}"
    )


def _call_llm(enriched: dict, profile: dict, extra_feedback: str | None = None,
              prior_opener: str | None = None) -> dict:
    agency_text = json.dumps(enriched, ensure_ascii=False, indent=2)
    profile_text = json.dumps(profile, ensure_ascii=False, indent=2)
    template_text = _load_template()

    user_content = f"AGENCY ENRICHED DATA:\n{agency_text}"
    if prior_opener:
        user_content += f"\n\nPRIOR OPENER (to improve):\n{prior_opener}"
    if extra_feedback:
        user_content += (
            f"\n\nIGOR'S FEEDBACK ON THE PRIOR DRAFT:\n{extra_feedback}\n"
            "Apply this feedback literally. If he says shorter, make it "
            "shorter. If he says less formal, drop formality. Do not "
            "argue.\n\n"
            "BUT: the HARD RULES in the system prompt are INVIOLABLE. "
            "Applying feedback must NOT cause you to drop a hard rule. "
            "In particular:\n"
            "- The INTENT clause ('Reaching out about contract work' or "
            "equivalent) MUST remain visible in the first sentence, even "
            "if the feedback is silent about it. If the prior opener had "
            "the intent clause and your regenerated opener doesn't, you "
            "have failed the task.\n"
            "- Third-person possessive ('Spruik's X') remains forbidden. "
            "If the feedback asks to add the agency name in the opener, "
            "use the SALUTATION form ('Hi Spruik team —', 'Spruik team "
            "—') instead of the possessive. Never 'Spruik's X'.\n"
            "- The pointer idiom must still be a fragment or 'that's' "
            "clause, never 'X is my kind of work'.\n"
            "If feedback and hard rules appear to conflict, find a form "
            "that satisfies both."
        )

    response = chat_completion(
        model="gpt-4.1-mini",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": _system_prompt(profile_text, template_text)},
            {"role": "user", "content": user_content},
        ],
    )
    return json.loads(response.choices[0].message.content)


def _pick_primary_contact(sb, agency_id: str) -> dict | None:
    """Pick the primary contact for this agency.

    Since pattern guessing was removed, every contact is
    `email_status='scraped_visible'` (only what the site actually
    publishes). All scraped contacts are flagged `is_primary=True`, so
    we just sort by email string for deterministic selection and
    return the first one.
    """
    rows = sb.table("agency_contacts").select("*").eq("agency_id", agency_id).execute().data or []
    if not rows:
        return None
    rows.sort(key=lambda r: (r.get("email") or ""))
    return rows[0]


def draft_for_agency(agency_id: str) -> int | None:
    """Generate a draft for one qualified agency. Returns the new draft's id,
    or None if no concrete hook was found (agency flipped to `no_hook_skip`)."""
    sb = get_supabase()
    agency = sb.table("agency_agencies").select("*").eq("id", agency_id).limit(1).execute().data
    if not agency:
        logger.warning(f"Agency {agency_id} not found")
        return None
    agency = agency[0]

    contact = _pick_primary_contact(sb, agency_id)
    if not contact:
        logger.info(f"No contact for {agency_id} — marking no_contact")
        sb.table("agency_agencies").update({"status": "no_contact"}).eq("id", agency_id).execute()
        return None

    profile = get_profile() or {}
    enriched = agency.get("enriched_data") or {}
    result = _call_llm(enriched, profile)

    opener = result.get("personalized_opener")
    subject = result.get("subject_line")
    if not opener or not subject:
        logger.info(f"No concrete hook for {agency_id} — marking no_hook_skip")
        sb.table("agency_agencies").update({"status": "no_hook_skip"}).eq("id", agency_id).execute()
        return None

    body = _assemble_body(opener)
    from_email = (profile.get("agency_sender_email") or "").strip()
    if not from_email:
        import os
        from_email = os.environ.get("AGENCY_SENDER_EMAIL", "")

    row = {
        "agency_id": agency_id,
        "contact_id": contact["id"],
        "to_email": contact["email"],
        "from_email": from_email,
        "subject": subject[:200],
        "body": body,
        "template_id": TEMPLATE_ID,
        "personalization": {
            "hook_type": result.get("hook_type", "other"),
            "hook_reference": result.get("hook_reference"),
            "personalized_opener": opener,
        },
        "status": "ready_to_send",
        "revision": 0,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    inserted = sb.table("agency_outreach_messages").insert(row).execute()
    draft_id = inserted.data[0]["id"]

    sb.table("agency_agencies").update({
        "status": "ready_to_send",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", agency_id).execute()

    logger.info(f"Drafted {draft_id} for {agency_id}")
    return draft_id


def regenerate(draft_id: int, feedback_text: str) -> int | None:
    """Re-run drafting for an existing row, incorporating Igor's feedback.

    Called by the Telegram /edit flow. Overwrites subject/body/personalization
    on the same row, increments `revision`, stores `edit_feedback`.
    """
    sb = get_supabase()
    draft = sb.table("agency_outreach_messages").select("*").eq("id", draft_id).limit(1).execute().data
    if not draft:
        logger.warning(f"Draft {draft_id} not found")
        return None
    draft = draft[0]

    agency = sb.table("agency_agencies").select("*").eq("id", draft["agency_id"]).limit(1).execute().data
    if not agency:
        return None
    agency = agency[0]

    profile = get_profile() or {}
    enriched = agency.get("enriched_data") or {}
    prior_opener = (draft.get("personalization") or {}).get("personalized_opener")

    result = _call_llm(
        enriched, profile,
        extra_feedback=feedback_text,
        prior_opener=prior_opener,
    )
    opener = result.get("personalized_opener")
    subject = result.get("subject_line")
    if not opener or not subject:
        logger.info(f"Regenerate for draft {draft_id} returned no hook — leaving row as-is")
        return None

    body = _assemble_body(opener)
    sb.table("agency_outreach_messages").update({
        "subject": subject[:200],
        "body": body,
        "personalization": {
            "hook_type": result.get("hook_type", "other"),
            "hook_reference": result.get("hook_reference"),
            "personalized_opener": opener,
        },
        "revision": (draft.get("revision") or 0) + 1,
        "edit_feedback": feedback_text,
        "status": "ready_to_send",
    }).eq("id", draft_id).execute()

    logger.info(f"Regenerated draft {draft_id} with feedback")
    return draft_id


def run_batch(limit: int = 20) -> int:
    """Draft outreach for agencies in `status='contact_found'`."""
    sb = get_supabase()
    rows = (
        sb.table("agency_agencies")
        .select("id")
        .eq("status", "contact_found")
        .limit(limit)
        .execute()
        .data
        or []
    )
    n = 0
    for row in rows:
        try:
            if draft_for_agency(row["id"]):
                n += 1
        except Exception as e:
            logger.error(f"draft_for_agency failed for {row['id']}: {e}")
    return n


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    n = run_batch()
    print(f"Drafted {n} outreach messages.")
