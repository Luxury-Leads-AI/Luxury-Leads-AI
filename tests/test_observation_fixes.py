"""Regression tests for the bugs Moaz found in live testing (Sept 2026).

Each test here reproduces something that actually went wrong in a real
chat transcript or on a real dashboard:

  1. A buyer asked for a 7-bedroom luxury home. The agency had 6-bed/7-bath
     estates in stock, but the AI was handed the 8 CHEAPEST listings
     (2-bed starters) and told the customer the largest thing available
     had 3 bedrooms. Relaxed bed/bath used to leave every listing tied on
     score, so the cheapest-first tiebreak decided the shortlist.
  2. The same customer was refused a second viewing on a day he'd already
     booked, even though he wanted a different time.
  3. He was asked for his email and his contact preference twice each.
  4. The Super Admin panel reported a paying agency when none had paid,
     and more agents than exist.
  5. Day-1/Day-7 follow-up emails only reached the agency owner, never the
     agent actually assigned to the lead.
"""
import uuid
from datetime import datetime, timedelta

import app as app_module

from tests.test_route_guards import make_agency, make_agent, make_lead


def _listing(agency_id, title, price, beds, baths, location="New York, NY", **kw):
    listing = app_module.Listing(
        agency_id=agency_id, title=title, location=location,
        price=price, price_numeric=price, bedrooms=beds, bathrooms=baths,
        status='available', listing_purpose='sale', **kw
    )
    app_module.db.session.add(listing)
    app_module.db.session.commit()
    return listing


def _msgs(*user_turns):
    return [{"role": "user", "content": t} for t in user_turns]


# ── 1. The 7-bedroom bug ─────────────────────────────────────────────

def test_big_home_request_surfaces_the_biggest_homes_not_the_cheapest():
    """The exact failure from the transcript: asking for 7 beds when the
    inventory tops out at 6 must show the 6-bed estates, never the 2-beds."""
    agency = make_agency()
    cheap = [_listing(agency.id, f"Starter Home {i}", 185000 + i * 1000, 2, 2.0)
             for i in range(8)]
    big = _listing(agency.id, "Waterfront Residence", 7505000, 6, 7.0)
    try:
        ctx = app_module.get_listings_context(
            agency.id, _msgs("I want luxury home with 7 beds."))
        assert "Waterfront Residence" in ctx, (
            "the closest real alternative was crowded out of the shortlist again")
        # And it should outrank the starters, not trail them.
        assert ctx.index("Waterfront Residence") < ctx.index("Starter Home 0")
    finally:
        for l in cheap + [big]:
            app_module.db.session.delete(l)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_shortlist_is_ordered_biggest_first_when_a_size_was_requested():
    agency = make_agency()
    rows = [
        _listing(agency.id, "Six Bed Estate", 7505000, 6, 7.0),
        _listing(agency.id, "Four Bed House", 900000, 4, 3.0),
        _listing(agency.id, "Two Bed Condo", 185000, 2, 2.0),
    ]
    try:
        ctx = app_module.get_listings_context(agency.id, _msgs("Looking for a 7 bedroom home"))
        assert ctx.index("Six Bed Estate") < ctx.index("Four Bed House") < ctx.index("Two Bed Condo")
    finally:
        for l in rows:
            app_module.db.session.delete(l)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_cheapest_first_still_applies_when_no_size_was_requested():
    """The old ordering is still right for an open browse - this fix must
    not turn every conversation into an upsell."""
    agency = make_agency()
    rows = [
        _listing(agency.id, "Pricey Place", 900000, 3, 2.0),
        _listing(agency.id, "Budget Place", 150000, 3, 2.0),
    ]
    try:
        ctx = app_module.get_listings_context(agency.id, _msgs("Just browsing, what do you have?"))
        assert ctx.index("Budget Place") < ctx.index("Pricey Place")
    finally:
        for l in rows:
            app_module.db.session.delete(l)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_whole_inventory_facts_are_stated_so_the_ai_cannot_deny_stock():
    agency = make_agency()
    rows = [
        _listing(agency.id, "Two Bed Condo", 185000, 2, 2.0, location="Miami, FL"),
        _listing(agency.id, "Six Bed Estate", 7505000, 6, 7.0, location="Austin, TX"),
    ]
    try:
        ctx = app_module.get_listings_context(agency.id, _msgs("I want luxury home with 7 beds."))
        assert "WHOLE-INVENTORY FACTS" in ctx
        assert "bedroom counts range 2 to 6" in ctx
        assert "Austin" in ctx and "Miami" in ctx
    finally:
        for l in rows:
            app_module.db.session.delete(l)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


# ── 2. Same-day second viewing ───────────────────────────────────────

