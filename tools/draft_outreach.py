"""
Phase 6 — outreach drafting.

Loads `templates/cold_v1.md` and the agency's `enriched_data`, then calls
OpenAI in JSON mode to produce ONLY:
    {subject_line, personalized_opener, hook_type, hook_reference}

OPENER CONCEPT: Igor built an automated pipeline that scrapes, enriches,
and scores agencies by stack fit. The opener tells the agency that this
pipeline matched them and WHY (specific tool/service overlap). This is
a flex — demonstrating automation skills TO an automation agency.

SUBJECT LINE: simple and human — just tool names + "contract work/dev/
contractor". E.g. "Contract dev — n8n + Make", "n8n contractor".
No clickbait, no "flagged you", no "surfaced a match".

The final email body is assembled by substituting the opener into the
template verbatim — **no other part of the template is touched**.

Regeneration:
    regenerate(draft_id, feedback_text) re-runs drafting with the prior
    opener and Igor's free-text feedback in the prompt, overwrites the
    row, increments `revision`, and persists `edit_feedback`.
    Used by the Telegram /edit flow.
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
        "specific AI-automation agency.\n\n"

        "CONCEPT: Igor built an automated pipeline that scrapes, enriches, "
        "and scores automation agencies by stack fit. The opener tells the "
        "agency that this pipeline matched them — and WHY (specific tools "
        "or services that overlap with Igor's stack). This is a flex: "
        "he's demonstrating automation skills TO an automation agency.\n\n"

        "Everything below your opener is a fixed template (shown further "
        "down). Your opener is the FIRST part; the template is the "
        "CONTINUATION. Your opener MUST NOT repeat anything in the "
        "template.\n\n"

        "OPENER FORMAT (1-2 sentences, pick one and fill in [concrete_detail]):\n"
        '  "Built a pipeline that scrapes, enriches, and scores automation '
        'agencies by stack fit — yours flagged because of [concrete_detail]. '
        'Reaching out about contract work."\n'
        '  "Wrote an AI pipeline that qualifies agencies by tool stack — '
        'your [concrete_detail] is why it matched us. Reaching out about '
        'contract work."\n'
        '  "My agency-matching pipeline flagged your [concrete_detail] as a '
        'stack overlap — reaching out about contract availability."\n'
        '  "Built an outreach system that scores agencies by stack fit — '
        'your [concrete_detail] is what surfaced you. Quick note about '
        'contract work."\n\n'

        "CONCRETE_DETAIL RULES:\n"
        "- Pick the strongest overlap between the agency's tools/services "
        "and Igor's stack: n8n, make.com, zapier, OpenAI, Anthropic, "
        "Supabase, WeWeb, Webflow, Retool, Bubble, Airtable, LangChain, "
        "Claude Code.\n"
        "- Priority: tool_match (agency lists a tool Igor knows) > "
        "service_match (agency offers a service Igor ships — RAG, "
        "multi-agent AI, workflow automation, no-code apps) > other.\n"
        "- Keep it brief: 'your n8n + Make work', 'your LangChain and "
        "Anthropic stack', 'your RAG implementation focus'.\n"
        "- Only reference things ACTUALLY in enriched_data. Never invent.\n"
        "- If no honest overlap exists, return null for opener and "
        "subject.\n\n"

        "SUBJECT LINE RULES:\n"
        "- ≤ 60 characters, simple, human-readable.\n"
        "- Format: pick one of these patterns with the matching tools:\n"
        '  "Contract dev — n8n + Make"\n'
        '  "Reaching out — LangChain + OpenAI"\n'
        '  "n8n + Supabase contractor"\n'
        '  "n8n — contract work"\n'
        '  "Contract work — Anthropic + n8n"\n'
        "- NO clickbait, NO emoji, NO fake 'Re:', NO phrases like "
        "'caught my pipeline', 'surfaced a match', 'flagged you'.\n"
        "- Just tools/services + 'contract work/dev/contractor'.\n\n"

        "HARD RULES:\n"
        "- Never use third-person possessive ('Agency's X'). Use 'your'.\n"
        "- Igor's intent (contract work) must be in the opener.\n"
        "- Don't paraphrase the template body.\n"
        "- Voice: direct, conversational, no buzzwords.\n\n"

        f"IGOR'S PROFILE:\n{profile_text}\n\n"

        "FULL EMAIL TEMPLATE (READ-ONLY context):\n"
        f"---BEGIN TEMPLATE---\n{template_text}\n---END TEMPLATE---\n\n"

        "Return ONLY this JSON:\n"
        "{\n"
        '  "subject_line": "<≤ 60 chars or null>",\n'
        '  "personalized_opener": "<1-2 sentences or null>",\n'
        '  "hook_type": "tool_match|service|other",\n'
        '  "hook_reference": "<tools/services from enriched_data that matched>"\n'
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
            "Apply this feedback literally. Keep the pipeline-hook opener "
            "format and intent clause ('Reaching out about contract work'). "
            "Never use third-person possessive ('Agency's X')."
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


def _pick_best_email(enriched: dict) -> str | None:
    """Pick the best email for cold outreach from enriched_data.

    Uses best_contact_email if the LLM already chose one during enrichment.
    Falls back to a heuristic over visible_emails for older rows.
    """
    # LLM-chosen best contact (new enrichment flow)
    best = (enriched.get("best_contact_email") or "").strip().lower()
    if best and "@" in best:
        return best

    # Fallback heuristic for pre-existing enriched_data without best_contact_email
    emails = [e.strip().lower() for e in (enriched.get("visible_emails") or []) if e and "@" in e]
    if not emails:
        return None

    _NON_SENDABLE = {"noreply", "no-reply", "donotreply", "do-not-reply", "mailer-daemon", "postmaster"}
    _DEPRIORITIZE = {"careers", "jobs", "support", "hr", "recruiting", "billing", "abuse"}
    emails = [e for e in emails if e.split("@")[0].lower() not in _NON_SENDABLE]
    if not emails:
        return None

    def _score(email: str) -> int:
        local = email.split("@")[0].lower()
        if local in _DEPRIORITIZE:
            return 0
        if local in ("contact", "enquiries", "enquiry"):
            return 80
        if local == "hello":
            return 70
        if local == "info":
            return 60
        # Personal-looking email (not a role address) scores highest
        if local not in ("hello", "info", "contact", "sales", "team", "admin", "office"):
            return 90
        return 50

    emails.sort(key=_score, reverse=True)
    return emails[0]


def draft_for_agency(agency_id: str) -> int | None:
    """Generate a draft for one qualified agency. Returns the new draft's id,
    or None if no concrete hook was found (agency flipped to `no_hook_skip`)."""
    sb = get_supabase()
    agency = sb.table("agency_agencies").select("*").eq("id", agency_id).limit(1).execute().data
    if not agency:
        logger.warning(f"Agency {agency_id} not found")
        return None
    agency = agency[0]

    enriched = agency.get("enriched_data") or {}
    to_email = _pick_best_email(enriched)
    if not to_email:
        logger.info(f"No contact for {agency_id} — marking no_contact")
        sb.table("agency_agencies").update({"status": "no_contact"}).eq("id", agency_id).execute()
        return None

    profile = get_profile() or {}
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
        "to_email": to_email,
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
    """Draft outreach for qualified agencies (contact selection is now inline)."""
    sb = get_supabase()
    rows = (
        sb.table("agency_agencies")
        .select("id")
        .eq("status", "qualified")
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
