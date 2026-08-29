"""Tests for the second round of auth fixes: the ~30 routes that used to
trust whatever agency_id/lead_id/appt_id/listing_id was in the URL, with no
check that the caller's session actually belongs to that agency (or, for
agent-side routes, that the caller actually is that agent).

Each of these previously would happily serve or modify another agency's
real customer data (names, emails, phones, budgets) to anyone who guessed
or enumerated an ID - no login required. This file confirms:
  1. an unauthenticated caller is rejected (401 for JSON endpoints, a
     redirect to the login page for HTML pages/direct-download links),
  2. the actual owner (or, where relevant, the actual agent) is still let
     through,
  3. a DIFFERENT agency's session cannot reach another agency's data
     (cross-tenant isolation - the core bug being fixed here).
"""
import uuid

import app as app_module


def make_agency(tier="agency"):
    agency = app_module.Agency(
        name=f"Test Agency {uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        tier=tier,
    )
    app_module.db.session.add(agency)
    app_module.db.session.commit()
    return agency


def make_lead(agency_id, **kwargs):
    lead = app_module.Lead(agency_id=agency_id, name="Test Lead",
                            email=f"{uuid.uuid4().hex[:8]}@example.test", **kwargs)
    app_module.db.session.add(lead)
    app_module.db.session.commit()
    return lead


def make_appointment(agency_id, **kwargs):
    appt = app_module.Appointment(agency_id=agency_id, customer_name="Test Customer", **kwargs)
    app_module.db.session.add(appt)
    app_module.db.session.commit()
    return appt


def make_listing_row(agency_id, **kwargs):
    listing = app_module.Listing(agency_id=agency_id, title="Test Listing", **kwargs)
    app_module.db.session.add(listing)
    app_module.db.session.commit()
    return listing


def make_agent(agency_id, **kwargs):
    agent = app_module.Agent(agency_id=agency_id, name="Test Agent",
                              email=f"{uuid.uuid4().hex[:8]}@example.test", **kwargs)
    agent.set_password("whatever123")
    app_module.db.session.add(agent)
    app_module.db.session.commit()
    return agent


def login_as_owner(client, agency_id):
    with client.session_transaction() as sess:
        sess["agency_id"] = str(agency_id)


def login_as_agent(client, agent_id):
    with client.session_transaction() as sess:
        sess["agent_id"] = agent_id


class _Ctx:
    """One throwaway agency (+ a second, unrelated agency for cross-tenant
    checks) with one lead/appointment/listing/agent each, cleaned up after
    the test."""

    def __init__(self):
        self.agency = make_agency()
        self.other_agency = make_agency()
        self.lead = make_lead(self.agency.id)
        self.appt = make_appointment(self.agency.id)
        self.listing = make_listing_row(self.agency.id)
        self.agent = make_agent(self.agency.id)

    def cleanup(self):
        for model, obj in [
            (app_module.Lead, self.lead), (app_module.Appointment, self.appt),
            (app_module.Listing, self.listing), (app_module.Agent, self.agent),
        ]:
            row = app_module.db.session.get(model, obj.id)
            if row:
                app_module.db.session.delete(row)
        app_module.db.session.commit()
        for agency in (self.agency, self.other_agency):
            row = app_module.db.session.get(app_module.Agency, agency.id)
            if row:
                app_module.db.session.delete(row)
        app_module.db.session.commit()


import pytest  # noqa: E402


@pytest.fixture()
def ctx():
    c = _Ctx()
    yield c
    c.cleanup()


# ── Lead management (owner-only) ────────────────────────────────────

def test_update_lead_status_rejects_logged_out(client, ctx):
    res = client.post(f"/update-lead-status/{ctx.lead.id}", json={"status": "contacted"})
    assert res.status_code == 401


def test_update_lead_status_rejects_other_agency(client, ctx):
    login_as_owner(client, ctx.other_agency.id)
    res = client.post(f"/update-lead-status/{ctx.lead.id}", json={"status": "contacted"})
    assert res.status_code == 401


def test_update_lead_status_allows_real_owner(client, ctx):
    login_as_owner(client, ctx.agency.id)
    res = client.post(f"/update-lead-status/{ctx.lead.id}", json={"status": "contacted"})
    assert res.status_code == 200


def test_add_lead_note_rejects_logged_out(client, ctx):
    res = client.post(f"/add-lead-note/{ctx.lead.id}", json={"note": "hi"})
    assert res.status_code == 401


def test_delete_lead_note_rejects_logged_out(client, ctx):
    res = client.delete(f"/delete-lead-note/{ctx.lead.id}/1")
    assert res.status_code == 401


def test_bulk_delete_leads_rejects_logged_out(client, ctx):
    res = client.post("/bulk-delete-leads", json={"lead_ids": [ctx.lead.id]})
    assert res.status_code == 401


