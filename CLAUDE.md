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
```

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

**Widget delivery** — `static/widget.js` is a self-contained IIFE. It hard-codes `BASE_URL = "https://luxury-leads-ai.onrender.com"`. When developing locally, you must temporarily change this URL or use ngrok.

## Deployment

Hosted on Render. The `DATABASE_URL` env var on Render uses `postgresql://` which is rewritten to `postgresql+psycopg://` at startup to satisfy SQLAlchemy 2.x.

**Auth** — Agency owners log in at `/owner-login` with a real per-agency hashed password (`Agency.set_password`/`check_password`, Werkzeug); agents log in at `/agent-login` the same way. New agencies/agents get a random one-time password generated with `secrets.token_urlsafe`, returned once in the API response — never a shared hardcoded default. Both owners (`/change-owner-password/<agency_id>`) and agents (`/change-agent-password/<agent_id>`) can rotate their own password from their dashboard; an owner or the super admin can also reset an agent's password. Locked-out users use `/forgot-password` (GET/POST, email-based, works for both Agency and Agent accounts off one shared design) and `/reset-password/<token>` — a `secrets.token_urlsafe(32)` token with a 1-hour expiry stored on `reset_token`/`reset_token_expires`, single-use, and the confirmation message never reveals whether the submitted email actually matched an account. The Super Admin panel (`/owner`, `/agencies`, `/delete-agency/<id>`) is gated behind `/super-admin-login`, checked against the `SUPER_ADMIN_PASSWORD` env var, with `session['super_admin']` as the guard; `/agencies` also returns each agency's tier/subscription/trial status and lead/appointment/agent counts, and `/platform-stats` feeds the overview cards at the top of `owner.html` (total agencies by tier, active/expired trials, paying-agency count, platform-wide totals).

**Profile self-service** — `/agency-profile/<id>` (owner) and `/agent-profile/<id>` (agent, or their owner/super admin) let accounts edit their own contact/business details (name, email, WhatsApp, AI assistant name, max-viewings-per-slot for agencies) via `/update-agency-profile/<id>` and `/update-agent-profile/<id>`. Agent email uniqueness is enforced within the owning agency only, matching `/add-agent`'s existing rule — see the known issue below.

**Tier visibility** — `SHOW_TIER_3` (env var, default off) controls only whether `/pricing` and the `/signup` chooser mention the Corporation tier; `TIER_LIMITS` and every `agency.tier in [...]` backend gate are unaffected, so a Corporation-tier agency can still be created manually (e.g. via `/create-agency`) regardless of the flag. Signup itself is a two-step flow: `/signup` is a chooser page linking to dedicated `/signup/solo` and `/signup/agency` forms, each posting to `/create-agency` with its tier hardcoded.

**Post-appointment loop** — once an `Appointment`'s date/time (parsed from `appointment_date_iso` + the fixed `TIME_SLOTS` strings like `"2:00 PM"`) is in the past with no outcome recorded, `process_appointment_checkins()` (wired into the existing `/send-followups` cron target, alongside the Day-1/Day-7 lead follow-up emails) emails the customer a link to the public `/appointment-feedback/<token>` page with three choices. "Wants to buy" notifies the assigned agent (or the owner) by email — but only when the customer's own click set it, not a manual entry. "Wants other options" embeds the same `widget.js` chat widget a first-time visitor gets, so re-qualification and re-booking reuse the existing AI rather than any new matching logic. Owners and agents can also record the same outcome manually via `/set-appointment-outcome/<id>` / `/agent-set-appointment-outcome/<id>`, for viewings discussed by phone.

**Every owner-scoped and agent-scoped route checks the session, not just the ID in the URL.** The helper `_owner_owns_agency(agency_id)` (true if `session['agency_id']` matches, or if `session['super_admin']` is set) guards essentially every route that reads or writes one agency's data: dashboards (`/admin`, `/agents/<id>`, `/appointments/<id>`, `/listings/<id>`, `/analytics/<id>`, `/agency-profile/<id>`), leads (`/update-lead-status`, `/add-lead-note`, `/delete-lead-note`, `/delete-lead`, `/clear-all-leads`, `/bulk-delete-leads`), appointments (`/reassign-appointment`, `/update-appointment-status`, `/set-appointment-outcome`, `/delete-appointment`, `/update-slot-capacity`, `/get-appointments-count`), listings (`/add-listing`, `/upload-listings`, `/toggle-listing-status`, `/delete-listing`, `/delete-all-listings`, `/get-listings`), `/export/<id>`, `/update-agency-profile/<id>`, and `/update-agency-webhook/<id>`. `/get-lead-detail/<id>` is the one exception with two valid callers (owner dashboard and agent dashboard both use it) — it's guarded by `_owner_owns_agency(...) or _agent_in_agency(...)`. The agent-side action routes (`/agent-update-lead-status`, `/agent-add-lead-note`, `/agent-update-appointment-status`, `/agent-add-appointment-note`, `/agent-set-appointment-outcome`, and the self/owner/super-admin-guarded `/agent-profile/<id>` + `/update-agent-profile/<id>`) identify the acting agent from `session['agent_id']`, never from a client-supplied `agent_id` in the request body (that used to be spoofable — anyone could claim any agent's ID). Routes that stay deliberately open: `/chat` and `/book-appointment` (the public widget), `/agency/<id>` (returns only an agency's name + assistant name, used by the widget), `/appointment-feedback/<token>` (public, token-gated, for customers with no platform account), `/paddle-webhook`, and `/send-followups` (meant to be hit by an external cron scheduler — no session to check, not yet secret-protected).

**Known issue, not yet fixed:** `/agent-login` looks up `Agent.query.filter_by(email=email, ...)` globally across all agencies, while `/add-agent` and `/update-agent-profile` only enforce email uniqueness *within* one agency. Two agents at different agencies sharing an email would make login ambiguous (first match wins). Candidate for a future hardening pass.
