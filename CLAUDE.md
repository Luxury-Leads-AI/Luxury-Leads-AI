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
SUPPORT_EMAIL=         # Optional; the address shown on the home, privacy and terms pages (falls back to SMTP_EMAIL, then DEFAULT_SUPPORT_EMAIL)
REPLY_TO_EMAIL=        # Optional; where a client's reply lands (falls back to SUPPORT_EMAIL)
EMAIL_FROM_NAME=       # Optional; the name beside the From address (default "Luxury Leads AI")
SITE_BASE_URL=         # Optional; the address the PUBLIC pages claim as canonical, and the one in robots.txt and the sitemap. Falls back to PUBLIC_BASE_URL. Set it to the plain domain visitors arrive on (https://ailuxuryleads.com) - PUBLIC_BASE_URL stays on the app subdomain because the widget's script URL must never move
BUSINESS_ADDRESS=      # Optional; the registered address in the public footer. Nothing is shown until it is set
SHOW_STATS=            # Optional; "true" to show the four figures across the home page. Off by default - a tile renders only when its STAT_n_VALUE is also set
STAT_1_VALUE=          # e.g. 87% - leave blank and that tile stays hidden
STAT_1_LABEL=          # default "Of visitors captured"
STAT_2_VALUE=          # default label "Always working"
STAT_2_LABEL=
STAT_3_VALUE=          # default label "More qualified leads"
STAT_3_LABEL=
STAT_4_VALUE=          # default label "Setup time"
STAT_4_LABEL=
```

**Live configuration (from 2026-10-05):** `PUBLIC_BASE_URL=https://app.ailuxuryleads.com`,
`SMTP_EMAIL=SUPPORT_EMAIL=REPLY_TO_EMAIL=support@ailuxuryleads.com`. The
`onrender.com` address still resolves and is still the fallback, and
`static/widget.js` works out its own origin from its `src`, so client embeds
made before the move keep working. The step-by-step setup (Hostinger DNS,
Render custom domain, Brevo domain authentication, security settings) is the
Claude Doc "Domain and email setup - ailuxuryleads.com".

**The public pages are what a payment processor reads.** All eight
(`/`, `/pricing`, `/terms`, `/privacy-policy`, `/refund-policy`, `/signup`,
`/signup/solo`, `/signup/agency`) include `templates/_public_footer.html` -
one copy of the legal links, the support address **as readable text** (a bare
`mailto:` does nothing for a reviewer with no mail client, which is how this
was found), and Paddle named as Merchant of Record. Styles live in
`static/theme.css` under `.site-footer*`; `BUSINESS_NAME`/`BUSINESS_ADDRESS`
render only when set, so nothing invented is published. Underscore-prefixed
templates are partials: `test_mobile_layout`, `test_theme` and the viewport
check in `test_agent_routing_and_stages` skip them, since a fragment has no
`<head>`.

`tests/test_public_pages.py` is the guard, and it checks **agreement rather
than wording**: every page links to all three policies and the links resolve;
every page shows the support address outside an href; every page and each
policy's own text names the Merchant of Record; the prices shown match
`TIER_LIMITS`; and - the one that caught the original bug -
`test_every_page_promises_the_same_trial_length` scans every template for a
"N-day trial" claim and fails unless they all agree with
`provision_agency(trial_days=...)`. Terms said 7 while the code and four other
pages said 14. Terms also promised an automatic charge the code cannot make
(no card is taken at signup) and the privacy policy named SendGrid, which was
replaced by Brevo; both are now asserted against.

**Addresses are settings, not literals.** The support address used to be typed
into `index.html`, `privacy.html` and `terms.html`; it is now the
`support_email` Jinja global, and `tests/test_domain_and_email.py` walks the
repository and fails if the owner's personal address reappears anywhere.
`send_email_brevo()` sets `replyTo` from `REPLY_TO_EMAIL` on every send - the
whole of the client-feedback mechanism until an in-app form exists. `/about-bot`
is the page the acquisition fetcher's User-Agent names: public, no login, says
how to stop the bot by email or `robots.txt`; a test resolves the URL out of
`fetch.USER_AGENT` and fails if it 404s. `fetch._public_base_url()` reads
`PUBLIC_BASE_URL` from the environment (never from `app.py` - the package does
not import it), so the bot's calling card follows the domain.