def test_bulk_delete_leads_skips_other_agencys_leads(client, ctx):
    """A logged-in owner cannot sneak a different agency's lead ID into the
    batch and have it deleted."""
    other_lead = make_lead(ctx.other_agency.id)
    try:
        login_as_owner(client, ctx.agency.id)
        res = client.post("/bulk-delete-leads", json={"lead_ids": [ctx.lead.id, other_lead.id]})
        assert res.status_code == 200
        assert res.get_json()["deleted"] == 1
        assert app_module.db.session.get(app_module.Lead, other_lead.id) is not None
    finally:
        row = app_module.db.session.get(app_module.Lead, other_lead.id)
        if row:
            app_module.db.session.delete(row)
            app_module.db.session.commit()


def test_delete_lead_rejects_other_agency(client, ctx):
    login_as_owner(client, ctx.other_agency.id)
    res = client.delete(f"/delete-lead/{ctx.lead.id}")
    assert res.status_code == 401
    assert app_module.db.session.get(app_module.Lead, ctx.lead.id) is not None


def test_clear_all_leads_rejects_logged_out(client, ctx):
    res = client.delete(f"/clear-all-leads/{ctx.agency.id}")
    assert res.status_code == 401


# ── get-lead-detail: owner OR an agent from the SAME agency ────────

def test_get_lead_detail_rejects_logged_out(client, ctx):
    res = client.get(f"/get-lead-detail/{ctx.lead.id}")
    assert res.status_code == 401


def test_get_lead_detail_rejects_other_agencys_owner(client, ctx):
    login_as_owner(client, ctx.other_agency.id)
    res = client.get(f"/get-lead-detail/{ctx.lead.id}")
    assert res.status_code == 401


def test_get_lead_detail_allows_owner(client, ctx):
    login_as_owner(client, ctx.agency.id)
    res = client.get(f"/get-lead-detail/{ctx.lead.id}")
    assert res.status_code == 200


def test_get_lead_detail_allows_an_agent_of_the_same_agency(client, ctx):
    login_as_agent(client, ctx.agent.id)
    res = client.get(f"/get-lead-detail/{ctx.lead.id}")
    assert res.status_code == 200


def test_get_lead_detail_rejects_an_agent_of_a_different_agency(client, ctx):
    other_agent = make_agent(ctx.other_agency.id)
    try:
        login_as_agent(client, other_agent.id)
        res = client.get(f"/get-lead-detail/{ctx.lead.id}")
        assert res.status_code == 401
    finally:
        row = app_module.db.session.get(app_module.Agent, other_agent.id)
        if row:
            app_module.db.session.delete(row)
            app_module.db.session.commit()


# ── Appointments (owner-only) ───────────────────────────────────────

def test_appointments_page_rejects_logged_out(client, ctx):
    res = client.get(f"/appointments/{ctx.agency.id}")
    assert res.status_code == 302
    assert "/owner-login" in res.headers["Location"]


def test_appointments_page_allows_owner(client, ctx):
    login_as_owner(client, ctx.agency.id)
    res = client.get(f"/appointments/{ctx.agency.id}")
    assert res.status_code == 200


def test_reassign_appointment_rejects_other_agency(client, ctx):
    login_as_owner(client, ctx.other_agency.id)
    res = client.post(f"/reassign-appointment/{ctx.appt.id}", json={"agent_id": None})
    assert res.status_code == 401


def test_update_slot_capacity_rejects_logged_out(client, ctx):
    res = client.post(f"/update-slot-capacity/{ctx.agency.id}", json={"capacity": 3})
    assert res.status_code == 401


def test_update_appointment_status_rejects_logged_out(client, ctx):
    res = client.post(f"/update-appointment-status/{ctx.appt.id}", json={"status": "confirmed"})
    assert res.status_code == 401


def test_delete_appointment_rejects_other_agency(client, ctx):
    login_as_owner(client, ctx.other_agency.id)
    res = client.delete(f"/delete-appointment/{ctx.appt.id}")
    assert res.status_code == 401
    assert app_module.db.session.get(app_module.Appointment, ctx.appt.id) is not None


def test_get_appointments_count_rejects_logged_out(client, ctx):
    res = client.get(f"/get-appointments-count/{ctx.agency.id}")
    assert res.status_code == 401


# ── Listings (owner-only) ───────────────────────────────────────────

def test_listings_page_rejects_logged_out(client, ctx):
    res = client.get(f"/listings/{ctx.agency.id}")
    assert res.status_code == 302
    assert "/owner-login" in res.headers["Location"]


def test_add_listing_rejects_logged_out(client, ctx):
    res = client.post(f"/add-listing/{ctx.agency.id}", json={"title": "New Listing"})
    assert res.status_code == 401


def test_toggle_listing_status_rejects_other_agency(client, ctx):
    login_as_owner(client, ctx.other_agency.id)
    res = client.post(f"/toggle-listing-status/{ctx.listing.id}", json={"status": "sold"})
    assert res.status_code == 401


def test_delete_listing_rejects_other_agency(client, ctx):
    login_as_owner(client, ctx.other_agency.id)
    res = client.delete(f"/delete-listing/{ctx.listing.id}")
    assert res.status_code == 401
    assert app_module.db.session.get(app_module.Listing, ctx.listing.id) is not None


def test_delete_all_listings_rejects_logged_out(client, ctx):
    res = client.delete(f"/delete-all-listings/{ctx.agency.id}")
    assert res.status_code == 401


