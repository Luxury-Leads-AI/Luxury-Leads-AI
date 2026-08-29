"""Tests for Item 7: lean Super Admin panel content - the /agencies list
now carries tier/subscription/trial status and basic per-agency counts,
and a new /platform-stats endpoint feeds the overview cards at the top of
owner.html. Deliberately lean per Moaz's instruction: agency list + trial/
subscription status + basic counts, not a full analytics build-out.
"""
from datetime import datetime, timedelta

import app as app_module

from tests.test_route_guards import make_agency, make_lead, make_appointment, make_agent

SUPER_ADMIN_PASSWORD = "test-super-admin-pw"


def _login_super_admin(client):
    client.post("/super-admin-login", data={"password": SUPER_ADMIN_PASSWORD})


# ── /agencies extended fields ────────────────────────────────────────

def test_agencies_list_includes_tier_subscription_and_counts(client):
    agency = make_agency(tier="agency")
    agency.subscription_status = "trialing"
    agency.trial_ends_at = datetime.utcnow() + timedelta(days=5)
    app_module.db.session.commit()
    lead = make_lead(agency.id)
    appt = make_appointment(agency.id)
    agent = make_agent(agency.id)
    try:
        _login_super_admin(client)
        res = client.get("/agencies")
        assert res.status_code == 200
        data = res.get_json()
        row = next(a for a in data if a["id"] == agency.id)
        assert row["tier"] == "agency"
        assert row["subscription_status"] == "trialing"
        assert row["trial_ends_at"] is not None
        assert row["trial_days_left"] is not None and row["trial_days_left"] > 0
        assert row["lead_count"] == 1
        assert row["appointment_count"] == 1
        assert row["agent_count"] == 1
    finally:
        app_module.db.session.delete(lead)
        app_module.db.session.delete(appt)
        app_module.db.session.delete(agent)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_agencies_list_flags_an_expired_trial_with_zero_days_left(client):
    agency = make_agency()
    agency.subscription_status = "trialing"
    agency.trial_ends_at = datetime.utcnow() - timedelta(days=3)
    app_module.db.session.commit()
    try:
        _login_super_admin(client)
        res = client.get("/agencies")
        row = next(a for a in res.get_json() if a["id"] == agency.id)
        assert row["trial_days_left"] == 0
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_agencies_list_defaults_missing_tier_to_solo(client):
    agency = make_agency()
    agency.tier = None
    app_module.db.session.commit()
    try:
        _login_super_admin(client)
        res = client.get("/agencies")
        row = next(a for a in res.get_json() if a["id"] == agency.id)
        assert row["tier"] == "solo"
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


# ── /platform-stats ───────────────────────────────────────────────────

def test_platform_stats_requires_super_admin(client):
    res = client.get("/platform-stats")
    assert res.status_code == 401


def test_platform_stats_returns_basic_counts(client):
    agency = make_agency(tier="solo")
    agency.subscription_status = "trialing"
    agency.trial_ends_at = datetime.utcnow() + timedelta(days=10)
    app_module.db.session.commit()
    lead = make_lead(agency.id)
    appt = make_appointment(agency.id)
    agent = make_agent(agency.id)
    try:
        _login_super_admin(client)
        res = client.get("/platform-stats")
        assert res.status_code == 200
        data = res.get_json()
        for key in ("total_agencies", "active_trials", "expired_trials",
                    "paying_agencies", "by_tier", "total_leads",
                    "total_appointments", "total_agents"):
            assert key in data
        assert data["total_agencies"] >= 1
        assert data["active_trials"] >= 1
        assert data["total_leads"] >= 1
        assert data["total_appointments"] >= 1
        assert data["total_agents"] >= 1
        assert data["by_tier"]["solo"] >= 1
    finally:
        app_module.db.session.delete(lead)
        app_module.db.session.delete(appt)
        app_module.db.session.delete(agent)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_platform_stats_counts_expired_trials_separately_from_active(client):
    active = make_agency()
    active.subscription_status = "trialing"
    active.trial_ends_at = datetime.utcnow() + timedelta(days=3)
    expired = make_agency()
    expired.subscription_status = "trialing"
    expired.trial_ends_at = datetime.utcnow() - timedelta(days=3)
    app_module.db.session.commit()
    try:
        _login_super_admin(client)
        before = client.get("/agencies")  # sanity: both should be listed
        assert before.status_code == 200
        stats = client.get("/platform-stats").get_json()
        assert stats["active_trials"] >= 1
        assert stats["expired_trials"] >= 1
    finally:
        app_module.db.session.delete(active)
        app_module.db.session.delete(expired)
        app_module.db.session.commit()
