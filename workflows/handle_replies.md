# Workflow: Handle replies (phase 2)

## Objective
Poll Gmail for replies to sent outreach, classify them, and update
the pipeline state.

## Status
**Not in MVP.** The current system sends and waits; Igor reads replies
manually in his Gmail.

## Phase 2 plan
1. Poll `users.messages.list` with `q='in:inbox newer_than:30d'` +
   match `threadId` against `agency_outreach_messages.thread_id`.
2. On match, update `reply_received_at` + `reply_content`.
3. Classify reply intent:
   - Interested → Telegram notification, no auto-reply
   - Not interested / opt-out keywords → write to `agency_opt_outs`
   - Bounce (mailer-daemon) → write to `agency_opt_outs` + flip
     `agency_outreach_messages.status='bounced'`
4. Follow-up cadence (phase 2+): one second email 7 days later if no
   reply, still under the 15/day cap.

## Tool (stub)
`tools/poll_replies.py` — to be built.
