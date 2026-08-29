"""Tests for Item 4: forgot/reset password, shared by Agency owners and
Agents.

Design being tested:
  - /forgot-password always returns the same generic message, whether or
    not the submitted email matches an account (no enumeration).
  - A matching Agency or Agent gets a random, single-use reset_token with
    a 1-hour expiry, and an email is sent (mocked here) with a link
    containing that token.
  - /reset-password/<token> rejects unknown/expired/already-used tokens,
    enforces the same >=6 character rule as the existing change-password
    routes, requires the two password fields to match, and on success
    clears the token (so the same link can't be replayed) and lets the
    account log in with the new password.
"""
from datetime import datetime, timedelta

import app as app_module

from tests.test_route_guards import make_agency, make_agent


def _last_sent_email(monkeypatch):
    """Patch send_email_brevo to capture calls instead of hitting the real
    Brevo API (BREVO_API_KEY isn't set in tests anyway)."""
    sent = []

    def fake_send(to_email, subject, body):
        sent.append({"to": to_email, "subject": subject, "body": body})
        return True

    monkeypatch.setattr(app_module, "send_email_brevo", fake_send)
    return sent


# ── /forgot-password ─────────────────────────────────────────────────

def test_forgot_password_page_renders(client):
    res = client.get("/forgot-password")
    assert res.status_code == 200
    assert "Reset Your Password" in res.get_data(as_text=True)


def test_forgot_password_unknown_email_gives_generic_message(client, monkeypatch):
    sent = _last_sent_email(monkeypatch)
    res = client.post("/forgot-password", data={"email": "nobody-here@example.test"})
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "If an account exists with that email" in html
    assert sent == []


def test_forgot_password_matching_agency_sends_email_and_sets_token(client, monkeypatch):
    sent = _last_sent_email(monkeypatch)
    agency = make_agency()
    try:
        res = client.post("/forgot-password", data={"email": agency.email})
        assert res.status_code == 200
        html = res.get_data(as_text=True)
        # Same generic message as the unknown-email case - no enumeration.
        assert "If an account exists with that email" in html

        assert len(sent) == 1
        assert sent[0]["to"] == agency.email

        app_module.db.session.refresh(agency)
        assert agency.reset_token
        assert agency.reset_token_expires > datetime.utcnow()
        assert f"/reset-password/{agency.reset_token}" in sent[0]["body"]
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_forgot_password_matching_agent_sends_email_and_sets_token(client, monkeypatch):
    sent = _last_sent_email(monkeypatch)
    agency = make_agency()
    agent = make_agent(agency.id)
    try:
        res = client.post("/forgot-password", data={"email": agent.email})
        assert res.status_code == 200
        assert len(sent) == 1
        assert sent[0]["to"] == agent.email

        app_module.db.session.refresh(agent)
        assert agent.reset_token
        assert agent.reset_token_expires > datetime.utcnow()
    finally:
        app_module.db.session.delete(agent)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_forgot_password_email_lookup_is_case_insensitive(client, monkeypatch):
    sent = _last_sent_email(monkeypatch)
    agency = make_agency()
    try:
        res = client.post("/forgot-password", data={"email": agency.email.upper()})
        assert res.status_code == 200
        assert len(sent) == 1
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


# ── /reset-password/<token> ──────────────────────────────────────────

def test_reset_password_unknown_token_shows_invalid(client):
    res = client.get("/reset-password/not-a-real-token")
    assert res.status_code == 200
    html = res.get_data(as_text=True)
    assert "invalid or has expired" in html.lower()


