"""Tests for Item 5: self-service profile management for agency owners
and agents.

Two GET pages (agency_profile / agent_profile) reuse the exact same
guards as the rest of the authenticated surface (_owner_owns_agency, and
the self/owner/super-admin check already used by /change-agent-password),
and two POST JSON routes (update-agency-profile / update-agent-profile)
do the actual writes, with basic validation:
  - required fields (name, email) can't be blanked out
  - email must look like an email
  - max_viewings_per_slot must be a positive integer
  - an agent's email must stay unique within their own agency (matching
    the existing rule in /add-agent)
"""
import app as app_module

from tests.test_route_guards import make_agency, make_agent, login_as_owner, login_as_agent


# ── Agency profile: page guard ───────────────────────────────────────

def test_agency_profile_page_requires_login(client):
    agency = make_agency()
    try:
        res = client.get(f"/agency-profile/{agency.id}")
        assert res.status_code == 302
        assert "/owner-login" in res.headers["Location"]
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_agency_profile_page_renders_for_owner(client):
    agency = make_agency()
    try:
        login_as_owner(client, agency.id)
        res = client.get(f"/agency-profile/{agency.id}")
        assert res.status_code == 200
        html = res.get_data(as_text=True)
        assert agency.name in html
        assert agency.email in html
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_agency_profile_page_blocks_a_different_agency(client):
    agency = make_agency()
    other_agency = make_agency()
    try:
        login_as_owner(client, other_agency.id)
        res = client.get(f"/agency-profile/{agency.id}")
        assert res.status_code == 302
        assert "/owner-login" in res.headers["Location"]
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.delete(other_agency)
        app_module.db.session.commit()


# ── Agency profile: update ───────────────────────────────────────────

def test_update_agency_profile_requires_login(client):
    agency = make_agency()
    try:
        res = client.post(f"/update-agency-profile/{agency.id}", json={
            "name": "New Name", "email": "new@example.test",
        })
        assert res.status_code == 401
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_update_agency_profile_saves_changes(client):
    agency = make_agency()
    try:
        login_as_owner(client, agency.id)
        res = client.post(f"/update-agency-profile/{agency.id}", json={
            "name": "Elite Realty Updated",
            "owner_name": "Jane Doe",
            "email": "jane@example.test",
            "whatsapp": "+15551234567",
            "assistant_name": "Sophia",
            "max_viewings_per_slot": "5",
        })
        assert res.status_code == 200
        assert res.get_json()["success"] is True

        app_module.db.session.refresh(agency)
        assert agency.name == "Elite Realty Updated"
        assert agency.owner_name == "Jane Doe"
        assert agency.email == "jane@example.test"
        assert agency.whatsapp == "+15551234567"
        assert agency.assistant_name == "Sophia"
        assert agency.max_viewings_per_slot == 5
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_update_agency_profile_rejects_blank_name(client):
    agency = make_agency()
    try:
        login_as_owner(client, agency.id)
        res = client.post(f"/update-agency-profile/{agency.id}", json={
            "name": "  ", "email": agency.email,
        })
        assert res.status_code == 400
        assert "name" in res.get_json()["error"].lower()
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_update_agency_profile_rejects_invalid_email(client):
    agency = make_agency()
    try:
        login_as_owner(client, agency.id)
        res = client.post(f"/update-agency-profile/{agency.id}", json={
            "name": agency.name, "email": "not-an-email",
        })
        assert res.status_code == 400
        assert "email" in res.get_json()["error"].lower()
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_update_agency_profile_rejects_non_positive_max_viewings(client):
    agency = make_agency()
    try:
        login_as_owner(client, agency.id)
        res = client.post(f"/update-agency-profile/{agency.id}", json={
            "name": agency.name, "email": agency.email,
            "max_viewings_per_slot": "0",
        })
        assert res.status_code == 400
        assert "viewings" in res.get_json()["error"].lower()
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_update_agency_profile_blocks_a_different_agency(client):
    agency = make_agency()
    other_agency = make_agency()
    try:
        login_as_owner(client, other_agency.id)
        res = client.post(f"/update-agency-profile/{agency.id}", json={
            "name": "Hijacked", "email": "hijack@example.test",
        })
        assert res.status_code == 401
        app_module.db.session.refresh(agency)
        assert agency.name != "Hijacked"
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.delete(other_agency)
        app_module.db.session.commit()


