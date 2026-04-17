"""
Phase 7 send — Gmail API OAuth sender.

Responsibilities:
    1. Pre-send safety checks:
        a. agency_opt_outs table (hard block list)
        b. agency_outreach_messages daily-cap check
        c. **Gmail history check** via users.messages.list with
           q='to:{email} OR from:{email}' — if ANY match exists, the
           address has been contacted before (from THIS gmail account),
           so skip, flip the agency to `previously_contacted`, and
           notify. Catches prior manual outreach from before this
           project existed.
        d. One-email-per-agency per 60 days (DB-level)
    2. Append the CAN-SPAM physical address footer at SEND time (env-var
       driven, per-sender). The soft opt-out line already lives verbatim
       inside `templates/cold_v1.md` — since the LLM never outputs body
       (only opener + subject) and `_assemble_body` is a pure
       `.replace()`, the template line ships byte-for-byte without risk.
    3. Build + send the MIME message, capture threadId/messageId back
       to the row, flip status to `sent`.

First-run OAuth: on the very first invocation the script opens a
browser, you click through Google's consent page, the token is cached
to `token.json`. Subsequent runs are silent.
"""

from __future__ import annotations

import os
import base64
import logging
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

from common.supabase_client import get_supabase
from common.profile import get_agency_config

load_dotenv()

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
]

_service = None


def _gmail_service():
    """Return an authorized Gmail API service. First call triggers OAuth."""
    global _service
    if _service is not None:
        return _service

    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds_path = os.environ.get("GMAIL_OAUTH_CREDENTIALS_PATH", "credentials.json")
    token_path = os.environ.get("GMAIL_TOKEN_PATH", "token.json")

    creds = None
    if Path(token_path).exists():
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
        Path(token_path).write_text(creds.to_json(), encoding="utf-8")

    _service = build("gmail", "v1", credentials=creds, cache_discovery=False)
    return _service


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


def _sent_today_count() -> int:
    sb = get_supabase()
    start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    rows = (
        sb.table("agency_outreach_messages")
        .select("id")
        .eq("status", "sent")
        .gte("sent_at", start)
        .execute()
    )
    return len(rows.data or [])


def _gmail_history_has(email: str) -> bool:
    """Return True if this Gmail account has EVER exchanged mail with `email`.

    Runs `users.messages.list` with `q='to:{email} OR from:{email}'`.
    Any hit → the user already corresponded with this address.
    """
    try:
        svc = _gmail_service()
        q = f"to:{email} OR from:{email}"
        resp = svc.users().messages().list(userId="me", q=q, maxResults=1).execute()
        return bool(resp.get("messages"))
    except Exception as e:
        # Fail closed — if we can't check, do NOT send.
        logger.error(f"Gmail history check failed for {email}: {e}. Refusing to send.")
        return True


def _compose_final_body(assembled_body: str) -> str:
    """Append the CAN-SPAM physical address footer at send time.

    The soft opt-out line lives verbatim in `templates/cold_v1.md` — no
    injection needed here. The physical address is env-var driven
    (per-sender, may change), so we add it at the bottom at send time.
    """
    body = assembled_body
    address = os.environ.get("AGENCY_SENDER_PHYSICAL_ADDRESS", "").strip()
    if address:
        body = f"{body.rstrip()}\n\n---\n{address}\n"
    return body


def _build_mime(from_email: str, to_email: str, subject: str, body: str) -> str:
    msg = EmailMessage()
    msg["To"] = to_email
    msg["From"] = from_email
    msg["Subject"] = subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    return raw


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
        # Terminal-fail the draft so the 30s scheduler loop stops retrying.
        logger.warning(f"Skip {draft_id}: to_email is empty — marking rejected")
        _reject_draft(draft_id, agency_id, "no_contact")
        return {"ok": False, "reason": "missing_to_email"}

    # ── Safety check: hard opt-out list ──
    if _is_opted_out(to_email):
        logger.info(f"Skip {draft_id}: {to_email} is opted out")
        _reject_draft(draft_id)
        return {"ok": False, "reason": "opted_out"}

    # ── Safety check: already emailed this agency in last 60 days ──
    if _already_sent_to_agency_recently(agency_id):
        logger.info(f"Skip {draft_id}: agency {agency_id} already contacted in last 60 days")
        _reject_draft(draft_id)
        return {"ok": False, "reason": "recently_contacted"}

    # ── Safety check: daily cap ──
    cfg = get_agency_config()
    cap = cfg["agency_send_cap"]
    sent_today = _sent_today_count()
    if sent_today >= cap:
        logger.info(f"Skip {draft_id}: daily cap {cap} reached ({sent_today} sent)")
        return {"ok": False, "reason": "daily_cap_reached"}

    # ── Safety check: CAN-SPAM physical address must be configured ──
    # Fail-closed: the footer is legally required on cold outreach. If
    # the env var is unset (or silently empty), refuse to send rather
    # than ship a non-compliant message. Caught by this guard after a
    # silent hole sent draft #2 without the footer during dry-run.
    if not os.environ.get("AGENCY_SENDER_PHYSICAL_ADDRESS", "").strip():
        logger.error(
            f"Skip {draft_id}: AGENCY_SENDER_PHYSICAL_ADDRESS unset — "
            f"refusing to send without CAN-SPAM footer"
        )
        return {"ok": False, "reason": "missing_physical_address"}

    # ── Safety check: Gmail history (prior manual outreach) ──
    if _gmail_history_has(to_email):
        logger.info(f"Skip {draft_id}: {to_email} found in Gmail history")
        _reject_draft(draft_id, agency_id, "previously_contacted")
        return {"ok": False, "reason": "previously_contacted"}

    # ── Compose final body ──
    from_email = (draft.get("from_email") or os.environ.get("AGENCY_SENDER_EMAIL", "")).strip()
    if not from_email:
        return {"ok": False, "reason": "no_from_email"}
    final_body = _compose_final_body(draft["body"])

    # ── Send ──
    try:
        raw = _build_mime(from_email, to_email, draft["subject"], final_body)
        svc = _gmail_service()
        sent = svc.users().messages().send(userId="me", body={"raw": raw}).execute()
    except Exception as e:
        logger.error(f"Gmail send failed for draft {draft_id}: {e}")
        return {"ok": False, "reason": f"send_error: {e}"}

    now = datetime.now(timezone.utc).isoformat()
    sb.table("agency_outreach_messages").update({
        "status": "sent",
        "sent_at": now,
        "thread_id": sent.get("threadId"),
        "message_id": sent.get("id"),
        "body": final_body,  # persist what we actually sent
    }).eq("id", draft_id).execute()

    sb.table("agency_agencies").update({
        "status": "sent",
        "updated_at": now,
    }).eq("id", agency_id).execute()

    return {"ok": True, "message_id": sent.get("id"), "thread_id": sent.get("threadId")}


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) < 2:
        print("Usage: python send_email_gmail.py <draft_id>")
        sys.exit(1)
    print(send_draft(int(sys.argv[1])))
