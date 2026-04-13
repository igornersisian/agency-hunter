# Workflow: Manage profile config

## Objective
Read and write agency-hunter config keys on the shared `profile` row.

## Background
The `profile` table is a single-row, JSONB-backed document shared with
`Job-search-automation`. Igor's parsed resume lives in `profile.parsed`.
Agency Hunter stores its own config keys in the same JSON so the bot
can read them without a separate config table.

## Config keys
| Key | Type | Default | Meaning |
|---|---|---|---|
| `agency_target_countries` | list[str] | 14 countries | ISO alpha-2 codes for discovery + classification |
| `agency_fit_threshold`    | int       | 65          | Minimum fit score for `qualified` status |
| `agency_send_cap`         | int       | 15          | Daily outreach send cap |
| `agency_sender_email`     | str       | (env var)   | FROM address (must be the authorized Gmail) |
| `agency_excluded_domains` | list[str] | defaults    | Domain blocklist for discovery |

## Reading
`tools/common/profile.py:get_agency_config()` merges profile keys over
defaults. Numeric defaults respect env vars
(`AGENCY_DAILY_SEND_CAP`, `AGENCY_FIT_THRESHOLD`) if the profile doesn't
override them.

## Writing
Telegram commands write back to `profile.parsed`:
- `/threshold N`      → `agency_fit_threshold`
- `/send_cap N`       → `agency_send_cap`
- `/countries CC,CC`  → `agency_target_countries`

Internally all three use `_save_profile_key(key, value)` in
`tools/telegram_bot.py`, which upserts the key on the newest profile row.

## Rule
**Never blow away `profile.parsed`.** Always merge. This row is shared
with the sibling project, which has its own keys (`custom_red_flags`,
excluded titles, target salary, etc.) — clobbering them would break
Job-search-automation.
