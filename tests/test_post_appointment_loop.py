"""Tests for Item 6: the post-appointment loop.

Design (Moaz, 2026-08-28):
  - After an appointment's time passes, the customer gets an automated
    check-in email with three choices, AND the agent/owner can record the
    same outcome manually (e.g. a phone call).
  - "Wants to buy": notify the agent only, no pipeline change, no
    e-signature. The auto-notify only fires when a CUSTOMER's own click
    sets the outcome - a human recording it manually already knows.
  - "Wants other options": fully automated hand-off - the feedback page
    embeds the same AI chat widget a first-time visitor gets, so there's
    no separate matching logic to test here beyond confirming the widget
    is embedded for the right agency.
"""
from datetime import datetime, timedelta

import pytz

import app as app_module

from tests.test_route_guards import (
    make_agency, make_agent, make_appointment, login_as_owner, login_as_agent,
)


def _last_sent_email(monkeypatch):
    sent = []

    def fake_send(to_email, subject, body):
        sent.append({"to": to_email, "subject": subject, "body": body})
        return True

    monkeypatch.setattr(app_module, "send_email_brevo", fake_send)
    return sent


def _karachi_now():
    return datetime.now(pytz.timezone('Asia/Karachi'))


# ── _appointment_datetime helper ─────────────────────────────────────

def test_appointment_datetime_parses_iso_date_and_slot_time():
    agency = make_agency()
    appt = make_appointment(agency.id, appointment_date_iso="2020-01-01", appointment_time="2:00 PM")
    try:
        dt = app_module._appointment_datetime(appt)
        assert dt is not None
        assert dt.year == 2020 and dt.month == 1 and dt.day == 1
        assert dt.hour == 14
    finally:
        app_module.db.session.delete(appt)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_appointment_datetime_returns_none_when_missing_or_unparseable():
    agency = make_agency()
    appt = make_appointment(agency.id, appointment_date_iso=None, appointment_time="2:00 PM")
    appt2 = make_appointment(agency.id, appointment_date_iso="2020-01-01", appointment_time="garbage")
    try:
        assert app_module._appointment_datetime(appt) is None
        assert app_module._appointment_datetime(appt2) is None
    finally:
        app_module.db.session.delete(appt)
        app_module.db.session.delete(appt2)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


# ── process_appointment_checkins ─────────────────────────────────────

def test_checkin_email_sent_for_a_past_appointment_with_no_outcome(monkeypatch):
    sent = _last_sent_email(monkeypatch)
    agency = make_agency()
    past = _karachi_now() - timedelta(days=2)
    appt = make_appointment(
        agency.id,
        customer_email="pastcustomer@example.test",
        appointment_date_iso=past.strftime("%Y-%m-%d"),
        appointment_time="10:00 AM",
        status="completed",
    )
    try:
        result = app_module.process_appointment_checkins()
        assert result.get("checkins_sent") == 1
        assert len(sent) == 1
        assert sent[0]["to"] == "pastcustomer@example.test"

        app_module.db.session.refresh(appt)
        assert appt.checkin_token
        assert appt.checkin_sent_at is not None
        assert f"/appointment-feedback/{appt.checkin_token}" in sent[0]["body"]
    finally:
        app_module.db.session.delete(appt)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_checkin_email_not_sent_for_a_future_appointment(monkeypatch):
    sent = _last_sent_email(monkeypatch)
    agency = make_agency()
    future = _karachi_now() + timedelta(days=5)
    appt = make_appointment(
        agency.id,
        customer_email="futurecustomer@example.test",
        appointment_date_iso=future.strftime("%Y-%m-%d"),
        appointment_time="10:00 AM",
    )
    try:
        result = app_module.process_appointment_checkins()
        assert result.get("checkins_sent") == 0
        assert sent == []
        app_module.db.session.refresh(appt)
        assert appt.checkin_sent_at is None
    finally:
        app_module.db.session.delete(appt)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_checkin_email_not_sent_for_a_cancelled_appointment(monkeypatch):
    sent = _last_sent_email(monkeypatch)
    agency = make_agency()
    past = _karachi_now() - timedelta(days=2)
    appt = make_appointment(
        agency.id,
        customer_email="cancelled@example.test",
        appointment_date_iso=past.strftime("%Y-%m-%d"),
        appointment_time="10:00 AM",
        status="cancelled",
    )
    try:
        result = app_module.process_appointment_checkins()
        assert result.get("checkins_sent") == 0
        assert sent == []
    finally:
        app_module.db.session.delete(appt)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_checkin_email_not_resent_once_outcome_or_checkin_already_recorded(monkeypatch):
    sent = _last_sent_email(monkeypatch)
    agency = make_agency()
    past = _karachi_now() - timedelta(days=2)
    already_answered = make_appointment(
        agency.id, customer_email="answered@example.test",
        appointment_date_iso=past.strftime("%Y-%m-%d"), appointment_time="10:00 AM",
        outcome="not_interested",
    )
    already_sent = make_appointment(
        agency.id, customer_email="alreadysent@example.test",
        appointment_date_iso=past.strftime("%Y-%m-%d"), appointment_time="10:00 AM",
        checkin_sent_at=datetime.utcnow(),
    )
    try:
        result = app_module.process_appointment_checkins()
        assert result.get("checkins_sent") == 0
        assert sent == []
    finally:
        app_module.db.session.delete(already_answered)
        app_module.db.session.delete(already_sent)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


