# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Run locally:**
```bash
python app.py
```

**Install dependencies:**
```bash
pip install -r requirements.txt
```

**Run with gunicorn (production-style):**
```bash
gunicorn app:app --bind 0.0.0.0:10000
```

**Run tests:**
```bash
pip install pytest
pytest tests/
```
Tests live in `tests/` and import `app.py` directly (no test client, no live server). `tests/conftest.py` sets dummy `OPENAI_API_KEY`/`SECRET_KEY` env vars and a throwaway temp-file SQLite `DATABASE_URL` *before* importing `app`, since `app.py` does real setup work (env validation, DB connection, migrations) at import time — never point tests at the real database or a real API key. Coverage so far is deliberately narrow: pure/DB-backed logic functions that don't call OpenAI (e.g. `detect_location`, `is_slot_within_booking_window`), not the full `/chat` endpoint. There is no linting configuration.

## Environment Variables

Required in `.env`:
```
OPENAI_API_KEY=
BREVO_API_KEY=
SMTP_EMAIL=
SMTP_PASSWORD=
SECRET_KEY=
DATABASE_URL=          # Optional; defaults to SQLite (luxury_leads.db)
SUPER_ADMIN_PASSWORD=  # Gates /super-admin-login (/owner, /agencies, /delete-agency)
SHOW_TIER_3=           # Optional; "true" to re-show the Corporation tier on /pricing and /signup
PUBLIC_BASE_URL=       # Optional; the public address used in every emailed link and in the embed code (default https://luxury-leads-ai.onrender.com)
SUPER_ADMIN_PASSWORD_HASH=  # Preferred over SUPER_ADMIN_PASSWORD; make one with `python tools/make_admin_hash.py`
SMOKE_TEST_TOKEN=      # Optional; with header `X-Smoke-Test: <token>`, /chat answers from a fixed string instead of calling OpenAI
```

## The acquisition engine

`acquisition/` is a separate package in this repository that finds and wins
new agencies. It shares the database with the SaaS and **never imports
`app.py`** - `tests/acquisition/test_engine_foundation.py` fails if it ever
does, because that would be a circular import and would load the whole app
twice under `python app.py`. Everything it needs arrives through
`acquisition.init_app(app, db, saas=SaaSServices(...))` at the bottom of
`app.py`: the database, `provision_agency`, `is_entitled`, `send_email_brevo`,
the OpenAI client and `PUBLIC_BASE_URL`.

```
acquisition/
├── models.py        the 19 acq_* tables, built inside define(db)
├── settings.py      mode, budgets, caps, kill switches (acq_setting)
├── compliance.py    can_contact(): the one gate before anything is sent
├── jobs/            runner.py (claim one, run it, record it), registry, handlers/, worker.py
├── providers/       discovery.py: manual, OpenAI web search, OpenStreetMap (Places later)
├── services/        ai.py (every OpenAI call + cost + budget), fetch.py (the only code that opens a stranger's site), prospects.py (one row per company)
├── admin/routes.py  the /owner/acquisition screens, behind the super admin session
└── static/acquisition/runner.js   the browser-driven queue loop
```

**The queue.** Render's free plan has no worker, so an open dashboard tab is
the worker: `runner.js` posts to `/owner/acquisition/jobs/run-next`, which
runs jobs for at most `runner.BUDGET_SECONDS` (20) and returns, safely inside
gunicorn's 30-second limit. `claim_one()` uses `FOR UPDATE SKIP LOCKED` on
Postgres so two tabs never take the same job; a claim holds a 90-second lock,
and `release_stale_locks()` returns anything whose owner vanished. Jobs carry
an `idempotency_key` (so the same work is not queued twice) and retry three
times with a growing wait. Growth mode changes nothing but who calls it:
`python -m acquisition.jobs.worker`.

**Money.** `services/ai.py` is the only code that calls OpenAI. Every call is
costed from the token counts the API reports into `acq_cost`, model prices
live in settings (not in code), and `can_spend()` refuses *before* a call once
the monthly budget is gone. Outside text is always passed as untrusted data,
the model gets no tools, and answers are parsed as JSON or thrown away.