def test_get_listings_api_rejects_logged_out(client, ctx):
    res = client.get(f"/get-listings/{ctx.agency.id}")
    assert res.status_code == 401


def test_get_listings_api_allows_owner(client, ctx):
    login_as_owner(client, ctx.agency.id)
    res = client.get(f"/get-listings/{ctx.agency.id}")
    assert res.status_code == 200


# ── Export / analytics / webhook (owner-only) ───────────────────────

def test_export_leads_redirects_when_logged_out(client, ctx):
    res = client.get(f"/export/{ctx.agency.id}")
    assert res.status_code == 302
    assert "/owner-login" in res.headers["Location"]


def test_analytics_page_redirects_when_logged_out(client, ctx):
    res = client.get(f"/analytics/{ctx.agency.id}")
    assert res.status_code == 302
    assert "/owner-login" in res.headers["Location"]


def test_analytics_page_allows_owner(client, ctx):
    login_as_owner(client, ctx.agency.id)
    res = client.get(f"/analytics/{ctx.agency.id}")
    assert res.status_code == 200


def test_update_agency_webhook_redirects_when_logged_out(client, ctx):
    res = client.post(f"/update-agency-webhook/{ctx.agency.id}", data={"webhook_url": "https://x.test"})
    assert res.status_code == 302
    assert "/owner-login" in res.headers["Location"]


# ── Agent-side actions: session-based, not client-supplied agent_id ──

def test_agent_update_lead_status_ignores_spoofed_agent_id_in_body(client, ctx):
    """Regression test for the pre-fix bug: the route used to trust
    whatever agent_id was in the JSON body instead of the session, so
    anyone could claim to be any agent just by naming their ID."""
    lead = make_lead(ctx.agency.id, agent_id=ctx.agent.id)
    other_agent = make_agent(ctx.other_agency.id)
    try:
        # Not logged in as any agent at all - must be rejected even though
        # the body claims to be the correct agent.
        res = client.post(f"/agent-update-lead-status/{lead.id}",
                           json={"status": "contacted", "agent_id": ctx.agent.id})
        assert res.status_code == 401

        # Logged in as a DIFFERENT agent, but claiming (via the body) to be
        # the one who owns this lead - must still be rejected.
        login_as_agent(client, other_agent.id)
        res = client.post(f"/agent-update-lead-status/{lead.id}",
                           json={"status": "contacted", "agent_id": ctx.agent.id})
        assert res.status_code == 403

        # The actual owning agent, via session, works.
        with client.session_transaction() as sess:
            sess["agent_id"] = ctx.agent.id
        res = client.post(f"/agent-update-lead-status/{lead.id}", json={"status": "contacted"})
        assert res.status_code == 200
    finally:
        row = app_module.db.session.get(app_module.Lead, lead.id)
        if row:
            app_module.db.session.delete(row)
        row2 = app_module.db.session.get(app_module.Agent, other_agent.id)
        if row2:
            app_module.db.session.delete(row2)
        app_module.db.session.commit()


def test_agent_update_appointment_status_requires_session(client, ctx):
    appt = make_appointment(ctx.agency.id, agent_id=ctx.agent.id)
    try:
        res = client.post(f"/agent-update-appointment-status/{appt.id}",
                           json={"status": "confirmed", "agent_id": ctx.agent.id})
        assert res.status_code == 401

        login_as_agent(client, ctx.agent.id)
        res = client.post(f"/agent-update-appointment-status/{appt.id}", json={"status": "confirmed"})
        assert res.status_code == 200
    finally:
        row = app_module.db.session.get(app_module.Appointment, appt.id)
        if row:
            app_module.db.session.delete(row)
            app_module.db.session.commit()


# ── Agent self-service password change ──────────────────────────────

def test_change_agent_password_requires_a_session(client, ctx):
    res = client.post(f"/change-agent-password/{ctx.agent.id}", json={"new_password": "new-password-1"})
    assert res.status_code == 401


def test_change_agent_password_works_for_the_agent_themself(client, ctx):
    login_as_agent(client, ctx.agent.id)
    res = client.post(f"/change-agent-password/{ctx.agent.id}", json={"new_password": "new-password-1"})
    assert res.status_code == 200
    app_module.db.session.refresh(ctx.agent)
    assert ctx.agent.check_password("new-password-1") is True


def test_change_agent_password_rejects_a_different_agent(client, ctx):
    other_agent = make_agent(ctx.other_agency.id)
    try:
        login_as_agent(client, other_agent.id)
        res = client.post(f"/change-agent-password/{ctx.agent.id}", json={"new_password": "new-password-1"})
        assert res.status_code == 401
    finally:
        row = app_module.db.session.get(app_module.Agent, other_agent.id)
        if row:
            app_module.db.session.delete(row)
            app_module.db.session.commit()


def test_change_agent_password_works_for_the_agencys_owner(client, ctx):
    login_as_owner(client, ctx.agency.id)
    res = client.post(f"/change-agent-password/{ctx.agent.id}", json={"new_password": "reset-by-owner-1"})
    assert res.status_code == 200