def test_reset_password_expired_token_shows_invalid(client):
    agency = make_agency()
    agency.reset_token = "expired-token-123"
    agency.reset_token_expires = datetime.utcnow() - timedelta(hours=1)
    app_module.db.session.commit()
    try:
        res = client.get("/reset-password/expired-token-123")
        html = res.get_data(as_text=True)
        assert "invalid or has expired" in html.lower()

        # An expired token also can't be POSTed through.
        res = client.post("/reset-password/expired-token-123", data={
            "new_password": "brandnewpw", "confirm_password": "brandnewpw",
        })
        html = res.get_data(as_text=True)
        assert "invalid or has expired" in html.lower()
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_reset_password_valid_token_renders_form(client):
    agency = make_agency()
    agency.reset_token = "valid-token-456"
    agency.reset_token_expires = datetime.utcnow() + timedelta(hours=1)
    app_module.db.session.commit()
    try:
        res = client.get("/reset-password/valid-token-456")
        html = res.get_data(as_text=True)
        assert "Set a New Password" in html
        assert "invalid or has expired" not in html.lower()
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_reset_password_rejects_short_password(client):
    agency = make_agency()
    agency.reset_token = "short-pw-token"
    agency.reset_token_expires = datetime.utcnow() + timedelta(hours=1)
    app_module.db.session.commit()
    try:
        res = client.post("/reset-password/short-pw-token", data={
            "new_password": "abc", "confirm_password": "abc",
        })
        html = res.get_data(as_text=True)
        assert "at least 6 characters" in html.lower()
        app_module.db.session.refresh(agency)
        assert agency.reset_token == "short-pw-token"  # untouched - reset never happened
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_reset_password_rejects_mismatched_passwords(client):
    agency = make_agency()
    agency.reset_token = "mismatch-token"
    agency.reset_token_expires = datetime.utcnow() + timedelta(hours=1)
    app_module.db.session.commit()
    try:
        res = client.post("/reset-password/mismatch-token", data={
            "new_password": "goodpassword", "confirm_password": "differentpassword",
        })
        html = res.get_data(as_text=True)
        assert "do not match" in html.lower()
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_reset_password_success_for_agency_updates_password_and_clears_token(client):
    agency = make_agency()
    agency.set_password("oldpassword")
    agency.reset_token = "agency-success-token"
    agency.reset_token_expires = datetime.utcnow() + timedelta(hours=1)
    app_module.db.session.commit()
    try:
        res = client.post("/reset-password/agency-success-token", data={
            "new_password": "newpassword1", "confirm_password": "newpassword1",
        })
        html = res.get_data(as_text=True)
        assert "password has been updated" in html.lower()
        assert '/owner-login' in html

        app_module.db.session.refresh(agency)
        assert agency.reset_token is None
        assert agency.reset_token_expires is None
        assert agency.check_password("newpassword1")
        assert not agency.check_password("oldpassword")

        # Login works with the new password.
        login_res = client.post("/owner-login", data={
            "agency_id": str(agency.id), "password": "newpassword1",
        })
        assert login_res.status_code == 302
        assert "/admin" in login_res.headers["Location"]
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_reset_password_success_for_agent_updates_password_and_clears_token(client):
    agency = make_agency()
    agent = make_agent(agency.id)
    agent.reset_token = "agent-success-token"
    agent.reset_token_expires = datetime.utcnow() + timedelta(hours=1)
    app_module.db.session.commit()
    try:
        res = client.post("/reset-password/agent-success-token", data={
            "new_password": "newagentpw1", "confirm_password": "newagentpw1",
        })
        html = res.get_data(as_text=True)
        assert "password has been updated" in html.lower()
        assert '/agent-login' in html

        app_module.db.session.refresh(agent)
        assert agent.reset_token is None
        assert agent.reset_token_expires is None
        assert agent.check_password("newagentpw1")

        login_res = client.post("/agent-login", data={
            "email": agent.email, "password": "newagentpw1",
        })
        assert login_res.status_code == 302
        assert "/agent-dashboard" in login_res.headers["Location"]
    finally:
        app_module.db.session.delete(agent)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_reset_password_token_is_single_use(client):
    agency = make_agency()
    agency.reset_token = "single-use-token"
    agency.reset_token_expires = datetime.utcnow() + timedelta(hours=1)
    app_module.db.session.commit()
    try:
        first = client.post("/reset-password/single-use-token", data={
            "new_password": "firstpassword", "confirm_password": "firstpassword",
        })
        assert "password has been updated" in first.get_data(as_text=True).lower()

        # Replaying the same link should now show it as invalid.
        second = client.get("/reset-password/single-use-token")
        assert "invalid or has expired" in second.get_data(as_text=True).lower()
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()