**Discovery (Phase 2).** Two jobs, deliberately split. `discover` asks one
source for candidates in one market; `confirm_candidate` opens each
candidate's website and only then creates a prospect - an AI search can
produce a convincing agency that does not exist, and one request settles it.
A site that answers but blocks robots, or shows a human check, is kept and
flagged `needs_human` rather than worked around. Paid sources are refused
twice over: `web_search_cap_month` counts `acq_cost.units` for
`purpose='discovery_search'`, and `ai.can_spend()` checks the budget before
the call. The OpenAI provider posts to `/v1/responses` over httpx (the pinned
SDK predates that endpoint) and, with `web_search_tool_type = auto`, tries
both tool spellings and remembers the one the API accepted - a rename at
OpenAI costs a setting, not a deploy. The OSM provider resolves a city to a
bounding box via Nominatim (cached in `acq_setting` as `osm_bbox:<market>`)
then queries Overpass for `office=estate_agent`; both carry a User-Agent that
says who we are, and `osm_nominatim_url` / `osm_overpass_url` point at a
self-hosted copy if we ever need one.

**`services/fetch.py` is the only code that opens a URL somebody else chose.**
It resolves the name first and refuses every address that is not public
(private, loopback, link-local, multicast, reserved, and 169.254.169.254 by
name), then connects to the address it checked with the `Host` header set and
`sni_hostname` for TLS, so DNS rebinding does not get a second answer. http
and https only, at most 3 redirects each re-checked, 10s timeout, 2 MB cap,
HTML only, no cookies, no JavaScript, robots.txt obeyed, one request per
second per host. A CAPTCHA or bot wall is reported, never solved.

**The gate.** `compliance.can_contact()` checks the kill switch, the
prospect's do-not-contact flag, the market's legal status (only `verified`
allows email) and the suppression list. `acq_suppression` deliberately has no
foreign key: an opt-out outlives the prospect row it came from.

**Screens.** `/owner/acquisition/` (Today, with the Run button and the budget),
`markets` (the registry, where a legal status is changed and logged),
`prospects` (paste websites, or queue a search; the canonical domain is the identity),
`jobs`, `costs`, `settings`, `audit`. All are behind `session['super_admin']`,
and every form carries a CSRF token checked in the blueprint's
`before_request`; session cookies are `SameSite=Lax` (and `Secure` on Render).