**One date, in one place.** The three policy pages each carried a hand-typed
"Last updated" line and all three had gone stale. The date is now
`POLICY_UPDATED` in `app.py`, exposed as the `policy_updated` Jinja global;
`test_the_date_is_not_written_into_the_templates_any_more` fails if a literal
date comes back. Changing the date alone proves nothing, so
`test_a_policy_cannot_change_without_somebody_noticing_the_date` fingerprints
each policy and compares it with a hash recorded in the test. **Edit a policy's
wording and that test fails on purpose** - move `POLICY_UPDATED` forward and
paste the new hash the failure prints. The fingerprint hashes only the
`<body>`, with `{% ... %}` statements stripped, so adding a shared partial or
changing the head is not mistaken for a change to the policy itself.

**The footer has to be told to take its own line.** `signup.html`,
`signup_solo.html` and `signup_agency.html` centre their card with
`body { display: flex }`. Including the shared footer put a second flex item in
that row, so the footer rendered *beside* the card. The fix is
`flex-direction: column` on those three bodies plus `margin-block: auto` on
`.box` to keep the vertical centring `align-items` used to give, and
`width: 100% / align-self: stretch / box-sizing: border-box` on `.site-footer`.
`.box` also gained `box-sizing: border-box`: `width: 100%` with `padding: 40px`
measured 416px on a 390px phone, which was a sideways scrollbar on every signup
page since long before the footer existed. The tier pages had no narrow
breakpoint at all; they now trim padding under 480px. Five tests in
`test_public_pages.py` hold all of this, and the check that actually proved it
was Chromium at 1366 and 390 asserting `scrollWidth <= clientWidth` and the
footer's top below the card's bottom.

**SEO and GEO live in one partial.** `templates/_seo_head.html` is included by
all eight public pages and writes the description, canonical URL, robots
directive, Open Graph and Twitter cards, and - on every page - a JSON-LD
`@graph` of Organization, WebSite and SoftwareApplication with an `Offers`
entry per visible tier. The offers are generated from `visible_tiers()` (the
`offer_tiers` Jinja global), so a structured price cannot drift from a sellable
one; `test_the_structured_prices_are_prices_somebody_can_buy` compares the two
sets. Canonical URLs are built from `PUBLIC_BASE_URL`. `/robots.txt` and
`/sitemap.xml` are generated from `INDEXABLE_PAGES`; the legal pages carry
`noindex` and stay out of the sitemap, and a test walks the sitemap asking the
app for each URL so it can never list a dead page. Descriptions must be unique
across pages (`test_no_two_pages_claim_the_same_description`).

**Two addresses, two jobs.** `PUBLIC_BASE_URL` is where the app lives: every
widget `src`, login link and email URL points at it, so it must never move -
it is `https://app.ailuxuryleads.com`. `SITE_BASE_URL` is the address the
marketing pages *claim*, and it is the plain domain visitors actually arrive
on. They were the same value for one deploy, which meant `robots.txt`
advertised a sitemap on the app subdomain and every canonical pointed away
from the host Google had just crawled - an invitation to drop the marketing
pages. `SITE_BASE_URL` falls back to `PUBLIC_BASE_URL` when unset, so an
untold deploy keeps working; `test_site_base_url_falls_back_rather_than_breaking`
holds that fallback in place. Canonicals, Open Graph URLs, the JSON-LD `@id`s,
`robots.txt` and `sitemap.xml` all use `SITE_BASE_URL`; nothing else does.

