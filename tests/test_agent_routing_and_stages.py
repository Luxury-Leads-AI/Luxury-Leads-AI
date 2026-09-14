"""Tests for observations 4, 5 and 9 (Moaz, Sept 2026).

  4. Appointments are grouped per agent, and an agent can be opened to see
     everything assigned to them.
  5. Agents manage their password and nothing else; the owner sets an
     agent's coverage area, and leads/viewings go to a local agent first.
  9. The status dropdown and the outcome dropdown are now ONE control -
     setting a result implies the viewing happened, and an agent can book
     the customer's next viewing straight from the card.
"""
from datetime import datetime, timedelta

import app as app_module

from tests.test_route_guards import (
    make_agency, make_agent, make_lead, make_appointment,
    login_as_owner, login_as_agent,
)


def _listing(agency_id, title, location, price=500000, beds=3, baths=2.0):
    row = app_module.Listing(
        agency_id=agency_id, title=title, location=location,
        price=price, price_numeric=price, bedrooms=beds, bathrooms=baths,
        status='available', listing_purpose='sale',
    )
    app_module.db.session.add(row)
    app_module.db.session.commit()
    return row


def _next_weekday(days_ahead=2):
    d = datetime.now(app_module.PK_TZ).date() + timedelta(days=days_ahead)
    while d.weekday() == 6:
        d += timedelta(days=1)
    return d


# ── 5. Location-aware assignment ─────────────────────────────────────

def test_agent_covers_location_matches_on_whole_words():
    agency = make_agency()
    miami = make_agent(agency.id, location="Miami, Orlando")
    try:
        assert app_module.agent_covers_location(miami, "Miami, FL")
        assert app_module.agent_covers_location(miami, "Orlando, FL")
        assert not app_module.agent_covers_location(miami, "Austin, TX")
        # No location set = never wins the location round.
        plain = make_agent(agency.id)
        assert not app_module.agent_covers_location(plain, "Miami, FL")
        app_module.db.session.delete(plain)
        app_module.db.session.commit()
    finally:
        app_module.db.session.delete(miami)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_lead_goes_to_the_agent_covering_that_city():
    agency = make_agency(tier="agency")
    local = make_agent(agency.id, location="Miami")
    far = make_agent(agency.id, location="Seattle")
    try:
        chosen = app_module.assign_next_agent(agency, "Miami, FL")
        assert chosen.id == local.id
    finally:
        app_module.db.session.delete(local)
        app_module.db.session.delete(far)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_lead_still_assigned_when_nobody_covers_that_city():
    """A lead must never be dropped just because no agent is local."""
    agency = make_agency(tier="agency")
    a1 = make_agent(agency.id, location="Seattle")
    a2 = make_agent(agency.id, location="Portland")
    try:
        chosen = app_module.assign_next_agent(agency, "Miami, FL")
        assert chosen is not None
        assert chosen.id in (a1.id, a2.id)
    finally:
        app_module.db.session.delete(a1)
        app_module.db.session.delete(a2)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_viewing_prefers_a_local_agent_for_that_property():
    agency = make_agency(tier="agency")
    local = make_agent(agency.id, location="Austin")
    other = make_agent(agency.id, location="Boston")
    try:
        d = _next_weekday().strftime('%Y-%m-%d')
        chosen = app_module.pick_agent_for_slot(
            agency, d, "10:00 AM", None, "Austin, TX")
        assert chosen.id == local.id
    finally:
        app_module.db.session.delete(local)
        app_module.db.session.delete(other)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_owner_can_set_an_agents_coverage_area(client):
    agency = make_agency()
    agent = make_agent(agency.id)
    try:
        login_as_owner(client, agency.id)
        res = client.post(f"/update-agent-profile/{agent.id}", json={
            "name": agent.name, "email": agent.email, "location": "Miami, Fort Lauderdale",
        })
        assert res.status_code == 200
        app_module.db.session.refresh(agent)
        assert agent.location == "Miami, Fort Lauderdale"
    finally:
        app_module.db.session.delete(agent)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


# ── 4. Per-agent views ───────────────────────────────────────────────