# ── Agent profile: page guard ────────────────────────────────────────

def test_agent_profile_page_requires_login(client):
    agency = make_agency()
    agent = make_agent(agency.id)
    try:
        res = client.get(f"/agent-profile/{agent.id}")
        assert res.status_code == 302
        assert "/agent-login" in res.headers["Location"]
    finally:
        app_module.db.session.delete(agent)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_agent_profile_page_renders_for_self(client):
    agency = make_agency()
    agent = make_agent(agency.id)
    try:
        login_as_agent(client, agent.id)
        res = client.get(f"/agent-profile/{agent.id}")
        assert res.status_code == 200
        assert agent.name in res.get_data(as_text=True)
    finally:
        app_module.db.session.delete(agent)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_agent_profile_page_renders_for_owner(client):
    agency = make_agency()
    agent = make_agent(agency.id)
    try:
        login_as_owner(client, agency.id)
        res = client.get(f"/agent-profile/{agent.id}")
        assert res.status_code == 200
    finally:
        app_module.db.session.delete(agent)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_agent_profile_page_blocks_a_different_agent(client):
    agency = make_agency()
    agent = make_agent(agency.id)
    other_agent = make_agent(agency.id)
    try:
        login_as_agent(client, other_agent.id)
        res = client.get(f"/agent-profile/{agent.id}")
        assert res.status_code == 302
        assert "/agent-login" in res.headers["Location"]
    finally:
        app_module.db.session.delete(agent)
        app_module.db.session.delete(other_agent)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


# ── Agent profile: update ────────────────────────────────────────────

def test_update_agent_profile_saves_changes_for_self(client):
    agency = make_agency()
    agent = make_agent(agency.id)
    try:
        login_as_agent(client, agent.id)
        res = client.post(f"/update-agent-profile/{agent.id}", json={
            "name": "Updated Agent Name", "email": "updatedagent@example.test",
        })
        assert res.status_code == 200
        app_module.db.session.refresh(agent)
        assert agent.name == "Updated Agent Name"
        assert agent.email == "updatedagent@example.test"
    finally:
        app_module.db.session.delete(agent)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_update_agent_profile_owner_can_update_their_agent(client):
    agency = make_agency()
    agent = make_agent(agency.id)
    try:
        login_as_owner(client, agency.id)
        res = client.post(f"/update-agent-profile/{agent.id}", json={
            "name": "Owner Edited", "email": agent.email,
        })
        assert res.status_code == 200
        app_module.db.session.refresh(agent)
        assert agent.name == "Owner Edited"
    finally:
        app_module.db.session.delete(agent)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_update_agent_profile_blocks_a_different_agent(client):
    agency = make_agency()
    agent = make_agent(agency.id)
    other_agent = make_agent(agency.id)
    try:
        login_as_agent(client, other_agent.id)
        res = client.post(f"/update-agent-profile/{agent.id}", json={
            "name": "Hijacked", "email": agent.email,
        })
        assert res.status_code == 401
        app_module.db.session.refresh(agent)
        assert agent.name != "Hijacked"
    finally:
        app_module.db.session.delete(agent)
        app_module.db.session.delete(other_agent)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_update_agent_profile_rejects_duplicate_email_within_agency(client):
    agency = make_agency()
    agent = make_agent(agency.id)
    other_agent = make_agent(agency.id)
    try:
        login_as_agent(client, agent.id)
        res = client.post(f"/update-agent-profile/{agent.id}", json={
            "name": agent.name, "email": other_agent.email,
        })
        assert res.status_code == 400
        assert "already uses this email" in res.get_json()["error"]
    finally:
        app_module.db.session.delete(agent)
        app_module.db.session.delete(other_agent)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_update_agent_profile_rejects_invalid_email(client):
    agency = make_agency()
    agent = make_agent(agency.id)
    try:
        login_as_agent(client, agent.id)
        res = client.post(f"/update-agent-profile/{agent.id}", json={
            "name": agent.name, "email": "nope",
        })
        assert res.status_code == 400
    finally:
        app_module.db.session.delete(agent)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()
