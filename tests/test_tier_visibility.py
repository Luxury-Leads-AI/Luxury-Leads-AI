"""Tests for the Tier 3 (Corporation) visibility feature flag, and for the
split signup flow (Item 3) that replaced the old single form.

Moaz wants to sell only Solo + Agency for now, without deleting any of the
Corporation-tier code (TIER_LIMITS, the agency.tier in ['agency',
'corporation'] feature gates elsewhere) so it can be switched back on later.
SHOW_TIER_3 (env var, default off) controls only whether the pricing page
shows the Corporation plan and whether the signup chooser mentions it.

The old /signup page used to be a single form with a 3(2)-card tier picker.
It's now a chooser page linking to two separate, tier-locked forms:
/signup/solo and /signup/agency - each collects the same fields but with
tailored copy, and posts to /create-agency with its tier hardcoded (the
visitor never picks a tier on these pages).
"""
import app as app_module


def test_show_tier_3_defaults_to_false_without_the_env_var():
    # conftest.py never sets SHOW_TIER_3, so the default ('false') applies.
    assert app_module.SHOW_TIER_3 is False


def test_pricing_page_hides_corporation_plan_by_default(client):
    res = client.get("/pricing")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Corporation" not in html
    assert "Solo Agent" in html
    assert "Agency" in html


def test_pricing_page_shows_corporation_plan_when_flag_is_on(monkeypatch, client):
    monkeypatch.setattr(app_module, "SHOW_TIER_3", True)
    res = client.get("/pricing")
    html = res.get_data(as_text=True)
    assert "Corporation" in html


def test_pricing_ctas_point_at_the_dedicated_signup_forms(client):
    html = client.get("/pricing").get_data(as_text=True)
    assert 'href="/signup/solo"' in html
    assert 'href="/signup/agency"' in html


def test_backend_tier_logic_is_untouched_by_the_flag(client):
    """Hiding the tier from the UI must not remove the ability to actually
    create a corporation-tier agency (e.g. Moaz creating one manually) -
    only the signup/pricing pages change."""
    res = client.post("/create-agency", json={
        "name": "Manually Created Corp Agency",
        "email": "manualcorp@example.test",
        "tier": "corporation",
    })
    assert res.status_code == 200
    data = res.get_json()
    assert data["tier"] == "corporation"
    agency = app_module.db.session.get(app_module.Agency, data["agency_id"])
    try:
        assert agency.tier == "corporation"
        assert app_module.TIER_LIMITS["corporation"]  # still defined, untouched
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


# ── Signup chooser page ──────────────────────────────────────────────

def test_signup_chooser_links_to_both_dedicated_forms(client):
    res = client.get("/signup")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert 'href="/signup/solo"' in html
    assert 'href="/signup/agency"' in html
    # no tier picker or hidden third option leaking through by default
    assert "Corporation" not in html


def test_signup_chooser_mentions_corporation_path_when_flag_is_on(monkeypatch, client):
    monkeypatch.setattr(app_module, "SHOW_TIER_3", True)
    html = client.get("/signup").get_data(as_text=True)
    assert "multiple branches" in html.lower()


# ── Dedicated Solo form ──────────────────────────────────────────────

def test_signup_solo_page_renders(client):
    res = client.get("/signup/solo")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Solo Agent Sign Up" in html
    assert "$197" in html
    # tier is hardcoded, not a picker - no tier selection UI on this page
    assert "tier-grid" not in html


def test_signup_solo_creates_a_solo_tier_agency(client):
    res = client.post("/create-agency", json={
        "name": "Solo Test Biz", "email": "solotest1@example.test",
        "tier": "solo", "subscription_type": "solo",
    })
    assert res.status_code == 200
    data = res.get_json()
    assert data["tier"] == "solo"
    agency = app_module.db.session.get(app_module.Agency, data["agency_id"])
    try:
        assert agency.tier == "solo"
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


# ── Dedicated Agency form ────────────────────────────────────────────

def test_signup_agency_page_renders(client):
    res = client.get("/signup/agency")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "Agency Sign Up" in html
    assert "$497" in html
    assert "tier-grid" not in html


def test_signup_agency_creates_an_agency_tier_agency(client):
    res = client.post("/create-agency", json={
        "name": "Agency Test Biz", "email": "agencytest1@example.test",
        "tier": "agency", "subscription_type": "agency",
    })
    assert res.status_code == 200
    data = res.get_json()
    assert data["tier"] == "agency"
    agency = app_module.db.session.get(app_module.Agency, data["agency_id"])
    try:
        assert agency.tier == "agency"
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()