def test_agent_detail_page_shows_that_agents_leads_and_viewings(client):
    agency = make_agency(tier="agency")
    agent = make_agent(agency.id, location="Miami")
    other_agent = make_agent(agency.id)
    mine = make_lead(agency.id, agent_id=agent.id)
    theirs = make_lead(agency.id, agent_id=other_agent.id)
    appt = make_appointment(agency.id, agent_id=agent.id,
                             property_interest="Bayfront Villa")
    try:
        login_as_owner(client, agency.id)
        html = client.get(f"/agent-detail/{agent.id}").get_data(as_text=True)
        assert agent.name in html
        assert "Bayfront Villa" in html
        assert "Miami" in html
        assert mine.email in html
        assert theirs.email not in html
    finally:
        for row in (appt, mine, theirs, agent, other_agent, agency):
            app_module.db.session.delete(row)
        app_module.db.session.commit()


def test_agent_detail_page_blocks_a_different_agency(client):
    agency = make_agency()
    other_agency = make_agency()
    agent = make_agent(agency.id)
    try:
        login_as_owner(client, other_agency.id)
        res = client.get(f"/agent-detail/{agent.id}")
        assert res.status_code == 302
        assert "/owner-login" in res.headers["Location"]
    finally:
        app_module.db.session.delete(agent)
        app_module.db.session.delete(agency)
        app_module.db.session.delete(other_agency)
        app_module.db.session.commit()


def test_appointments_page_groups_bookings_under_each_agent(client):
    agency = make_agency(tier="agency")
    a1 = make_agent(agency.id)
    a2 = make_agent(agency.id)
    ap1 = make_appointment(agency.id, agent_id=a1.id, property_interest="Villa One")
    ap2 = make_appointment(agency.id, agent_id=a2.id, property_interest="Villa Two")
    orphan = make_appointment(agency.id, property_interest="Villa Three")
    try:
        login_as_owner(client, agency.id)
        html = client.get(f"/appointments/{agency.id}").get_data(as_text=True)
        assert a1.name in html and a2.name in html
        assert "Unassigned" in html
        assert "Villa One" in html and "Villa Three" in html
    finally:
        for row in (ap1, ap2, orphan, a1, a2, agency):
            app_module.db.session.delete(row)
        app_module.db.session.commit()


# ── 9. One unified stage ─────────────────────────────────────────────

def test_stage_reads_back_the_outcome_once_one_is_recorded():
    agency = make_agency()
    appt = make_appointment(agency.id, status='completed')
    try:
        assert app_module.appointment_stage(appt) == 'completed'
        appt.outcome = 'wants_to_buy'
        app_module.db.session.commit()
        assert app_module.appointment_stage(appt) == 'wants_to_buy'
        # Cancelling always wins - it's not a viewing result.
        appt.status = 'cancelled'
        app_module.db.session.commit()
        assert app_module.appointment_stage(appt) == 'cancelled'
    finally:
        app_module.db.session.delete(appt)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_setting_a_result_also_marks_the_viewing_completed(client):
    agency = make_agency()
    appt = make_appointment(agency.id, status='confirmed')
    try:
        login_as_owner(client, agency.id)
        res = client.post(f"/update-appointment-status/{appt.id}",
                          json={"stage": "wants_to_buy"})
        assert res.status_code == 200
        app_module.db.session.refresh(appt)
        assert appt.status == 'completed'
        assert appt.outcome == 'wants_to_buy'
        assert res.get_json()["stage"] == 'wants_to_buy'
    finally:
        app_module.db.session.delete(appt)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_rescheduling_clears_a_result_that_no_longer_applies(client):
    agency = make_agency()
    appt = make_appointment(agency.id, status='completed', outcome='not_interested')
    try:
        login_as_owner(client, agency.id)
        client.post(f"/update-appointment-status/{appt.id}", json={"stage": "confirmed"})
        app_module.db.session.refresh(appt)
        assert appt.status == 'confirmed'
        assert appt.outcome is None
        assert app_module.appointment_stage(appt) == 'confirmed'
    finally:
        app_module.db.session.delete(appt)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_the_old_status_key_still_works(client):
    """Anything still posting {status: ...} must not break."""
    agency = make_agency()
    appt = make_appointment(agency.id)
    try:
        login_as_owner(client, agency.id)
        res = client.post(f"/update-appointment-status/{appt.id}",
                          json={"status": "confirmed"})
        assert res.status_code == 200
        app_module.db.session.refresh(appt)
        assert appt.status == 'confirmed'
    finally:
        app_module.db.session.delete(appt)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_agent_sets_the_unified_stage_on_their_own_appointment(client):
    agency = make_agency(tier="agency")
    agent = make_agent(agency.id)
    appt = make_appointment(agency.id, agent_id=agent.id)
    try:
        login_as_agent(client, agent.id)
        res = client.post(f"/agent-update-appointment-status/{appt.id}",
                          json={"stage": "not_interested"})
        assert res.status_code == 200
        app_module.db.session.refresh(appt)
        assert appt.status == 'completed'
        assert appt.outcome == 'not_interested'
        assert appt.outcome_source == 'agent'
    finally:
        for row in (appt, agent, agency):
            app_module.db.session.delete(row)
        app_module.db.session.commit()


