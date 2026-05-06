"""
Phase 7 send — Gmail SMTP sender (App Password auth).

Responsibilities:
    1. Pre-send safety checks:
        a. agency_opt_outs table (hard block list)
        b. 60-day duplicate check (DB-level)
        c. DB-based prior-contact check (agency_outreach_messages where
           to_email already marked `sent`) — replaces the old Gmail-API
           history scan, which required OAuth + gmail.readonly and broke
           every 7 days in Testing-mode apps.
        d. Daily-cap check
    2. Open an SMTP_SSL connection to smtp.gmail.com:465, log in with the
       App Password, send, close. Save the generated Message-ID back to
       the row, flip status to `sent`.
"""

from __future__ import annotations

import os
import smtplib
import logging
from email.message import EmailMessage
from email.utils import make_msgid
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

from common.supabase_client import get_supabase
from common.profile import get_agency_config

load_dotenv()

logger = logging.getLogger(__name__)


def _is_opted_out(email: str) -> bool:
    row = get_supabase().table("agency_opt_outs").select("email").eq("email", email.lower()).execute()
    return bool(row.data)


def _already_sent_to_agency_recently(agency_id: str, days: int = 60) -> bool:
    sb = get_supabase()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = (
        sb.table("agency_outreach_messages")
        .select("id")
        .eq("agency_id", agency_id)
        .eq("status", "sent")
        .gte("sent_at", cutoff)
        .execute()
    )
    return bool(rows.data)


def _sent_today_count(from_email: str) -> int:
    sb = get_supabase()
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    rows = (
        sb.table("agency_outreach_messages")
        .select("id")
        .eq("status", "sent")
        .eq("from_email", from_email)
        .gte("sent_at", start)
        .execute()
    )
    return len(rows.data or [])


def _accounts() -> list[tuple[str, str]]:
    """Configured sender accounts as (email, password) pairs.

    Accounts with a missing email OR missing password are dropped — the
    env presence is the only switch. This means removing either var in
    `.env` cleanly degrades to single-account mode without code changes.
    """
    pairs = [
        (os.environ.get("AGENCY_SENDER_EMAIL", "").strip(),
         os.environ.get("GMAIL_APP_PASSWORD", "").strip()),
        (os.environ.get("AGENCY_SENDER_EMAIL_2", "").strip(),
         os.environ.get("2ACC_GMAIL_APP_PASSWORD", "").strip()),
    ]
    return [(e, p) for e, p in pairs if e and p]


def _pick_account(cap: int) -> tuple[str, str] | None:
    """Pick the configured account with the most remaining daily capacity.

    Returns (email, password) or None if every account has hit the cap.
    Ties break toward the first account in `_accounts()` order, which
    effectively round-robins when both accounts stay in lockstep.
    """
    best: tuple[int, str, str] | None = None
    for email, password in _accounts():
        sent = _sent_today_count(email)
        if sent >= cap:
            continue
        if best is None or sent < best[0]:
            best = (sent, email, password)
    if best is None:
        return None
    _, email, password = best
    return email, password


def _db_history_has(email: str) -> bool:
    """True iff this project has ever marked a message to `email` as sent.

    Replaces the old Gmail API inbox scan. Catches the case where we
    previously sent to this address via this pipeline but the 60-day
    dedupe (`_already_sent_to_agency_recently`) wouldn't trigger because
    the agency_id differs (e.g. the same inbox listed under two domains).
    """
    sb = get_supabase()
    rows = (
        sb.table("agency_outreach_messages")
        .select("id")
        .eq("to_email", email.lower())
        .eq("status", "sent")
        .limit(1)
        .execute()
    )
    return bool(rows.data)


def _build_message(from_email: str, to_email: str, subject: str, body: str) -> tuple[EmailMessage, str]:
    msg = EmailMessage()
    msg["To"] = to_email
    msg["From"] = from_email
    msg["Subject"] = subject
    message_id = make_msgid(domain=from_email.split("@", 1)[-1] or "localhost")
    msg["Message-ID"] = message_id
    msg.set_content(body)
    return msg, message_id


def _smtp_send(from_email: str, password: str, to_email: str, msg: EmailMessage) -> None:
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
        server.login(from_email, password)
        server.send_message(msg, from_addr=from_email, to_addrs=[to_email])


def _reject_draft(draft_id: int, agency_id: str | None = None, agency_status: str | None = None) -> None:
    sb = get_supabase()
    sb.table("agency_outreach_messages").update({"status": "rejected"}).eq("id", draft_id).execute()
    if agency_id and agency_status:
        sb.table("agency_agencies").update({
            "status": agency_status,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", agency_id).execute()


def send_draft(draft_id: int) -> dict:
    """Send one approved/ready draft. Returns a status dict."""
    sb = get_supabase()
    rows = sb.table("agency_outreach_messages").select("*").eq("id", draft_id).limit(1).execute().data
    if not rows:
        return {"ok": False, "reason": "draft_not_found"}
    draft = rows[0]

    to_email = (draft.get("to_email") or "").strip()
    agency_id = draft.get("agency_id")
    if not to_email:
        logger.warning(f"Skip {draft_id}: to_email is empty — marking rejected")
        _reject_draft(draft_id, agency_id, "no_contact")
        return {"ok": False, "reason": "missing_to_email"}

    if _is_opted_out(to_email):
        logger.info(f"Skip {draft_id}: {to_email} is opted out")
        _reject_draft(draft_id)
        return {"ok": False, "reason": "opted_out"}

    if _already_sent_to_agency_recently(agency_id):
        logger.info(f"Skip {draft_id}: agency {agency_id} already contacted in last 60 days")
        _reject_draft(draft_id)
        return {"ok": False, "reason": "recently_contacted"}

    cfg = get_agency_config()
    cap = cfg["agency_send_cap"]
    picked = _pick_account(cap)
    if picked is None:
        logger.info(f"Skip {draft_id}: daily cap {cap}/account reached on all accounts")
        return {"ok": False, "reason": "daily_cap_reached"}
    from_email, password = picked

    if _db_history_has(to_email):
        logger.info(f"Skip {draft_id}: {to_email} already marked sent in DB")
        _reject_draft(draft_id, agency_id, "previously_contacted")
        return {"ok": False, "reason": "previously_contacted"}

    msg, message_id = _build_message(from_email, to_email, draft["subject"], draft["body"])

    try:
        _smtp_send(from_email, password, to_email, msg)
    except smtplib.SMTPAuthenticationError as e:
        logger.error(f"SMTP auth failed for draft {draft_id}: {e}")
        return {"ok": False, "reason": "smtp_auth_error"}
    except Exception as e:
        logger.error(f"SMTP send failed for draft {draft_id}: {e}")
        return {"ok": False, "reason": f"send_error: {e}"}

    now = datetime.now(timezone.utc).isoformat()
    sb.table("agency_outreach_messages").update({
        "status": "sent",
        "sent_at": now,
        "message_id": message_id,
        "from_email": from_email,
    }).eq("id", draft_id).execute()

    sb.table("agency_agencies").update({
        "status": "sent",
        "updated_at": now,
    }).eq("id", agency_id).execute()

    return {"ok": True, "message_id": message_id}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("Usage: python send_email_gmail.py <draft_id>")
        sys.exit(1)
    print(send_draft(int(sys.argv[1])))
