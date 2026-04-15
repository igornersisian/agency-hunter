"""
Agency Hunter — Telegram review bot.

Minimal subset of Job-search-automation's bot, scoped to the review and
approval flow for cold outreach.

Commands:
    /start      short intro
    /help       command list
    /stats      counts per status in agency_agencies
    /review     show the next `ready_to_send` draft card
    /approve N  send draft #N via Gmail
    /reject  N  mark draft #N as rejected (no send)
    /edit    N <feedback>   regenerate draft #N with written feedback
    /fetch      trigger a discovery + enrichment + classify + draft pass
    /threshold [int]        view / set agency_fit_threshold on profile
    /send_cap  [int]        view / set agency_send_cap on profile
    /countries [CC,CC,...]  view / set agency_target_countries on profile

The review card shows: agency name + domain + country, fit_score,
bulleted pros + cons from fit_breakdown, subject + body preview,
and three inline buttons [Approve] [Reject] [Edit]. /edit prompts for
free-text feedback in a reply.
"""

from __future__ import annotations

import os
import sys
import json
import logging
import asyncio
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from dotenv import load_dotenv

load_dotenv()

# Auto-create tables on startup if DATABASE_URL is available
try:
    sys.path.insert(0, os.path.dirname(__file__))
    from setup_db import ensure_tables
    ensure_tables()
except Exception as _setup_err:
    logging.getLogger(__name__).warning(f"DB setup skipped: {_setup_err}")

from common.supabase_client import get_supabase
from common.profile import get_profile, get_agency_config

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Profile config helpers (write back to the shared profile row)
# ---------------------------------------------------------------------------