**The feature pages come from one list.** `FEATURES` in `app.py` is the only
place a feature is described. The home-page tiles, `/features`, and each
`/features/<slug>` page are all rendered from it, and every entry is added to
the sitemap automatically. That is deliberate: six hand-written tiles on the
home page is how the site ends up advertising something no page explains.
`test_the_feature_index_and_the_home_tiles_cannot_drift` compares the two sets
of links, and `test_every_feature_has_everything_a_page_needs` fails on an
entry missing any field. Each feature's `answers` are published twice - read
on the page, and as `FAQPage` JSON-LD - and
`test_every_feature_page_publishes_its_questions_as_data` asserts the two say
the same thing, because markup that tells a machine something it does not tell
a reader is cloaking.

**An inline element with a border fractures when it wraps.** `.trial-note span`
on the pricing page was `inline` with a border and a `border-radius`. One line
wide it looks like a pill; wrapped to three lines on a phone the browser draws
that border round *each line fragment*, and the outlines cut through the words.
Anything with both a border and a radius needs a `display`.
`test_no_bordered_pill_on_a_public_page_is_left_inline` sweeps the public
templates for the same mistake (admin screens are exempt - a fixed-width backup
code cannot wrap).

**`theme.css` draws a border round `.box`; the page has to supply the padding.**
`signup.html` set none, so the two plan cards pressed against that border on
every screen size. The tier pages had `padding: 40px` from their own style
blocks and looked fine, which is why it went unnoticed.

**Claims have to be checkable.** "87% of visitors captured", "3x more qualified
leads" and "Join agencies generating 50+ qualified leads per month" were on the
home page before there was a customer to measure. The tiles are kept but
switched off: `SHOW_STATS` plus `STAT_<n>_VALUE`/`STAT_<n>_LABEL`, and
`visible_stats()` renders a tile only when the switch is on *and* that figure is
set. `test_no_public_page_claims_a_result_it_cannot_show_its_working_for` fails
if any of those phrases comes back.

The flag alone was not enough. The first time it was switched on, the home
page published **"e.g. 72%"** - the example from the setup instructions,
pasted in whole. So `looks_like_a_placeholder()` now rejects any figure
containing `e.g`, `example`, `your `, `xx`, `<`, `tbd` or `todo`, or longer
than 10 characters (a real figure is `87%`, `24/7`, `2.1x`; anything longer
is prose and breaks the row as well as the claim). A rejected value hides
its tile and prints a warning to the Render log - **it is not stripped and
published**, because the number behind the example was not measured either.
The length rule applies to figures only, not to labels, which are phrases.
Also note what these tiles are NOT: static text from the environment, not
anything computed. Nothing on the home page reads the database. The pricing page separately sold "10
languages" while `widget.js` carries 9;
`test_every_page_agrees_on_how_many_languages_we_speak` reads the count out of
the widget and checks every template and every rendered page against it - the
same agreement test as the trial length, for the same reason.

**`guides.py` is content, not a second backend.** It imports nothing, holds no
logic and touches no database - every route, model and AI call is still in
`app.py`. It exists only because 13 screen guides, 3 role walkthroughs and 20
FAQs are about 700 lines of prose, and `app.py` is already 320 KB. Three
things come out of it: `GUIDES`, keyed by **Flask endpoint name**;
`ROLE_GUIDES`; and `FAQS`.

The endpoint key is the whole trick. `inject_screen_guide()` is a context
processor that looks up `request.endpoint`, so a template gets its guide by
including `_help_panel.html` and nothing else - no route has to remember to
pass one in. A screen with no entry renders **no button at all** rather than an
empty panel (`test_a_screen_with_no_guide_shows_no_button`).

**The guide tests are agreement tests.** A wrong guide is worse than no guide:
it sends a customer hunting for a control that is not there, and they conclude
the product is broken. `test_every_control_a_guide_names_is_on_that_screen`
takes all 96 named controls and looks for their words in the template that
renders that screen. **Rename a button and that test fails, naming the guide
and the exact words it could not find.** Controls that are a group rather than
one label (a row of filter chips, the status dropdown) are listed in
`GROUP_SAMPLES` in the test with the token to search for instead - add a
control of that shape and you add a line there. `SCREEN_TEMPLATES` maps each
endpoint to the template it renders, written out rather than inferred, so a
route that starts rendering something else is caught too.