def test_an_invalid_stage_is_rejected(client):
    agency = make_agency()
    appt = make_appointment(agency.id)
    try:
        login_as_owner(client, agency.id)
        res = client.post(f"/update-appointment-status/{appt.id}",
                          json={"stage": "maybe_someday"})
        assert res.status_code == 400
    finally:
        app_module.db.session.delete(appt)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


# ── 9b. Booking the next viewing ─────────────────────────────────────

def test_agent_books_a_follow_up_viewing_for_the_same_customer(client):
    agency = make_agency(tier="agency")
    agent = make_agent(agency.id, location="Miami")
    listing = _listing(agency.id, "Second Villa", "Miami, FL")
    appt = make_appointment(agency.id, agent_id=agent.id,
                             customer_email="repeat@example.test",
                             property_interest="First Villa")
    created = None
    try:
        login_as_agent(client, agent.id)
        d = _next_weekday(3).strftime('%Y-%m-%d')
        res = client.post(f"/book-followup-viewing/{appt.id}", json={
            "appointment_date_iso": d,
            "appointment_time": "2:00 PM",
            "property_interest": "Second Villa",
        })
        assert res.status_code == 200, res.get_data(as_text=True)
        new_id = res.get_json()["appointment_id"]
        created = app_module.db.session.get(app_module.Appointment, new_id)

        assert created.customer_email == "repeat@example.test"
        assert created.property_interest == "Second Villa"
        assert created.agent_id == agent.id          # stays with the same agent
        assert created.status == 'pending'

        # The original is now explicitly 'they wanted something else'.
        app_module.db.session.refresh(appt)
        assert appt.outcome == 'wants_other_options'
    finally:
        if created:
            app_module.db.session.delete(created)
        for row in (appt, listing, agent, agency):
            app_module.db.session.delete(row)
        app_module.db.session.commit()


def test_follow_up_viewing_rejects_a_taken_or_invalid_slot(client):
    agency = make_agency()
    appt = make_appointment(agency.id)
    try:
        login_as_owner(client, agency.id)
        # No date at all
        assert client.post(f"/book-followup-viewing/{appt.id}",
                           json={"appointment_time": "2:00 PM"}).status_code == 400
        # A time that isn't one of the offered slots
        d = _next_weekday(3).strftime('%Y-%m-%d')
        res = client.post(f"/book-followup-viewing/{appt.id}", json={
            "appointment_date_iso": d, "appointment_time": "3:37 AM"})
        assert res.status_code == 400
    finally:
        app_module.db.session.delete(appt)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_follow_up_viewing_requires_the_owner_or_the_assigned_agent(client):
    agency = make_agency(tier="agency")
    agent = make_agent(agency.id)
    stranger = make_agent(agency.id)
    appt = make_appointment(agency.id, agent_id=agent.id)
    try:
        login_as_agent(client, stranger.id)
        d = _next_weekday(3).strftime('%Y-%m-%d')
        res = client.post(f"/book-followup-viewing/{appt.id}", json={
            "appointment_date_iso": d, "appointment_time": "2:00 PM"})
        assert res.status_code == 401
    finally:
        for row in (appt, agent, stranger, agency):
            app_module.db.session.delete(row)
        app_module.db.session.commit()


# ── 7. Mobile responsiveness ─────────────────────────────────────────

def test_every_rendered_page_declares_a_mobile_viewport(client):
    """Without this meta tag a phone renders the page at ~980px and scales
    it down - which is exactly why the dashboards were unusable on mobile,
    regardless of any CSS."""
    import glob, os
    missing = []
    for path in sorted(glob.glob(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'templates', '*.html'))):
        with open(path, encoding='utf-8') as fh:
            if 'name="viewport"' not in fh.read():
                missing.append(os.path.basename(path))
    assert missing == [], f"templates with no viewport meta tag: {missing}"
