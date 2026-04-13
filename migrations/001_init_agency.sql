-- Agency Hunter schema. Runs against the same self-hosted Supabase instance
-- as Job-search-automation. All tables prefixed `agency_` to avoid collisions.
--
-- The `profile` table already exists (created by the sibling project); it is
-- shared between both projects and stores Igor's parsed resume + per-project
-- JSONB config keys.
--
-- Run this in the Supabase SQL Editor, or `python tools/setup_db.py` will
-- apply it automatically when DATABASE_URL is set.

-- ---------------------------------------------------------------------------
-- agency_agencies — core record, one row per unique agency
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agency_agencies (
    id                  TEXT PRIMARY KEY,        -- canonical root domain, e.g. "acme-automation.com"
    name                TEXT NOT NULL,
    domain              TEXT NOT NULL,
    website_url         TEXT,
    country             TEXT,                    -- ISO-3166 alpha-2 when available
    city                TEXT,
    team_size           TEXT,                    -- "2-10", "11-50", etc.
    founded_year        INTEGER,
    specialization      JSONB,                   -- e.g. ["n8n","make","openai","zapier"]
    short_description   TEXT,
    enriched_data       JSONB,                   -- full Phase 3 extraction payload
    fit_score           INTEGER,                 -- 0-100, server-recomputed (never trust LLM math)
    fit_reasoning       TEXT,                    -- fit_summary from LLM
    flagged_issues      JSONB,                   -- pros/cons/red_flags structured blob
    fit_breakdown       JSONB,                   -- sub-score dict from classifier
    status              TEXT NOT NULL,           -- state machine
    discovered_at       TIMESTAMPTZ DEFAULT NOW(),
    last_enriched_at    TIMESTAMPTZ,
    last_classified_at  TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW(),
    updated_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agency_agencies_status    ON agency_agencies(status);
CREATE INDEX IF NOT EXISTS idx_agency_agencies_country   ON agency_agencies(country);
CREATE INDEX IF NOT EXISTS idx_agency_agencies_fit_score ON agency_agencies(fit_score DESC);


-- ---------------------------------------------------------------------------
-- agency_sources — provenance (many-to-one with agencies)
-- A single agency may be found via multiple channels across multiple runs.
-- Keeping a row per (agency, channel, source_url) preserves every lead's
-- provenance for debugging and confidence scoring.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agency_sources (
    id             BIGSERIAL PRIMARY KEY,
    agency_id      TEXT REFERENCES agency_agencies(id) ON DELETE CASCADE,
    channel        TEXT NOT NULL,                -- "apify_google_search", "clutch", "n8n_partners", ...
    source_url     TEXT,
    raw_payload    JSONB,                        -- whatever the scraper returned for this row
    discovered_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agency_sources_agency  ON agency_sources(agency_id);
CREATE INDEX IF NOT EXISTS idx_agency_sources_channel ON agency_sources(channel);


-- ---------------------------------------------------------------------------
-- agency_contacts — one row per discovered contact person at an agency
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agency_contacts (
    id                BIGSERIAL PRIMARY KEY,
    agency_id         TEXT REFERENCES agency_agencies(id) ON DELETE CASCADE,
    full_name         TEXT,
    role              TEXT,
    email             TEXT,
    email_status      TEXT,                       -- "scraped_visible", "guessed_pattern", "verified_mx", "invalid"
    email_confidence  INTEGER,                    -- 0-100
    linkedin_url      TEXT,
    source            TEXT,                       -- "site_scrape", "pattern_guess"
    is_primary        BOOLEAN DEFAULT FALSE,
    created_at        TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agency_contacts_agency ON agency_contacts(agency_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agency_contacts_email
    ON agency_contacts(email) WHERE email IS NOT NULL;


-- ---------------------------------------------------------------------------
-- agency_outreach_messages — drafts, approvals, sends, replies
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agency_outreach_messages (
    id                    BIGSERIAL PRIMARY KEY,
    agency_id             TEXT REFERENCES agency_agencies(id) ON DELETE CASCADE,
    contact_id            BIGINT REFERENCES agency_contacts(id) ON DELETE SET NULL,
    to_email              TEXT NOT NULL,
    from_email            TEXT NOT NULL,
    subject               TEXT NOT NULL,
    body                  TEXT NOT NULL,
    template_id           TEXT,                   -- e.g. "cold_v1"
    personalization       JSONB,                  -- {hook_type, hook_reference, personalized_opener}
    thread_id             TEXT,                   -- Gmail thread id for reply matching
    message_id            TEXT,                   -- RFC 2822 Message-ID
    status                TEXT NOT NULL,          -- draft, ready_to_send, approved, sent, bounced, replied, rejected, edited
    revision              INTEGER DEFAULT 0,      -- bumped by /edit regeneration
    edit_feedback         TEXT,                   -- free-text feedback from last /edit
    sent_at               TIMESTAMPTZ,
    reply_received_at     TIMESTAMPTZ,
    reply_content         TEXT,
    created_at            TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agency_outreach_agency  ON agency_outreach_messages(agency_id);
CREATE INDEX IF NOT EXISTS idx_agency_outreach_status  ON agency_outreach_messages(status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agency_outreach_message_id
    ON agency_outreach_messages(message_id) WHERE message_id IS NOT NULL;


-- ---------------------------------------------------------------------------
-- agency_discovery_runs — per-channel run history for observability
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agency_discovery_runs (
    id                BIGSERIAL PRIMARY KEY,
    channel           TEXT NOT NULL,
    started_at        TIMESTAMPTZ DEFAULT NOW(),
    finished_at       TIMESTAMPTZ,
    status            TEXT,                       -- running, success, error
    candidates_found  INTEGER DEFAULT 0,
    new_agencies      INTEGER DEFAULT 0,
    error_message     TEXT,
    metadata          JSONB                       -- channel-specific knobs (queries, countries, ...)
);

CREATE INDEX IF NOT EXISTS idx_agency_discovery_runs_channel ON agency_discovery_runs(channel);


-- ---------------------------------------------------------------------------
-- agency_opt_outs — hard block list for outreach
-- Populated by:
--   1. Inbound replies containing opt-out language ("not interested", "unsubscribe", ...)
--   2. Manual entries via Telegram bot
--   3. Bounces
-- Checked before every send.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS agency_opt_outs (
    email         TEXT PRIMARY KEY,
    opted_out_at  TIMESTAMPTZ DEFAULT NOW(),
    source        TEXT                            -- "reply_stop", "manual", "bounce"
);