# ── /appointment-feedback/<token> ────────────────────────────────────

def test_feedback_page_unknown_token_is_invalid(client):
    res = client.get("/appointment-feedback/not-a-real-token")
    assert res.status_code == 200
    assert "isn't valid" in res.get_data(as_text=True)


def test_feedback_page_with_no_choice_shows_three_options(client):
    agency = make_agency()
    appt = make_appointment(agency.id, checkin_token="fb-token-1")
    try:
        res = client.get("/appointment-feedback/fb-token-1")
        html = res.get_data(as_text=True)
        assert "choice=buy" in html
        assert "choice=other" in html
        assert "choice=no" in html
    finally:
        app_module.db.session.delete(appt)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_feedback_page_buy_choice_records_outcome_and_notifies_agent(client, monkeypatch):
    sent = _last_sent_email(monkeypatch)
    agency = make_agency()
    agent = make_agent(agency.id)
    appt = make_appointment(agency.id, checkin_token="fb-token-buy", agent_id=agent.id)
    try:
        res = client.get("/appointment-feedback/fb-token-buy?choice=buy")
        html = res.get_data(as_text=True)
        assert res.status_code == 200
        assert "Great news" in html

        app_module.db.session.refresh(appt)
        assert appt.outcome == "wants_to_buy"
        assert appt.outcome_source == "customer"
        assert appt.outcome_at is not None

        assert len(sent) == 1
        assert sent[0]["to"] == agent.email
        assert "Test Customer" in sent[0]["body"]
    finally:
        app_module.db.session.delete(appt)
        app_module.db.session.delete(agent)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_feedback_page_buy_choice_falls_back_to_agency_email_with_no_agent(client, monkeypatch):
    sent = _last_sent_email(monkeypatch)
    agency = make_agency()
    appt = make_appointment(agency.id, checkin_token="fb-token-buy-noagent")
    try:
        client.get("/appointment-feedback/fb-token-buy-noagent?choice=buy")
        assert len(sent) == 1
        assert sent[0]["to"] == agency.email
    finally:
        app_module.db.session.delete(appt)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_feedback_page_other_choice_embeds_the_chat_widget_for_this_agency(client):
    agency = make_agency()
    appt = make_appointment(agency.id, checkin_token="fb-token-other")
    try:
        res = client.get("/appointment-feedback/fb-token-other?choice=other")
        html = res.get_data(as_text=True)
        assert "Let's find you something else" in html
        assert f'data-agency="{agency.id}"' in html
        assert "widget.js" in html

        app_module.db.session.refresh(appt)
        assert appt.outcome == "wants_other_options"
    finally:
        app_module.db.session.delete(appt)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_feedback_page_not_interested_choice_records_outcome_without_notifying(client, monkeypatch):
    sent = _last_sent_email(monkeypatch)
    agency = make_agency()
    appt = make_appointment(agency.id, checkin_token="fb-token-no")
    try:
        res = client.get("/appointment-feedback/fb-token-no?choice=no")
        html = res.get_data(as_text=True)
        assert "Thanks for letting us know" in html
        assert sent == []

        app_module.db.session.refresh(appt)
        assert appt.outcome == "not_interested"
    finally:
        app_module.db.session.delete(appt)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_feedback_page_outcome_is_not_overwritten_on_a_second_visit(client, monkeypatch):
    sent = _last_sent_email(monkeypatch)
    agency = make_agency()
    appt = make_appointment(agency.id, checkin_token="fb-token-idempotent")
    try:
        client.get("/appointment-feedback/fb-token-idempotent?choice=no")
        sent.clear()
        # Clicking a DIFFERENT choice on the same link afterwards must not
        # flip the recorded outcome or re-fire any notification.
        res = client.get("/appointment-feedback/fb-token-idempotent?choice=buy")
        html = res.get_data(as_text=True)
        assert "Thanks for letting us know" in html  # still shows 'not_interested'
        assert sent == []

        app_module.db.session.refresh(appt)
        assert appt.outcome == "not_interested"
    finally:
        app_module.db.session.delete(appt)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


