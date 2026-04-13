# Workflow: Send outreach

## Objective
Actually deliver approved drafts via personal Gmail, with every legal
and deliverability safeguard in place.

## Inputs
- `agency_outreach_messages` row with `status='ready_to_send'`
- `credentials.json` + `token.json` (Gmail OAuth)
- `AGENCY_SENDER_EMAIL`, `AGENCY_SENDER_PHYSICAL_ADDRESS` env vars

## Pre-send safety checks (in order)
1. **Opt-out list** — `agency_opt_outs` table. Hit → reject the draft.
2. **Recency cap** — ≥1 sent message to this agency in the last 60
   days. Hit → reject.
3. **Daily cap** — `agency_send_cap` (default 15) already reached today.
   Hit → defer.
4. **Gmail history check** — `users.messages.list` with
   `q='to:{email} OR from:{email}'`. **Any hit means Igor has already
   exchanged mail with this address from his Gmail account**, even
   before this project existed. Hit → flip agency to
   `previously_contacted`, reject the draft, notify Telegram.
   **Fail closed**: if the API call itself errors, we do NOT send.

## Build the MIME message
1. Load the assembled body from `agency_outreach_messages.body`. This
   already contains the verbatim template (including the soft opt-out
   line) with the personalized opener substituted in by
   `draft_outreach._assemble_body()`.
2. Append the **physical address footer**:
   `\n\n---\n{AGENCY_SENDER_PHYSICAL_ADDRESS}\n`
3. Build an `email.message.EmailMessage`, base64url encode, POST to
   `users.messages.send`.
4. Persist `message_id`, `thread_id`, `sent_at`; flip
   `agency_outreach_messages.status='sent'` and
   `agency_agencies.status='sent'`.

The soft opt-out line is already part of the template on disk — no
send-time injection needed. The physical address stays env-driven
because it's per-sender and changes independently from the template.

## Deliverability notes
- Personal Gmail inherits sender reputation — no warmup needed.
- Hard cap: ~15/day. Going higher flips Gmail's abuse heuristics fast.
- Send window (phase 2): Tue–Thu 09:00–11:00 recipient-local time.
  For MVP we just cap daily count and let Igor approve during his day.

## Legal baseline (CAN-SPAM)
- Physical address in every message ✅
- Clear identification of the sender ✅
- Honest subject line ✅
- Working opt-out (the soft "let me know" line counts as informal
  opt-out; `agency_opt_outs` captures the real ones) ✅

## Tool
`tools/send_email_gmail.py`

```bash
python tools/send_email_gmail.py <draft_id>
```

Called by the Telegram bot's `/approve` command. First run opens a
browser for OAuth consent.

## Troubleshooting
- **OAuth consent fails** → check that the Gmail API is enabled on the
  Google Cloud project backing `credentials.json`, and that the
  authorized redirect URI includes `http://localhost`.
- **"insufficient permissions"** → the token was minted with a smaller
  scope set. Delete `token.json` and re-auth.
- **Gmail history check throws 403** → the token doesn't have
  `gmail.readonly`. Delete `token.json` and re-auth; the script already
  requests both `gmail.send` and `gmail.readonly`.