**And the reverse, which matters more.** The agreement test asks "is
everything the guide names still on the screen?" It cannot catch the
opposite - a screen grows a control and the guide stays quiet, every test
green. That is not hypothetical: it is how the search boxes on **four**
screens (leads, agent dashboard, appointments, listings) and the agent
dashboard's "Notes from your owner" panel went unwritten in the first pass.
`test_no_control_on_a_screen_goes_unexplained` collects every
`onclick`/`onchange`/`oninput`/`onsubmit` handler on a guided screen and
requires each to be either mapped in `HANDLER_COVERED_BY` to the guide entry
that covers it, or matched by `PLUMBING` (opening and closing a dialog, the
machinery behind a table-row click - things a reader never looks up). Add a
button and the test fails, naming it, until you make that decision. Two more
tests keep the map honest in both directions: every control it points at has
to exist in the guide, and every handler it names has to still exist on the
screen.

**The panel is injected into 13 pages that each have their own CSS**, so it
isolates itself: every class and id is prefixed `hp-`, and it resets
`text-align` and the `width` of its own buttons. That last one is not
hypothetical - `owner_login.html` has `button { width: 100% }` for its submit
button, which made the panel's close button eat the whole header and squeeze
the title into a two-character column. Four tests hold the isolation
(`test_the_panel_styles_cannot_collide_with_a_page`,
`test_the_panel_ids_are_prefixed_too`,
`test_the_panel_buttons_set_their_own_width`,
`test_the_panel_does_not_inherit_a_page_that_centres_everything`).

That `button { width: 100% }` had a sibling: **six** pages (`owner_login`,
`agent_login`, `forgot_password`, `reset_password`, `agency_profile`,
`agent_profile`) also set `button { padding: 14px }`. `.hp-open` was a fixed
46px circle with no `padding: 0` and no `box-sizing`, so on those six pages it
rendered 74px wide with the glyph shoved down and right. The rule now states
its entire box model, the glyph is centred with flex rather than a
`line-height` a page can override, and the glyph itself is an **inline SVG** -
a text `?` inherits `font-family`, `font-size`, `letter-spacing` and
`text-transform` from whatever page it lands on, an SVG inherits only
`currentColor`. `test_the_help_button_states_its_whole_box_model` and
`test_the_glyph_is_centred_by_the_box_not_by_the_font` both fail against the
old CSS; the Chromium check measures the glyph's centre against the button's
on every affected page and wants them within 1.5px.

The button's look comes from the same three layers the public cards use: the
brand gradient, a 1px inner highlight ring, and a violet glow under an ambient
shadow. It is 54px (50px on a phone - above the 44px touch minimum, which
`test_the_button_is_big_enough_to_hit_with_a_thumb` holds), clears a notched
phone's gesture bar with `env(safe-area-inset-bottom)`, and drops its
transforms under `prefers-reduced-motion`.

**Who sees which screen matters.** Each guide carries a `role`: `owner`,
`agency` (Agency plan only), `agent` or `anyone`. The role guides are checked
against it - a solo owner is never shown the Agents page they do not have, an
agent is never shown Clear All Leads, and the agency owner guide has to cover
every owner screen that exists (`test_a_solo_owner_is_not_shown_the_team_screens`,
`test_an_agent_guide_never_shows_an_agent_an_owner_screen`,
`test_the_agency_owner_guide_covers_everything_an_owner_has`).

`/guide` and `/guide/<role>` are public and indexed - somebody deciding
whether to buy can see what they would be operating, and the FAQs are
published as `FAQPage` data as well as read on the page. `guide_role.html`
carries a print stylesheet; its `body` rule uses `background: none` rather
than `#fff`, because a literal light colour on `body` is what
`test_theme.py::test_the_body_never_keeps_a_light_background` exists to catch.

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
the call. The OpenAI provider posts to `/v1/responses` through
`fetch.request()` (the pinned SDK predates that endpoint) and, with `web_search_tool_type = auto`, tries
both tool spellings and remembers the one the API accepted - a rename at
OpenAI costs a setting, not a deploy. The OSM provider resolves a city to a
bounding box via Nominatim (cached in `acq_setting` as `osm_bbox:<market>`)
then queries Overpass for `office=estate_agent`; both carry a User-Agent that
says who we are.