# ── /set-appointment-outcome (owner) ─────────────────────────────────

def test_set_appointment_outcome_requires_login(client):
    agency = make_agency()
    appt = make_appointment(agency.id)
    try:
        res = client.post(f"/set-appointment-outcome/{appt.id}", json={"outcome": "wants_to_buy"})
        assert res.status_code == 401
    finally:
        app_module.db.session.delete(appt)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_set_appointment_outcome_owner_can_record_manually_without_auto_email(client, monkeypatch):
    sent = _last_sent_email(monkeypatch)
    agency = make_agency()
    appt = make_appointment(agency.id)
    try:
        login_as_owner(client, agency.id)
        res = client.post(f"/set-appointment-outcome/{appt.id}", json={"outcome": "wants_to_buy"})
        assert res.status_code == 200
        assert res.get_json()["success"] is True

        app_module.db.session.refresh(appt)
        assert appt.outcome == "wants_to_buy"
        assert appt.outcome_source == "owner"
        # Manual entries don't trigger the auto-notify - the person
        # recording it already knows.
        assert sent == []
    finally:
        app_module.db.session.delete(appt)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_set_appointment_outcome_rejects_invalid_value(client):
    agency = make_agency()
    appt = make_appointment(agency.id)
    try:
        login_as_owner(client, agency.id)
        res = client.post(f"/set-appointment-outcome/{appt.id}", json={"outcome": "maybe"})
        assert res.status_code == 400
    finally:
        app_module.db.session.delete(appt)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_set_appointment_outcome_blocks_a_different_agency(client):
    agency = make_agency()
    other_agency = make_agency()
    appt = make_appointment(agency.id)
    try:
        login_as_owner(client, other_agency.id)
        res = client.post(f"/set-appointment-outcome/{appt.id}", json={"outcome": "wants_to_buy"})
        assert res.status_code == 401
        app_module.db.session.refresh(appt)
        assert appt.outcome is None
    finally:
        app_module.db.session.delete(appt)
        app_module.db.session.delete(agency)
        app_module.db.session.delete(other_agency)
        app_module.db.session.commit()


# ── /agent-set-appointment-outcome ───────────────────────────────────

def test_agent_set_appointment_outcome_for_own_appointment(client, monkeypatch):
    sent = _last_sent_email(monkeypatch)
    agency = make_agency()
    agent = make_agent(agency.id)
    appt = make_appointment(agency.id, agent_id=agent.id)
    try:
        login_as_agent(client, agent.id)
        res = client.post(f"/agent-set-appointment-outcome/{appt.id}", json={"outcome": "not_interested"})
        assert res.status_code == 200

        app_module.db.session.refresh(appt)
        assert appt.outcome == "not_interested"
        assert appt.outcome_source == "agent"
        assert sent == []
    finally:
        app_module.db.session.delete(appt)
        app_module.db.session.delete(agent)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_agent_set_appointment_outcome_blocks_a_different_agents_appointment(client):
    agency = make_agency()
    agent = make_agent(agency.id)
    other_agent = make_agent(agency.id)
    appt = make_appointment(agency.id, agent_id=agent.id)
    try:
        login_as_agent(client, other_agent.id)
        res = client.post(f"/agent-set-appointment-outcome/{appt.id}", json={"outcome": "wants_to_buy"})
        assert res.status_code == 403
        app_module.db.session.refresh(appt)
        assert appt.outcome is None
    finally:
        app_module.db.session.delete(appt)
        app_module.db.session.delete(agent)
        app_module.db.session.delete(other_agent)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


# ── /send-followups wiring ───────────────────────────────────────────

def test_send_followups_route_also_runs_appointment_checkins(client, monkeypatch):
    _last_sent_email(monkeypatch)
    res = client.get("/send-followups")
    assert res.status_code == 200
    data = res.get_json()
    assert "appointment_checkins" in data["results"]
    assert "checkins_sent" in data["results"]["appointment_checkins"]