def test_a_day_stays_open_after_the_customer_books_one_slot_on_it():
    agency = make_agency()
    try:
        target = (datetime.now(app_module.PK_TZ).date() + timedelta(days=2))
        if target.weekday() == 6:
            target += timedelta(days=1)
        iso = target.strftime('%Y-%m-%d')
        day_label = target.strftime('%A, %B %d')

        ctx = app_module.get_availability_context(
            agency.id, 2, booked_slots={f"{iso}|6:00 PM"})

        # The day itself must still be offered...
        assert day_label in ctx
        # ...with its other times still open, and the taken one removed.
        day_line = next(l for l in ctx.splitlines() if l.startswith(f"- {day_label}"))
        assert "10:00 AM" in day_line
        assert "6:00 PM" not in day_line
        # ...and the model told explicitly that same-day is allowed.
        assert "SAME DAY" in ctx
        assert "already booked" in ctx.lower()
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_availability_without_any_prior_bookings_is_unchanged(client):
    agency = make_agency()
    try:
        ctx = app_module.get_availability_context(agency.id, 2)
        assert "VIEWING AVAILABILITY" in ctx
        assert "10:00 AM" in ctx
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


# ── 4. Super Admin panel counts ──────────────────────────────────────

def test_agency_without_a_paddle_subscription_is_not_counted_as_paying(client):
    """subscription_status defaults to 'active' on the model, so agencies
    that never paid anything were being reported as paying customers."""
    agency = make_agency()
    agency.subscription_status = 'active'
    agency.paddle_subscription_id = None
    app_module.db.session.commit()
    try:
        client.post("/super-admin-login", data={"password": "test-super-admin-pw"})
        stats = client.get("/platform-stats").get_json()
        assert stats["paying_agencies"] == 0
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_agency_with_a_real_subscription_is_counted_as_paying(client):
    agency = make_agency()
    agency.subscription_status = 'active'
    agency.paddle_subscription_id = 'sub_live_123'
    app_module.db.session.commit()
    try:
        client.post("/super-admin-login", data={"password": "test-super-admin-pw"})
        stats = client.get("/platform-stats").get_json()
        assert stats["paying_agencies"] == 1
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_orphaned_agents_from_deleted_agencies_are_not_counted(client):
    """Agencies deleted before the cascade cleanup existed left agent rows
    behind - the panel reported five agents when three existed."""
    agency = make_agency()
    real_agent = make_agent(agency.id)
    orphan = make_agent(999999)  # agency that does not exist
    try:
        client.post("/super-admin-login", data={"password": "test-super-admin-pw"})
        stats = client.get("/platform-stats").get_json()
        agent_ids_counted = stats["total_agents"]
        assert agent_ids_counted >= 1
        # The orphan must not be in the count.
        live_total = app_module.Agent.query.filter(
            app_module.Agent.agency_id.in_(
                [a.id for a in app_module.Agency.query.with_entities(app_module.Agency.id).all()])
        ).count()
        assert agent_ids_counted == live_total
        assert app_module.Agent.query.count() > live_total  # orphan really is there
    finally:
        app_module.db.session.delete(real_agent)
        app_module.db.session.delete(orphan)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_platform_stats_reports_active_agents_separately(client):
    agency = make_agency()
    active = make_agent(agency.id, status='active')
    disabled = make_agent(agency.id, status='disabled')
    try:
        client.post("/super-admin-login", data={"password": "test-super-admin-pw"})
        stats = client.get("/platform-stats").get_json()
        assert "active_agents" in stats
        assert stats["active_agents"] < stats["total_agents"]
    finally:
        app_module.db.session.delete(active)
        app_module.db.session.delete(disabled)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


# ── 5. Follow-up emails reaching the agent ───────────────────────────

def test_followup_email_also_goes_to_the_assigned_agent(monkeypatch):
    sent = []
    monkeypatch.setattr(app_module, "send_email_brevo",
                        lambda to, subject, body: sent.append({"to": to, "body": body}) or True)
    agency = make_agency()
    agent = make_agent(agency.id)
    lead = make_lead(agency.id, agent_id=agent.id)
    try:
        app_module.send_followup_email(agency, lead, 1)
        recipients = [s["to"] for s in sent]
        assert agency.email in recipients
        assert agent.email in recipients

        agent_copy = next(s for s in sent if s["to"] == agent.email)
        assert agent.name in agent_copy["body"]
        assert "/agent-login" in agent_copy["body"]
    finally:
        app_module.db.session.delete(lead)
        app_module.db.session.delete(agent)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_followup_email_for_an_unassigned_lead_goes_to_the_owner_only(monkeypatch):
    sent = []
    monkeypatch.setattr(app_module, "send_email_brevo",
                        lambda to, subject, body: sent.append({"to": to}) or True)
    agency = make_agency()
    lead = make_lead(agency.id)
    try:
        app_module.send_followup_email(agency, lead, 7)
        assert [s["to"] for s in sent] == [agency.email]
    finally:
        app_module.db.session.delete(lead)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()