**Overpass is a list, not an address.** The main public server
(`overpass-api.de`) refuses connections from cloud hosting - it blocked whole
AWS and Azure ranges in October 2025 after being abused from them - and Render
is cloud hosting, so the first real search got `[Errno 111] Connection refused`
from both of its IPv4 addresses. `DEFAULT_OVERPASS_URLS` is tried in order
(private.coffee first, overpass-api.de second), `osm_overpass_url` overrides it
with a comma-separated list, and the one that answered is remembered in
`osm_overpass_last_good` and tried first next time. A refusal, a timeout, a 429 or a 5xx
moves to the next server; one request per server, never disguised, never
retried in a loop. Each server gets `PER_SERVER_SECONDS` (8), not the whole
`SEARCH_BUDGET_SECONDS` (20) - a slow first server used to leave nothing for
the second - and the Overpass-side `[timeout:N]` is set to the same number so
a volunteer server stops working when we stop waiting.

**Busy is not closed** (`is_temporary`). A refusal or an unreachable address
is that server's answer and will be the same in five minutes; a timeout, a 429
or a 5xx is worth repeating. When a search fails for a temporary reason the
`discover` handler RAISES, so the queue's own backoff (30s, 5min, 30min) tries
it again instead of writing the city off; a refusal returns `{'error': ...}`
and the job goes amber straight away.

**When no Overpass server answers at all**, `OSMDiscovery.nominatim_candidates()`
asks Nominatim's own POI search (`q=estate agent`, `bounded=1` on the city's
viewbox, `extratags=1` for the website) and uses that. It finds fewer and caps
at 40, but Nominatim answers from Render and Overpass does not. Off with
`osm_nominatim_fallback=off`; the phrase is `osm_poi_query`.

**A 429 is obeyed, not pushed through.** Nominatim's search answered 429 from
Render within a few jobs (its policy is one request a second, no bulk).
`discovery.pause_poi_search()` sets `osm_poi_pause_until` an hour ahead and
`poi_search_paused()` skips the fallback until then, and
`runner.RATE_LIMIT_BACKOFF_SECONDS` (15min, 30min, 1h) replaces the ordinary
backoff whenever `was_rate_limited()` matches the error. Hammering through a
rate limit is how a free service stops answering at all.

**`tools/osm_agencies.py`** runs the same Overpass query from a machine that
is not cloud hosting - standard library only, one city per run, 1 req/s, a
User-Agent that says it is run by hand - and prints `https://domain, Name`
lines for the Prospects paste box. It applies the same shared-host and
duplicate rules as `services/prospects.py`. This is the free route that
actually works; `discovery_enabled` now ships with `openai` in it because a
source that cannot answer is not a source. `osm_overpass_url` and `osm_nominatim_url` are on the Settings screen,
because the error message tells the operator to change them there.

**`services/fetch.py` is the only code that opens a connection to another
machine.** It resolves the name first and refuses every address that is not
public (private, loopback, link-local, multicast, reserved, and
169.254.169.254 by name), then connects to the address it checked with the
`Host` header set and `sni_hostname` for TLS, so DNS rebinding does not get a
second answer. `get()` is for strangers' websites: http and https only, at
most 3 redirects each re-checked, 10s timeout, 2 MB cap, HTML only, no
cookies, no JavaScript, robots.txt obeyed, one request per second per host. A
CAPTCHA or bot wall is reported, never solved. `request()` is for services we
chose (OpenStreetMap, OpenAI): same address handling, no robots check.