On Render, `SECRET_KEY` is mandatory: if it is missing or still the `'change-this-in-production'` fallback, `app.py` raises at import and the deploy fails with a message saying so (`secret_key_problem()`, keyed off Render's own `RENDER=true`). Locally the fallback still works.

## Architecture

**Single-file backend** — all Flask routes, DB models, AI logic, and email are in `app.py`. There are no blueprints or separate modules.

**Multi-tenant via `agency_id`** — every `Agency` record is a customer. Each agency gets a custom AI assistant name and prompt. All `Lead` records are scoped by `agency_id`. The embeddable widget (`static/widget.js`) is deployed on the agency's own website and passes `data-agency="<id>"` to identify which agency's bot is running.

**Conversation memory is in-process** — `conversation_memory` and `session_timestamps` are plain Python dicts. Sessions are keyed by `{agency_id}_{md5(ip+user_agent)[:12]}` and expire after 30 minutes. This means memory is lost on server restart and does not work across multiple Render instances.

**Lead qualification flow** (in `app.py`):
1. Each chat message goes through `extract_lead_data()` (regex-based, no AI) to pull name/email/phone/budget from the conversation so far.
2. `is_lead_qualified()` fires when: email + name + budget are present AND ≥7 user messages have been sent.
3. On qualification: `generate_lead_summary()` calls GPT-4o-mini for a 2–3 sentence summary, `analyze_lead_quality()` scores 1–5, the `Lead` row is saved, and `send_lead_email()` notifies the agency via Brevo.
4. Duplicate leads are blocked by `(agency_id, email)` uniqueness check before insert.

**Objection handling** — `detect_objection()` pattern-matches user messages. If a match is found, the suggested response text is injected into the system prompt's tail (`objection_context`) rather than hardcoded into the reply. GPT decides whether to use it.

**Database migrations run at startup** — `app.py` uses SQLAlchemy `inspect()` to check existing columns and issues raw `ALTER TABLE` statements if columns are missing. No Alembic.

**Email** — Brevo (`send_email_brevo()`) is the only working email path on Render (Gmail SMTP is blocked, and the old SendGrid integration was replaced). The `BREVO_API_KEY` env var must be set or emails are silently skipped (logged, not raised).

**Widget delivery** — `static/widget.js` is a self-contained IIFE. It finds its server from the address it was loaded from (`new URL(document.currentScript.src).origin`), so a locally served widget talks to the local app and a custom domain needs no edit. `data-base-url` on the script tag overrides it; `https://luxury-leads-ai.onrender.com` is the last resort and the fallback if the derived address doesn't answer `/agency/<id>`. The widget shows an AI disclosure (header label plus a first line in the message list, localized by browser language: en, fr, es, pt, pt-BR, de, it, nl, ar) for EU AI Act Article 50.

**Public address** — every link `app.py` writes (emails, `LOGIN_URLS`, the check-in email) and the embed code in `admin.html` / `signup_*.html` read `PUBLIC_BASE_URL` (a Jinja global, `public_base_url`, for templates). The Render address appears exactly once, as `DEFAULT_PUBLIC_BASE_URL`; `tests/test_phase0_cycle1.py` fails if a second copy creeps in.

**Super admin security** — `/super-admin-login` checks the password against `SUPER_ADMIN_PASSWORD_HASH` with `check_password_hash`, falling back to `secrets.compare_digest` against the old plain `SUPER_ADMIN_PASSWORD` so a deploy can't lock anyone out (Render logs a reminder until the hash is set). When `AdminSecurity` row 1 has a confirmed TOTP secret, the password only sets `session['super_admin_pending']` and `/super-admin-2fa` asks for the 6-digit code; codes are accepted within ±30s and each 30-second slot is spent once (`last_step`), so a code can't be replayed. `AdminBackupCode` holds 8 one-time codes as hashes. `/super-admin-2fa/setup` (QR drawn locally with `qrcode`'s SVG factory - no outside service ever sees the key) confirms, regenerates codes, or turns 2FA off against a current code. `AdminAudit` + `record_admin_action()` log sign-ins, failures, 2FA changes and agency create/delete; `/super-admin-audit` shows the last 100.

**One door in, one entitlement answer** — `provision_agency()` is the only place an `Agency` row is created (`/create-agency` is a thin wrapper; pilot onboarding and Paddle checkout will call it too), and `is_entitled(agency)` is the only answer to "may this agency use the product?" (`has_dashboard_access()` is kept as an alias). Both exist for the acquisition engine, which must never write to SaaS tables directly.

**Smoke test** — with `SMOKE_TEST_TOKEN` set, a `/chat` POST carrying `X-Smoke-Test: <token>` returns a fixed reply after the agency lookup, so a new pilot's install can be checked end to end without an OpenAI call and without writing a lead or a conversation row.

**Rate limits** — Flask-Limiter with in-memory storage (right for one Render instance with one gunicorn worker; more instances would need a Redis-style store). Keyed on the first `X-Forwarded-For` entry, which Render sets to the real client (`client_ip()`). Limits: `/chat` 20/min and 200/hour, `/create-agency` 5/hour and 20/day (super admin exempt), `/owner-login` and `/agent-login` 10 per 15 min, `/super-admin-login` and `/super-admin-2fa` 5 per 15 min, `/forgot-password` 5/hour - POST only. The 429 handler answers in each caller's own shape (JSON `reply` for the widget, JSON `error` for signup, `?error=` redirect for logins). `tests/conftest.py` resets the counters before every test.

## Deployment

Hosted on Render. The `DATABASE_URL` env var on Render uses `postgresql://` which is rewritten to `postgresql+psycopg://` at startup to satisfy SQLAlchemy 2.x.

**Auth** — Agency owners log in at `/owner-login` with a real per-agency hashed password (`Agency.set_password`/`check_password`, Werkzeug); agents log in at `/agent-login` the same way. New agencies/agents get a random one-time password generated with `secrets.token_urlsafe`, returned once in the API response — never a shared hardcoded default. Both owners (`/change-owner-password/<agency_id>`) and agents (`/change-agent-password/<agent_id>`) can rotate their own password from their dashboard; an owner or the super admin can also reset an agent's password. Locked-out users use `/forgot-password` (GET/POST, email-based, works for both Agency and Agent accounts off one shared design) and `/reset-password/<token>` — a `secrets.token_urlsafe(32)` token with a 1-hour expiry stored on `reset_token`/`reset_token_expires`, single-use, and the confirmation message never reveals whether the submitted email actually matched an account. The Super Admin panel (`/owner`, `/agencies`, `/delete-agency/<id>`) is gated behind `/super-admin-login`, checked against the `SUPER_ADMIN_PASSWORD` env var, with `session['super_admin']` as the guard; `/agencies` also returns each agency's tier/subscription/trial status and lead/appointment/agent counts, and `/platform-stats` feeds the overview cards at the top of `owner.html` (total agencies by tier, active/expired trials, paying-agency count, platform-wide totals).

**Profile self-service** — `/agency-profile/<id>` (owner) and `/agent-profile/<id>` (agent, or their owner/super admin) let accounts edit their own contact/business details (name, email, WhatsApp, AI assistant name, max-viewings-per-slot for agencies) via `/update-agency-profile/<id>` and `/update-agent-profile/<id>`. Agent email uniqueness is enforced within the owning agency only, matching `/add-agent`'s existing rule — see the known issue below.

**Tier visibility** — `SHOW_TIER_3` (env var, default off) controls only whether `/pricing` and the `/signup` chooser mention the Corporation tier; `TIER_LIMITS` and every `agency.tier in [...]` backend gate are unaffected, so a Corporation-tier agency can still be created manually (e.g. via `/create-agency`) regardless of the flag. Signup itself is a two-step flow: `/signup` is a chooser page linking to dedicated `/signup/solo` and `/signup/agency` forms, each posting to `/create-agency` with its tier hardcoded.

**Post-appointment loop** — once an `Appointment`'s date/time (parsed from `appointment_date_iso` + the fixed `TIME_SLOTS` strings like `"2:00 PM"`) is in the past with no outcome recorded, `process_appointment_checkins()` (wired into the existing `/send-followups` cron target, alongside the Day-1/Day-7 lead follow-up emails) emails the customer a link to the public `/appointment-feedback/<token>` page with three choices. "Wants to buy" notifies the assigned agent (or the owner) by email — but only when the customer's own click set it, not a manual entry. "Wants other options" embeds the same `widget.js` chat widget a first-time visitor gets, so re-qualification and re-booking reuse the existing AI rather than any new matching logic. Owners and agents can also record the same outcome manually via `/set-appointment-outcome/<id>` / `/agent-set-appointment-outcome/<id>`, for viewings discussed by phone.

**Every owner-scoped and agent-scoped route checks the session, not just the ID in the URL.** The helper `_owner_owns_agency(agency_id)` (true if `session['agency_id']` matches, or if `session['super_admin']` is set) guards essentially every route that reads or writes one agency's data: dashboards (`/admin`, `/agents/<id>`, `/appointments/<id>`, `/listings/<id>`, `/analytics/<id>`, `/agency-profile/<id>`), leads (`/update-lead-status`, `/add-lead-note`, `/delete-lead-note`, `/delete-lead`, `/clear-all-leads`, `/bulk-delete-leads`), appointments (`/reassign-appointment`, `/update-appointment-status`, `/set-appointment-outcome`, `/delete-appointment`, `/update-slot-capacity`, `/get-appointments-count`), listings (`/add-listing`, `/upload-listings`, `/toggle-listing-status`, `/delete-listing`, `/delete-all-listings`, `/get-listings`), `/export/<id>`, `/update-agency-profile/<id>`, and `/update-agency-webhook/<id>`. `/get-lead-detail/<id>` is the one exception with two valid callers (owner dashboard and agent dashboard both use it) — it's guarded by `_owner_owns_agency(...) or _agent_in_agency(...)`. The agent-side action routes (`/agent-update-lead-status`, `/agent-add-lead-note`, `/agent-update-appointment-status`, `/agent-add-appointment-note`, `/agent-set-appointment-outcome`, and the self/owner/super-admin-guarded `/agent-profile/<id>` + `/update-agent-profile/<id>`) identify the acting agent from `session['agent_id']`, never from a client-supplied `agent_id` in the request body (that used to be spoofable — anyone could claim any agent's ID). Routes that stay deliberately open: `/chat` and `/book-appointment` (the public widget), `/agency/<id>` (returns only an agency's name + assistant name, used by the widget), `/appointment-feedback/<token>` (public, token-gated, for customers with no platform account), `/paddle-webhook`, and `/send-followups` (meant to be hit by an external cron scheduler — no session to check, not yet secret-protected).

**Known issue, not yet fixed:** `/agent-login` looks up `Agent.query.filter_by(email=email, ...)` globally across all agencies, while `/add-agent` and `/update-agent-profile` only enforce email uniqueness *within* one agency. Two agents at different agencies sharing an email would make login ambiguous (first match wins). Candidate for a future hardening pass.
