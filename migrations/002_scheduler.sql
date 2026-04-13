-- Migration 002 — send-window scheduler
--
-- Adds `scheduled_for` to agency_outreach_messages so approved drafts
-- can wait in a queue until their allotted send slot (Mon-Fri
-- 09:00-17:00 recipient-local, with random 5-20 min intervals).
--
-- A new status value `scheduled` is introduced: it sits between
-- `ready_to_send` (approved by user, awaiting slot) and `sent`
-- (delivered by Gmail). There is no schema constraint on status
-- values, so adding a new one requires only code changes — this
-- migration only adds the column + index.
--
-- Idempotent: safe to run multiple times.

ALTER TABLE agency_outreach_messages
    ADD COLUMN IF NOT EXISTS scheduled_for TIMESTAMPTZ;

-- Index for the scheduler's hot query: "give me rows ready to send now"
CREATE INDEX IF NOT EXISTS idx_agency_outreach_scheduled
    ON agency_outreach_messages(scheduled_for)
    WHERE status = 'scheduled';