def _save_profile_key(key: str, value) -> None:
    sb = get_supabase()
    row = sb.table("profile").select("id,parsed").order("updated_at", desc=True).limit(1).execute()
    if not row.data:
        logger.warning("Cannot set %s — profile row missing", key)
        return
    parsed = row.data[0].get("parsed") or {}
    parsed[key] = value
    sb.table("profile").update({
        "parsed": parsed,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", row.data[0]["id"]).execute()


# Per-chat pending-input state. Each chat can be waiting for at most
# one kind of free-text follow-up at a time. Shape:
#   { chat_id: ("edit_feedback",  draft_id) }
#   { chat_id: ("no_contact_email", agency_id) }
_chat_pending: dict[int, tuple[str, object]] = {}


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Agency Hunter bot ready.\n\n"
        "Use /review to triage the next draft, /fetch to run a discovery pass, "
        "/stats to see the pipeline state, /help for the full command list."
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Plain text — underscores in command names (/no_contact, /send_cap)
    # break Telegram's Markdown parser, so we avoid parse_mode entirely.
    await update.message.reply_text(
        "Agency Hunter — command reference\n"
        "\n"
        "HOW IT WORKS\n"
        "1. /fetch runs the pipeline: Apify Google Search → dedup → "
        "enrich → classify → find contacts → draft cold email.\n"
        "2. /review walks through everything waiting on you — first "
        "ready drafts, then agencies with no scraped email. One command, "
        "one queue.\n"
        "3. Approve sends via Gmail (pre-send history check blocks "
        "duplicates). Edit asks for free-text feedback and regenerates "
        "the opener. Reject discards.\n"
        "\n"
        "PIPELINE\n"
        "/fetch — full discovery → enrich → classify → draft pass "
        "(idempotent; safe to re-run)\n"
        "/stats — agency counts per state + drafts ready + sent total\n"
        "\n"
        "REVIEW & SEND\n"
        "/review — next item in the unified queue. Shows either:\n"
        "  • a ready draft card with [Approve] [Reject] [Edit] buttons, or\n"
        "  • a no-contact agency card with [Add email] [Reject] buttons "
        "(adding an email auto-drafts and shows the draft card next).\n"
        "/approve <id> — schedule draft for send via Gmail (recipient-"
        "local Mon–Fri 09:00–17:00, random 5–20 min spacing, respects "
        "/send_cap, opt-outs, prior-contact check)\n"
        "/reject <id> — mark draft rejected, no send\n"
        "/edit <id> [feedback...] — regenerate opener. Two forms:\n"
        "  • /edit 42 make it warmer and drop the word partnership\n"
        "  • /edit 42   (then send feedback as the next message)\n"
        "/no_contact — shortcut straight to the next no-contact "
        "agency, skipping ready drafts. /review already handles this "
        "automatically — this is just a fast-path.\n"
        "\n"
        "CONFIG (writes to shared profile row)\n"
        "/threshold [N] — fit score cutoff for qualified "
        "(default 70; applies from next /fetch)\n"
        "/send_cap [N]  — daily Gmail send ceiling "
        "(default 10 via AGENCY_DAILY_SEND_CAP env)\n"
        "/countries [CC,CC,...] — ISO-2 target countries for discovery "
        "(e.g. /countries US,GB,NZ)\n"
        "\n"
        "Run any config command without arguments to just view the "
        "current value. Profile overrides take precedence over .env.\n"
        "\n"
        "SAFETY RAILS\n"
        "• No auto-send — every outbound email needs your /approve\n"
        "• Gmail pre-send check skips addresses you've already emailed\n"
        "• Role inboxes (info@, sales@, …) are never targeted\n"
        "• Replies with \"not interested\" add the address to opt-outs\n"
        "• Soft opt-out line + physical address appended at send time"
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sb = get_supabase()

    # Agency-level pipeline (one row per agency)
    agency_statuses = [
        "discovered", "enriching", "enriched",
        "classifying", "qualified", "rejected",
        "contact_found", "no_contact",
        "ready_to_send", "scheduled", "sent",
        "previously_contacted", "no_hook_skip",
        "enrich_failed", "classify_failed",
    ]
    agency_counts: dict[str, int] = {}
    for s in agency_statuses:
        r = sb.table("agency_agencies").select("id", count="exact").eq("status", s).execute()
        agency_counts[s] = r.count or 0

    # Draft-level pipeline (one row per outreach message; an agency can
    # have several drafts across revisions or re-drafts)
    def _draft_count(status: str) -> int:
        return sb.table("agency_outreach_messages").select("id", count="exact") \
            .eq("status", status).execute().count or 0

    drafts_ready     = _draft_count("ready_to_send")
    drafts_scheduled = _draft_count("scheduled")
    drafts_sent      = _draft_count("sent")
    drafts_rejected  = _draft_count("rejected")

    lines = ["📊 Agency Hunter stats", "", "AGENCIES (pipeline state)"]
    for s, n in agency_counts.items():
        if n:
            lines.append(f"  {s}: {n}")

    lines.append("")
    lines.append("DRAFTS (one agency can have several)")
    lines.append(f"  waiting for your review:   {drafts_ready}")
    lines.append(f"  approved, awaiting send:   {drafts_scheduled}")
    lines.append(f"  sent:                      {drafts_sent}")
    if drafts_rejected:
        lines.append(f"  rejected:                  {drafts_rejected}")

    await update.message.reply_text("\n".join(lines))


async def cmd_threshold(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = get_agency_config()
    if context.args:
        try:
            new_val = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Usage: /threshold <int 0-100>")
            return
        _save_profile_key("agency_fit_threshold", new_val)
        await update.message.reply_text(f"fit threshold → {new_val}")
        return
    await update.message.reply_text(f"fit threshold: {cfg['agency_fit_threshold']}")


async def cmd_send_cap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = get_agency_config()
    if context.args:
        try:
            new_val = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Usage: /send_cap <int>")
            return
        _save_profile_key("agency_send_cap", new_val)
        await update.message.reply_text(f"daily send cap → {new_val}")
        return
    await update.message.reply_text(f"daily send cap: {cfg['agency_send_cap']}")


async def cmd_countries(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg = get_agency_config()
    if context.args:
        arg = " ".join(context.args).replace(" ", "")
        codes = [c.strip().upper() for c in arg.split(",") if c.strip()]
        _save_profile_key("agency_target_countries", codes)
        await update.message.reply_text(f"target countries → {', '.join(codes)}")
        return
    await update.message.reply_text(
        f"target countries: {', '.join(cfg['agency_target_countries'])}"
    )


# ---------------------------------------------------------------------------
# Review / approve / reject / edit
# ---------------------------------------------------------------------------

def _as_dict(value) -> dict:
    """Supabase JSONB usually comes back as dict, but rows written with
    json.dumps land as a string — normalize both."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _fmt_draft_card(draft: dict, agency: dict) -> str:
    breakdown = _as_dict(agency.get("fit_breakdown"))
    pros = breakdown.get("pros") or []
    cons = breakdown.get("cons") or []

    pros_block = "\n".join(f"  + {p}" for p in pros) or "  (none)"
    cons_block = "\n".join(f"  - {c}" for c in cons) or "  (none)"

    body_preview = draft["body"]
    if len(body_preview) > 900:
        body_preview = body_preview[:900] + "…"

    return (
        f"*{agency.get('name') or agency['id']}*  ·  {agency.get('country') or '??'}\n"
        f"{agency.get('website_url') or agency['id']}\n"
        f"fit score: *{agency.get('fit_score')}*/100\n\n"
        f"*Pros:*\n{pros_block}\n\n"
        f"*Cons:*\n{cons_block}\n\n"
        f"*Subject:* {draft['subject']}\n"
        f"*To:* {draft['to_email']}\n\n"
        f"```\n{body_preview}\n```\n\n"
        f"draft id: `{draft['id']}`  rev: {draft.get('revision', 0)}"
    )


def _fetch_next_draft() -> tuple[dict | None, dict | None]:
    sb = get_supabase()
    drafts = (
        sb.table("agency_outreach_messages")
        .select("*")
        .eq("status", "ready_to_send")
        .order("created_at")
        .limit(1)
        .execute()
        .data
        or []
    )
    if not drafts:
        return None, None
    draft = drafts[0]
    agency_rows = sb.table("agency_agencies").select("*").eq("id", draft["agency_id"]).limit(1).execute().data
    return draft, (agency_rows[0] if agency_rows else None)


async def _send_next_review(reply_target) -> None:
    """Send the next review card to the given target (update.message or
    query.message — both expose reply_text). Used by /review and
    auto-advance after approve/reject."""
    draft, agency = _fetch_next_draft()
    if draft and agency:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=f"approve:{draft['id']}"),
            InlineKeyboardButton("❌ Reject",  callback_data=f"reject:{draft['id']}"),
            InlineKeyboardButton("✏️ Edit",    callback_data=f"edit:{draft['id']}"),
        ]])
        await reply_target.reply_text(
            _fmt_draft_card(draft, agency),
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
        return

    nc_agency = _fetch_next_no_contact()
    if nc_agency:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("➕ Add email", callback_data=f"nc_add_email:{nc_agency['id']}"),
            InlineKeyboardButton("❌ Reject",    callback_data=f"nc_reject:{nc_agency['id']}"),
        ]])
        await reply_target.reply_text(
            "No drafts ready. Next agency with no scraped email:\n\n"
            + _fmt_no_contact_card(nc_agency),
            parse_mode="Markdown",
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
        return

    await reply_target.reply_text(
        "Nothing to review — no ready drafts and no no-contact agencies waiting."
    )


async def cmd_review(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Unified review queue: first surfaces ready_to_send drafts, then
    falls through to no_contact agencies waiting for a manual email."""
    await _send_next_review(update.message)


async def _do_approve(draft_id: int) -> str:
    """Approve a draft: compute its scheduled_for slot and flip it into
    `status='scheduled'`. The background scheduler task will pick it up
    when its slot arrives and actually send via Gmail."""
    def _schedule_sync() -> tuple[str, str]:
        from common.send_window import compute_next_slot, country_timezone
        sb = get_supabase()

        draft = sb.table("agency_outreach_messages").select("id,agency_id,status") \
            .eq("id", draft_id).limit(1).execute().data
        if not draft:
            return ("error", f"draft {draft_id} not found")
        if draft[0]["status"] not in ("ready_to_send", "scheduled"):
            return ("error", f"draft {draft_id} is in status `{draft[0]['status']}`")

        agency_id = draft[0]["agency_id"]
        agency = sb.table("agency_agencies").select("country") \
            .eq("id", agency_id).limit(1).execute().data
        country = (agency[0].get("country") if agency else None)

        # Find the current max scheduled_for across all pending sends —
        # we need to slot the new send AFTER that with random spacing.
        pending = (
            sb.table("agency_outreach_messages")
            .select("scheduled_for")
            .eq("status", "scheduled")
            .order("scheduled_for", desc=True)
            .limit(1)
            .execute()
            .data
            or []
        )
        last_utc = None
        if pending and pending[0].get("scheduled_for"):
            last_utc = datetime.fromisoformat(
                pending[0]["scheduled_for"].replace("Z", "+00:00")
            )

        slot = compute_next_slot(country, last_utc)
        sb.table("agency_outreach_messages").update({
            "status": "scheduled",
            "scheduled_for": slot.isoformat(),
        }).eq("id", draft_id).execute()
        sb.table("agency_agencies").update({"status": "scheduled"}) \
            .eq("id", agency_id).execute()

        tz = country_timezone(country)
        local = slot.astimezone(tz)
        return ("ok", f"{local.strftime('%a %Y-%m-%d %H:%M')} {country or '??'} "
                      f"(UTC {slot.strftime('%Y-%m-%d %H:%M')})")

    loop = asyncio.get_running_loop()
    kind, msg = await loop.run_in_executor(None, _schedule_sync)
    if kind == "ok":
        return f"📅 Draft {draft_id} scheduled for {msg}"
    return f"⚠️ Could not schedule: {msg}"


async def _do_reject(draft_id: int) -> str:
    sb = get_supabase()
    draft = sb.table("agency_outreach_messages").select("agency_id") \
        .eq("id", draft_id).limit(1).execute().data
    sb.table("agency_outreach_messages").update({"status": "rejected"}).eq("id", draft_id).execute()
    if draft:
        sb.table("agency_agencies").update({"status": "rejected"}).eq("id", draft[0]["agency_id"]).execute()
    return f"❌ Rejected draft {draft_id}"


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /approve <draft_id>")
        return
    try:
        draft_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("draft_id must be an integer")
        return
    result = await _do_approve(draft_id)
    await update.message.reply_text(result)


async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /reject <draft_id>")
        return
    try:
        draft_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("draft_id must be an integer")
        return
    result = await _do_reject(draft_id)
    await update.message.reply_text(result)


async def cmd_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Either /edit <id> <feedback...> (one-shot) or /edit <id> then reply with
    feedback on the next message."""
    if not context.args:
        await update.message.reply_text("Usage: /edit <draft_id> <feedback text>")
        return
    try:
        draft_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("draft_id must be an integer")
        return

    feedback = " ".join(context.args[1:]).strip()
    if not feedback:
        # Two-step flow: remember which draft is awaiting feedback
        _chat_pending[update.effective_chat.id] = ("edit_feedback", draft_id)
        await update.message.reply_text(
            f"Send your feedback for draft {draft_id} as the next message. "
            f"(or send /cancel)"
        )
        return

    await _regenerate(update, draft_id, feedback)


async def _regenerate(update: Update, draft_id: int, feedback: str) -> None:
    def _regen_sync():
        from draft_outreach import regenerate
        return regenerate(draft_id, feedback)

    loop = asyncio.get_running_loop()
    new_id = await loop.run_in_executor(None, _regen_sync)
    if not new_id:
        await update.message.reply_text(
            f"Could not regenerate draft {draft_id} — LLM returned no concrete hook."
        )
        return

    # Re-show the updated card
    sb = get_supabase()
    draft = sb.table("agency_outreach_messages").select("*").eq("id", draft_id).limit(1).execute().data[0]
    agency = sb.table("agency_agencies").select("*").eq("id", draft["agency_id"]).limit(1).execute().data[0]

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve:{draft_id}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"reject:{draft_id}"),
        InlineKeyboardButton("✏️ Edit",    callback_data=f"edit:{draft_id}"),
    ]])
    await update.message.reply_text(
        "Regenerated draft:\n\n" + _fmt_draft_card(draft, agency),
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Inline button callback + free-text handler for /edit follow-up
# ---------------------------------------------------------------------------

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data or ""

    try:
        action, target = data.split(":", 1)
    except ValueError:
        return

    if action == "approve":
        msg = await _do_approve(int(target))
        await query.message.reply_text(msg)
        await _send_next_review(query.message)
    elif action == "reject":
        msg = await _do_reject(int(target))
        await query.message.reply_text(msg)
        await _send_next_review(query.message)
    elif action == "edit":
        _chat_pending[query.message.chat_id] = ("edit_feedback", int(target))
        await query.message.reply_text(
            f"Send your feedback for draft {target} as the next message. (or /cancel)"
        )
    elif action == "nc_add_email":
        _chat_pending[query.message.chat_id] = ("no_contact_email", target)
        await query.message.reply_text(
            f"Send the email address for *{target}* as the next message. "
            f"(or /cancel)",
            parse_mode="Markdown",
        )
    elif action == "nc_reject":
        msg = await _do_nc_reject(target)
        await query.message.reply_text(msg)
        await _send_next_review(query.message)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Free-text handler: catches follow-up input for pending two-step flows
    (/edit feedback or /no_contact email)."""
    chat_id = update.effective_chat.id
    if chat_id not in _chat_pending:
        return

    kind, target = _chat_pending.pop(chat_id)
    text = (update.message.text or "").strip()
    if text.lower() == "/cancel":
        await update.message.reply_text("Cancelled.")
        return

    if kind == "edit_feedback":
        await _regenerate(update, target, text)
    elif kind == "no_contact_email":
        await _handle_nc_email(update, target, text)


# ---------------------------------------------------------------------------
# /no_contact — manual email input for agencies with no scraped address
# ---------------------------------------------------------------------------

import re as _re

_EMAIL_RE = _re.compile(r"^[\w.+\-]+@[\w\-]+\.[\w.\-]+$")


def _fetch_next_no_contact() -> dict | None:
    sb = get_supabase()
    rows = (
        sb.table("agency_agencies")
        .select("id,name,country,website_url,short_description,fit_score,fit_breakdown,enriched_data")
        .eq("status", "no_contact")
        .order("fit_score", desc=True)
        .limit(1)
        .execute()
        .data
        or []
    )
    return rows[0] if rows else None


def _fmt_no_contact_card(agency: dict) -> str:
    breakdown = _as_dict(agency.get("fit_breakdown"))
    pros = breakdown.get("pros") or []
    pros_block = "\n".join(f"  + {p}" for p in pros[:4]) or "  (none)"

    enriched = _as_dict(agency.get("enriched_data"))
    tools = ", ".join((enriched.get("tools") or [])[:6]) or "—"
    services = ", ".join((enriched.get("services") or [])[:4]) or "—"

    desc = agency.get("short_description") or ""
    if len(desc) > 300:
        desc = desc[:300] + "…"

    return (
        f"*{agency.get('name') or agency['id']}*  ·  {agency.get('country') or '??'}\n"
        f"{agency.get('website_url') or agency['id']}\n"
        f"fit score: *{agency.get('fit_score')}*/100\n\n"
        f"{desc}\n\n"
        f"*Tools:* {tools}\n"
        f"*Services:* {services}\n\n"
        f"*Pros:*\n{pros_block}\n\n"
        f"_No email was scraped from their site. Add one manually or reject._"
    )


async def cmd_no_contact(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    agency = _fetch_next_no_contact()
    if not agency:
        await update.message.reply_text("No agencies waiting in no_contact.")
        return

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("➕ Add email", callback_data=f"nc_add_email:{agency['id']}"),
        InlineKeyboardButton("❌ Reject",    callback_data=f"nc_reject:{agency['id']}"),
    ]])
    await update.message.reply_text(
        _fmt_no_contact_card(agency),
        parse_mode="Markdown",
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


async def _do_nc_reject(agency_id: str) -> str:
    sb = get_supabase()
    sb.table("agency_agencies").update({"status": "rejected"}).eq("id", agency_id).execute()
    return f"❌ Rejected {agency_id}"


def _is_non_sendable_email(email: str) -> bool:
    local = email.split("@", 1)[0].lower()
    return local in {"noreply", "no-reply", "donotreply", "do-not-reply",
                     "mailer-daemon", "postmaster"}


async def _handle_nc_email(update: Update, agency_id: str, email: str) -> None:
    """User supplied an email address for a no_contact agency. Validate,
    insert as a contact, run draft_for_agency, show the resulting draft card."""
    email = email.strip().lower()
    if not _EMAIL_RE.match(email):
        await update.message.reply_text(
            f"`{email}` doesn't look like a valid email. "
            f"Send /no_contact to try again.",
            parse_mode="Markdown",
        )
        return
    if _is_non_sendable_email(email):
        await update.message.reply_text(
            f"`{email}` is a non-sendable system address. "
            f"Send /no_contact to try again.",
            parse_mode="Markdown",
        )
        return

    sb = get_supabase()

    # Insert (or skip if already there)
    existing = sb.table("agency_contacts").select("id").eq("agency_id", agency_id).eq("email", email).execute().data or []
    if existing:
        contact_id = existing[0]["id"]
    else:
        inserted = sb.table("agency_contacts").insert({
            "agency_id": agency_id,
            "email": email,
            "email_status": "manual_tg_input",
            "email_confidence": 100,  # user vouched for it
            "source": "telegram_manual",
            "is_primary": True,
        }).execute()
        contact_id = inserted.data[0]["id"]

    # Flip agency to contact_found so draft_for_agency will accept it
    sb.table("agency_agencies").update({"status": "contact_found"}).eq("id", agency_id).execute()

    await update.message.reply_text(
        f"✔ Added `{email}` — drafting now…",
        parse_mode="Markdown",
    )

    # Draft the email in a background thread
    def _draft_sync():
        from draft_outreach import draft_for_agency
        return draft_for_agency(agency_id)

    loop = asyncio.get_running_loop()
    try:
        draft_id = await loop.run_in_executor(None, _draft_sync)
    except Exception as e:
        await update.message.reply_text(f"Drafting failed: {e}")
        return

    if not draft_id:
        await update.message.reply_text(
            f"Could not generate a draft — no concrete hook found in enriched data. "
            f"Agency marked `no_hook_skip`."
        )
        return

    # Show the fresh draft card with the normal review buttons
    draft = sb.table("agency_outreach_messages").select("*").eq("id", draft_id).limit(1).execute().data[0]
    agency = sb.table("agency_agencies").select("*").eq("id", agency_id).limit(1).execute().data[0]

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve:{draft_id}"),
        InlineKeyboardButton("❌ Reject",  callback_data=f"reject:{draft_id}"),
        InlineKeyboardButton("✏️ Edit",    callback_data=f"edit:{draft_id}"),
    ]])
    await update.message.reply_text(
        "Draft ready:\n\n" + _fmt_draft_card(draft, agency),
        parse_mode="Markdown",
        reply_markup=keyboard,
    )


# ---------------------------------------------------------------------------
# Background scheduler — actually sends `status='scheduled'` drafts once
# their slot arrives. Runs as a JobQueue repeating job every 30s.
#
# One-draft-per-tick policy: if the bot was offline and multiple drafts
# are overdue on the same tick, send only the first (oldest slot) and
# reschedule the rest forward from now using `compute_next_slot`, so the
# original random 5-20 min spacing is reapplied inside the recipient's
# send window instead of blasting a batch at secondly cadence.
# ---------------------------------------------------------------------------


async def scheduler_tick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Poll for scheduled drafts whose slot has arrived, send them via
    Gmail. Runs every 30s via JobQueue. All errors are logged, nothing
    is re-raised — the job must survive transient failures."""
    sb = get_supabase()
    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()

    try:
        due = (
            sb.table("agency_outreach_messages")
            .select("id,agency_id,to_email,scheduled_for")
            .eq("status", "scheduled")
            .lte("scheduled_for", now_iso)
            .order("scheduled_for")
            .execute()
            .data
            or []
        )
    except Exception as e:
        logger.error(f"scheduler tick: query failed — {e}")
        return

    if not due:
        return

    # If more than one is overdue, the bot was likely offline. Send the
    # oldest now, and re-stagger the rest forward from now through the
    # same send-window + 5-20 min spacing logic used at approve time.
    if len(due) > 1:
        from common.send_window import compute_next_slot

        to_reschedule = due[1:]
        agency_ids = list({row["agency_id"] for row in to_reschedule})
        try:
            agencies = (
                sb.table("agency_agencies")
                .select("id,country")
                .in_("id", agency_ids)
                .execute()
                .data
                or []
            )
        except Exception as e:
            logger.error(f"scheduler tick: agency country fetch failed — {e}")
            agencies = []
        country_by_agency = {a["id"]: a.get("country") for a in agencies}

        logger.warning(
            f"scheduler tick: {len(due)} overdue — sending oldest, "
            f"rescheduling {len(to_reschedule)} forward"
        )

        # Seed with `now_utc` (not None) so the FIRST rescheduled draft
        # still gets 5-20 min spacing from the draft we're about to send
        # this tick — otherwise compute_next_slot skips spacing when
        # last_utc is None and we'd fire two sends ~30s apart.
        last_utc: datetime = now_utc
        for row in to_reschedule:
            country = country_by_agency.get(row["agency_id"])
            new_slot = compute_next_slot(country, last_utc, now_utc=now_utc)
            try:
                sb.table("agency_outreach_messages").update({
                    "scheduled_for": new_slot.isoformat(),
                }).eq("id", row["id"]).execute()
            except Exception as e:
                logger.error(
                    f"scheduler tick: reschedule failed for draft {row['id']} — {e}"
                )
                continue
            last_utc = new_slot

        # Only process the oldest this tick; the rest now sit in the future.
        due = due[:1]

    logger.info(f"scheduler tick: {len(due)} draft(s) due for send")

    def _send_sync(draft_id: int) -> dict:
        from send_email_gmail import send_draft
        return send_draft(draft_id)

    loop = asyncio.get_running_loop()
    admin_chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    for row in due:
        draft_id = row["id"]
        try:
            result = await loop.run_in_executor(None, _send_sync, draft_id)
        except Exception as e:
            logger.error(f"scheduler tick: send_draft({draft_id}) raised {e}")
            if admin_chat_id:
                try:
                    await context.bot.send_message(
                        chat_id=int(admin_chat_id),
                        text=f"⚠️ Scheduled send failed for draft {draft_id}: {e}",
                    )
                except Exception:
                    pass
            continue

        if result.get("ok"):
            logger.info(f"scheduler tick: sent draft {draft_id} → {row['to_email']}")
            if admin_chat_id:
                try:
                    await context.bot.send_message(
                        chat_id=int(admin_chat_id),
                        text=(
                            f"✉️ Sent draft {draft_id} → {row['to_email']} "
                            f"(message_id={result.get('message_id')})"
                        ),
                    )
                except Exception:
                    pass
        else:
            reason = result.get("reason", "unknown")
            logger.warning(f"scheduler tick: draft {draft_id} skipped — {reason}")

            # Daily cap is self-inflicted and predictable — push the draft
            # past the end of today's send window so compute_next_slot lands
            # it on the next valid weekday, and stay silent. Without this
            # the draft remains overdue and scheduler_tick retries every
            # 30s, each retry pinging Telegram.
            if reason == "daily_cap_reached":
                from common.send_window import compute_next_slot, _SENDER_TZ

                end_of_today_local = now_utc.astimezone(_SENDER_TZ).replace(
                    hour=22, minute=0, second=0, microsecond=0
                )
                next_slot = compute_next_slot(
                    None,
                    last_scheduled_utc=None,
                    now_utc=end_of_today_local.astimezone(timezone.utc),
                )
                try:
                    sb.table("agency_outreach_messages").update({
                        "scheduled_for": next_slot.isoformat(),
                    }).eq("id", draft_id).execute()
                    logger.info(
                        f"scheduler tick: draft {draft_id} pushed to "
                        f"{next_slot.astimezone(_SENDER_TZ).isoformat()} (cap hit)"
                    )
                except Exception as e:
                    logger.error(
                        f"scheduler tick: cap push failed for draft {draft_id} — {e}"
                    )
                continue

            if admin_chat_id:
                try:
                    await context.bot.send_message(
                        chat_id=int(admin_chat_id),
                        text=f"⚠️ Draft {draft_id} skipped: {reason}",
                    )
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# /fetch — trigger pipeline
# ---------------------------------------------------------------------------

async def cmd_fetch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Starting discovery → enrich → classify → draft…")

    def _run_sync():
        from run_pipeline import run_pipeline
        return run_pipeline()

    loop = asyncio.get_running_loop()
    try:
        summary = await loop.run_in_executor(None, _run_sync)
    except Exception as e:
        await update.message.reply_text(f"Pipeline failed: {e}")
        return

    await update.message.reply_text(
        f"Done.\n"
        f"discovered: {summary.get('discovered', 0)}\n"
        f"enriched:   {summary.get('enriched',   0)}\n"
        f"classified: {summary.get('classified', 0)}\n"
        f"qualified:  {summary.get('qualified',  0)}\n"
        f"drafts:     {summary.get('drafts',     0)}"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("review", cmd_review))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CommandHandler("edit", cmd_edit))
    app.add_handler(CommandHandler("no_contact", cmd_no_contact))
    app.add_handler(CommandHandler("fetch", cmd_fetch))
    app.add_handler(CommandHandler("threshold", cmd_threshold))
    app.add_handler(CommandHandler("send_cap", cmd_send_cap))
    app.add_handler(CommandHandler("countries", cmd_countries))

    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    # Background scheduler — polls every 30s, sends drafts whose slot is due
    app.job_queue.run_repeating(scheduler_tick, interval=30, first=15)

    logger.info("Agency Hunter bot started (long-polling + scheduler)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