**Every address is tried, IPv4 first** (`order_addresses`). A name usually has
several addresses and a container does not necessarily have a route to all of
them; `socket.create_connection` reports only the *last* error, which is how
the first live search came back as nothing but "ConnectError: [Errno 101]
Network is unreachable". `send()` tries up to `MAX_ADDRESS_ATTEMPTS` (3)
addresses inside `REQUEST_BUDGET_SECONDS` (20) and raises `Unreachable`
carrying every address and what it said, so the message distinguishes "IPv6
has no route" from "nothing gets out". Trying more addresses costs time, so
every caller is bounded: `site_answers` gets 18s for both schemes, the OSM
provider 12s for Nominatim and 15s for Overpass - a search cannot outlast
gunicorn's 30-second limit, and `tests/acquisition/test_connection.py` fails
if those add up to more than 27.

**A job that returns `{'error': ...}` is marked `problem`, not `done`**
(`runner.problem_in`). It ran and reported bad news: no retry, amber in the
queue, `last_error` filled, and "Try again" offered. A green tick over
"Network is unreachable" is how a dead engine goes unnoticed for a week.
`/owner/acquisition/connection` ("Check the connection" on Jobs) opens each
address of each configured service on its own and reports one of four
verdicts - reachable, answers-but-blocked, refused, no-route, name-not-found -
inside a 20-second budget, and it tests every Overpass server in the list. A
refusal is told apart from silence on purpose: it means the service is up and
turning this server away.

**Research (Phase 3).** One `research` job per prospect, queued from the
prospect screen or "Research the new ones" on Prospects. `read_site()` opens
the home page then up to `research_pages` (5) more chosen by
`services/research.py:page_links()` - contact, about, listings, services, one
per kind, same domain only. `facts_from_markup()` then learns for free what
the markup states: generic emails (own domain only), `tel:` numbers, wa.me
links, a chat widget by its own script (`WIDGETS`), a contact form, a booking
link, `lang`/`hreflang`. Those are written `verified`. One `ai.ask_json()` pass
over the already-fetched text adds the judgement calls - luxury, price band,
property types, offices, one line - written `inferred` with the model as
`extractor`. **The markup wins**: an AI language list is only stored when the
rules found none.

Every fact carries `source_url`, and `record_fact()` deletes the prior row for
that field first, so researching twice updates rather than piles up.
**Only generic inboxes are collected** (`research.is_generic`) - a named
person's address is personal data with a duty of care, and the engine does not
take it at all; what it does keep gets `personal_data_expires_at` from
`retention_days_uncontacted`. Money: `research_cost_cap_usd` per prospect
(checked against `acq_cost` rows with `purpose like 'research%'`) on top of the
monthly budget, and `research_ai=off` turns the paid half off entirely.
`RESEARCH_BUDGET_SECONDS` (22) bounds the whole job; a site that times out
raises so the queue retries it, a site whose robots.txt says no is marked
`needs_human` and not retried.

**The gate.** `compliance.can_contact()` checks the kill switch, the
prospect's do-not-contact flag, the market's legal status (only `verified`
allows email) and the suppression list. `acq_suppression` deliberately has no
foreign key: an opt-out outlives the prospect row it came from.

**Screens.** `/owner/acquisition/` (Today, with the Run button and the budget),
`markets` (the registry, where a legal status is changed and logged),
`prospects` (paste websites, or queue a search; the canonical domain is the identity),
`jobs`, `connection`, `costs`, `settings`, `audit`. All are behind `session['super_admin']`,
and every form carries a CSRF token checked in the blueprint's
`before_request`; session cookies are `SameSite=Lax` (and `Secure` on Render).

On Render, `SECRET_KEY` is mandatory: if it is missing or still the `'change-this-in-production'` fallback, `app.py` raises at import and the deploy fails with a message saying so (`secret_key_problem()`, keyed off Render's own `RENDER=true`). Locally the fallback still works.

## Architecture

**Single-file backend** — all Flask routes, DB models, AI logic, and email are in `app.py`. There are no blueprints. The one other module, `guides.py`, is pure content (the user guides as data) with no imports, no logic and no database access; `app.py` reads it and owns every route.

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
