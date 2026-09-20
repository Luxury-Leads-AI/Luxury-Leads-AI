"""The shared activity feed and status-change notifications.

Moaz, Sept 20: "I would like a system where, whenever an agent or agency
owner updates a status, all relevant parties are notified. This should be
handled via email notifications, and there should also be a mechanism to
track these updates directly on the dashboards. When an agency owner or
agent logs in, their dashboard should display the latest updates within
the system."

Both halves matter and neither is sufficient alone: an email is a
notification you can miss, and a feed is one you have to remember to
check. So every status, outcome and note now writes a feed row AND emails
the other side.

The three rules these tests exist to hold:
  1. nobody is emailed about their own click,
  2. each side has its own seen flag, so the owner reading the feed never
     marks it read for the agent,
  3. a failed email never rolls back the status change the user asked for.
"""
import app as app_module

from test_route_guards import (make_agency, make_agent, make_lead,
                               make_appointment, make_listing_row,
                               login_as_owner, login_as_agent)


def spy_email(monkeypatch):
    sent = []

    def fake(to_email, subject, body):
        sent.append({"to": to_email, "subject": subject, "body": body})
        return True

    monkeypatch.setattr(app_module, "send_email_brevo", fake)
    return sent


def events_for(agency_id):
    return (app_module.ActivityEvent.query
            .filter_by(agency_id=agency_id)
            .order_by(app_module.ActivityEvent.id.desc()).all())


def wipe(agency_id):
    app_module.ActivityEvent.query.filter_by(agency_id=agency_id).delete()
    app_module.db.session.commit()


class Setup:
    """An agency, an agent, and a lead + appointment assigned to them."""

    def __enter__(self):
        self.agency = make_agency()
        self.agent = make_agent(self.agency.id)
        self.lead = make_lead(self.agency.id, agent_id=self.agent.id,
                              lead_status='new')
        self.appt = make_appointment(self.agency.id, agent_id=self.agent.id,
                                     customer_email=self.lead.email,
                                     appointment_date="Saturday, September 26, 2026",
                                     appointment_time="6:00 PM")
        return self

    def __exit__(self, *exc):
        wipe(self.agency.id)
        for row in (self.appt, self.lead, self.agent, self.agency):
            app_module.db.session.delete(row)
        app_module.db.session.commit()


# ── an agent acts: the owner hears about it ──────────────────────────

def test_agent_changing_a_lead_status_writes_a_row_and_emails_the_owner(client, monkeypatch):
    sent = spy_email(monkeypatch)
    with Setup() as s:
        login_as_agent(client, s.agent.id)
        res = client.post(f"/agent-update-lead-status/{s.lead.id}",
                          json={"status": "contacted"})
        assert res.status_code == 200

        rows = events_for(s.agency.id)
        assert len(rows) == 1
        e = rows[0]
        assert e.action == 'lead_status'
        assert e.actor_type == 'agent'
        assert e.actor_name == s.agent.name
        assert e.agent_id == s.agent.id
        assert e.subject_type == 'lead' and e.subject_id == s.lead.id
        assert "from New to Contacted" in e.summary, e.summary

        assert [m['to'] for m in sent] == [s.agency.email]
        assert s.agent.email not in [m['to'] for m in sent], \
            "the agent was emailed about their own click"


def test_agent_note_agent_appointment_stage_and_outcome_all_notify_the_owner(client, monkeypatch):
    with Setup() as s:
        login_as_agent(client, s.agent.id)
        for url, payload, action in [
            (f"/agent-add-lead-note/{s.lead.id}", {"note": "Called, keen"}, 'lead_note'),
            (f"/agent-update-appointment-status/{s.appt.id}", {"stage": "confirmed"},
             'appointment_stage'),
            (f"/agent-add-appointment-note/{s.appt.id}", {"note": "Running late"},
             'appointment_note'),
            (f"/agent-set-appointment-outcome/{s.appt.id}", {"outcome": "wants_to_buy"},
             'appointment_outcome'),
        ]:
            sent = spy_email(monkeypatch)
            wipe(s.agency.id)
            res = client.post(url, json=payload)
            assert res.status_code == 200, (url, res.get_json())
            rows = events_for(s.agency.id)
            assert [r.action for r in rows] == [action], (url, [r.action for r in rows])
            assert s.agency.email in [m['to'] for m in sent], f"{url}: owner not told"


# ── the owner acts: the assigned agent hears about it ────────────────

def test_owner_changing_a_lead_status_emails_the_assigned_agent(client, monkeypatch):
    sent = spy_email(monkeypatch)
    with Setup() as s:
        login_as_owner(client, s.agency.id)
        res = client.post(f"/update-lead-status/{s.lead.id}", json={"status": "closed"})
        assert res.status_code == 200

        rows = events_for(s.agency.id)
        assert len(rows) == 1 and rows[0].actor_type == 'owner'
        assert "to Closed" in rows[0].summary

        assert [m['to'] for m in sent] == [s.agent.email]
        assert s.agency.email not in [m['to'] for m in sent], \
            "the owner was emailed about their own click"


def test_owner_note_appointment_stage_and_outcome_all_notify_the_agent(client, monkeypatch):
    with Setup() as s:
        login_as_owner(client, s.agency.id)
        for url, payload, action in [
            (f"/add-lead-note/{s.lead.id}", {"note": "Spoke to them"}, 'lead_note'),
            (f"/update-appointment-status/{s.appt.id}", {"stage": "confirmed"},
             'appointment_stage'),
            (f"/set-appointment-outcome/{s.appt.id}", {"outcome": "not_interested"},
             'appointment_outcome'),
        ]:
            sent = spy_email(monkeypatch)
            wipe(s.agency.id)
            res = client.post(url, json=payload)
            assert res.status_code == 200, (url, res.get_json())
            assert [r.action for r in events_for(s.agency.id)] == [action], url
            assert s.agent.email in [m['to'] for m in sent], f"{url}: agent not told"


def test_an_owner_action_on_an_unassigned_lead_still_lands_in_the_feed(client, monkeypatch):
    """No agent to email, but the owner's own dashboard should still show
    what they did - a feed with holes in it isn't a record."""
    sent = spy_email(monkeypatch)
    agency = make_agency()
    lead = make_lead(agency.id)
    try:
        login_as_owner(client, agency.id)
        client.post(f"/update-lead-status/{lead.id}", json={"status": "contacted"})
        assert len(events_for(agency.id)) == 1
        assert sent == [], "emailed somebody about an unassigned lead"
    finally:
        wipe(agency.id)
        app_module.db.session.delete(lead)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


# ── reassignment reaches both agents ─────────────────────────────────

def test_reassigning_a_viewing_tells_the_agent_who_lost_it_too(client, monkeypatch):
    """Whoever was preparing for Saturday needs to know it isn't theirs
    any more, which is at least as urgent as telling the new agent."""
    sent = spy_email(monkeypatch)
    agency = make_agency()
    first = make_agent(agency.id)
    second = make_agent(agency.id)
    appt = make_appointment(agency.id, agent_id=first.id,
                            appointment_date="Saturday, September 26, 2026")
    try:
        login_as_owner(client, agency.id)
        res = client.post(f"/reassign-appointment/{appt.id}",
                          json={"agent_id": second.id})
        assert res.status_code == 200

        rows = events_for(agency.id)
        assert len(rows) == 2, [r.summary for r in rows]
        assert {r.agent_id for r in rows} == {first.id, second.id}
        recipients = sorted(m['to'] for m in sent)
        assert recipients == sorted([first.email, second.email]), recipients
    finally:
        wipe(agency.id)
        for row in (appt, first, second, agency):
            app_module.db.session.delete(row)
        app_module.db.session.commit()


# ── listing review and customer feedback ─────────────────────────────

def test_approving_a_seller_submission_lands_in_the_feed(client, monkeypatch):
    sent = spy_email(monkeypatch)
    agency = make_agency()
    agent = make_agent(agency.id)
    seller = make_lead(agency.id, agent_id=agent.id, lead_type='seller')
    listing = make_listing_row(agency.id, status='pending', source='seller_chat',
                               seller_lead_id=seller.id)
    try:
        login_as_owner(client, agency.id)
        res = client.post(f"/review-seller-listing/{listing.id}",
                          json={"decision": "approve"})
        assert res.status_code == 200

        rows = events_for(agency.id)
        assert len(rows) == 1
        assert rows[0].action == 'listing_approved'
        assert rows[0].subject_type == 'listing'
        assert rows[0].agent_id == agent.id
        # the seller gets their own "you're live" email; the agent gets
        # the feed notification
        assert agent.email in [m['to'] for m in sent]
        assert seller.email in [m['to'] for m in sent]
    finally:
        wipe(agency.id)
        for row in (listing, seller, agent, agency):
            app_module.db.session.delete(row)
        app_module.db.session.commit()


def test_a_customer_answering_the_checkin_email_lands_in_the_feed(client, monkeypatch):
    sent = spy_email(monkeypatch)
    agency = make_agency()
    agent = make_agent(agency.id)
    appt = make_appointment(agency.id, agent_id=agent.id,
                            checkin_token="feed-token-1")
    try:
        client.get("/appointment-feedback/feed-token-1?choice=other")
        rows = events_for(agency.id)
        assert len(rows) == 1
        assert rows[0].action == 'customer_feedback'
        assert rows[0].actor_type == 'customer'
        # both sides are told: this is the customer speaking, not staff
        recipients = [m['to'] for m in sent]
        assert agency.email in recipients and agent.email in recipients
    finally:
        wipe(agency.id)
        for row in (appt, agent, agency):
            app_module.db.session.delete(row)
        app_module.db.session.commit()


def test_wants_to_buy_does_not_send_two_emails_for_one_event(client, monkeypatch):
    """apply_appointment_outcome already sends its own richer email for
    this one answer. The feed row must not duplicate it."""
    sent = spy_email(monkeypatch)
    agency = make_agency()
    agent = make_agent(agency.id)
    appt = make_appointment(agency.id, agent_id=agent.id,
                            checkin_token="feed-token-2")
    try:
        client.get("/appointment-feedback/feed-token-2?choice=buy")
        assert len(events_for(agency.id)) == 1
        assert [m['to'] for m in sent] == [agent.email], [m['to'] for m in sent]
    finally:
        wipe(agency.id)
        for row in (appt, agent, agency):
            app_module.db.session.delete(row)
        app_module.db.session.commit()


# ── chat-side events go in the feed without a second email ───────────

def test_a_new_lead_from_the_chatbot_is_recorded_without_re_emailing():
    """send_lead_email and the agent's assignment email already fire for
    a new lead; the feed row exists for the dashboard, not the inbox."""
    agency = make_agency()
    try:
        e = app_module.record_activity(
            agency.id, 'lead_new', "New 5-star buyer lead: Sparrow",
            actor_type='system', actor_name='Chatbot',
            subject_type='lead', subject_id=1, subject_name='Sparrow',
            notify=False)
        assert e is not None
        rows = events_for(agency.id)
        assert len(rows) == 1 and rows[0].action == 'lead_new'
        # notify=False means neither seen flag is pre-set: it's news to both
        assert rows[0].seen_by_owner == 0 and rows[0].seen_by_agent == 0
    finally:
        wipe(agency.id)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


# ── seen flags are per side ──────────────────────────────────────────

def test_the_actor_has_already_seen_their_own_action(client, monkeypatch):
    spy_email(monkeypatch)
    with Setup() as s:
        login_as_agent(client, s.agent.id)
        client.post(f"/agent-update-lead-status/{s.lead.id}", json={"status": "contacted"})
        e = events_for(s.agency.id)[0]
        assert e.seen_by_agent == 1, "the agent's own change showed as unread to them"
        assert e.seen_by_owner == 0, "the owner had no unread marker"


def test_the_owner_marking_the_feed_read_leaves_the_agents_count_alone(client, monkeypatch):
    spy_email(monkeypatch)
    with Setup() as s:
        # the owner acts, so it's unread for the agent and read for the owner
        login_as_owner(client, s.agency.id)
        client.post(f"/update-lead-status/{s.lead.id}", json={"status": "contacted"})
        # ...and a system event that's unread for both
        app_module.record_activity(
            s.agency.id, 'lead_new', "New lead", actor_type='system',
            agent_id=s.agent.id, subject_type='lead', subject_id=s.lead.id,
            notify=False)

        assert app_module.unseen_activity_count(s.agency.id) == 1
        assert app_module.unseen_activity_count(s.agency.id, agent_id=s.agent.id) == 2

        res = client.post(f"/mark-activity-seen/{s.agency.id}")
        assert res.status_code == 200
        assert app_module.unseen_activity_count(s.agency.id) == 0
        assert app_module.unseen_activity_count(s.agency.id, agent_id=s.agent.id) == 2, \
            "the owner reading the feed hid an update from the agent"

        login_as_agent(client, s.agent.id)
        assert client.post(f"/agent-mark-activity-seen/{s.agent.id}").status_code == 200
        assert app_module.unseen_activity_count(s.agency.id, agent_id=s.agent.id) == 0


def test_mark_seen_refuses_a_stranger(client):
    agency = make_agency()
    other = make_agency()
    agent = make_agent(agency.id)
    try:
        assert client.post(f"/mark-activity-seen/{agency.id}").status_code == 401
        login_as_owner(client, other.id)
        assert client.post(f"/mark-activity-seen/{agency.id}").status_code == 401
        login_as_agent(client, agent.id)
        assert client.post(f"/agent-mark-activity-seen/{agent.id + 999}").status_code == 401
    finally:
        for row in (agent, other, agency):
            app_module.db.session.delete(row)
        app_module.db.session.commit()


# ── what each dashboard shows ────────────────────────────────────────

def test_an_agent_sees_only_what_touches_their_own_work(client, monkeypatch):
    """The owner's feed is the whole agency; an agent's is their own leads
    and viewings. A feed full of other people's clients is one nobody
    reads."""
    spy_email(monkeypatch)
    agency = make_agency()
    mine = make_agent(agency.id)
    theirs = make_agent(agency.id)
    try:
        app_module.record_activity(agency.id, 'lead_status', "About my lead",
                                   actor_type='owner', agent_id=mine.id, notify=False)
        app_module.record_activity(agency.id, 'lead_status', "About their lead",
                                   actor_type='owner', agent_id=theirs.id, notify=False)
        owner_feed = [e.summary for e in app_module.recent_activity(agency.id)]
        assert len(owner_feed) == 2

        agent_feed = [e.summary for e in
                      app_module.recent_activity(agency.id, agent_id=mine.id)]
        assert agent_feed == ["About my lead"], agent_feed
    finally:
        wipe(agency.id)
        for row in (mine, theirs, agency):
            app_module.db.session.delete(row)
        app_module.db.session.commit()


def test_the_feed_appears_on_both_dashboards_with_its_unread_count(client, monkeypatch):
    spy_email(monkeypatch)
    with Setup() as s:
        app_module.record_activity(
            s.agency.id, 'lead_status',
            "Sara Khan moved Sparrow from New to Contacted",
            actor_type='agent', actor_name='Sara Khan', agent_id=s.agent.id,
            subject_type='lead', subject_id=s.lead.id, subject_name='Sparrow',
            notify=False)

        login_as_owner(client, s.agency.id)
        html = client.get(f"/admin?agency_id={s.agency.id}").get_data(as_text=True)
        assert 'Latest updates' in html
        assert 'Sara Khan moved Sparrow from New to Contacted' in html
        assert 'id="feed-badge"' in html, "no unread count on the owner dashboard"
        assert f'/mark-activity-seen/{s.agency.id}' in html

        login_as_agent(client, s.agent.id)
        html = client.get(f"/agent-dashboard/{s.agent.id}").get_data(as_text=True)
        assert 'Latest updates' in html
        assert 'Sara Khan moved Sparrow from New to Contacted' in html
        assert f'/agent-mark-activity-seen/{s.agent.id}' in html


def test_an_empty_feed_explains_itself_rather_than_showing_nothing(client):
    agency = make_agency()
    try:
        login_as_owner(client, agency.id)
        html = client.get(f"/admin?agency_id={agency.id}").get_data(as_text=True)
        assert 'Latest updates' in html
        assert 'Nothing yet' in html
        assert 'id="feed-badge"' not in html, "an empty feed showed an unread badge"
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_the_panel_opens_itself_when_something_is_waiting(client, monkeypatch):
    """"their dashboard should display the latest updates" - an update
    behind a collapsed panel is one nobody read."""
    spy_email(monkeypatch)
    with Setup() as s:
        login_as_owner(client, s.agency.id)
        html = client.get(f"/admin?agency_id={s.agency.id}").get_data(as_text=True)
        assert 'class="feed-panel"' in html, "expected the panel closed with nothing new"

        app_module.record_activity(s.agency.id, 'lead_status', "Something happened",
                                   actor_type='agent', agent_id=s.agent.id, notify=False)
        html = client.get(f"/admin?agency_id={s.agency.id}").get_data(as_text=True)
        assert 'class="feed-panel open"' in html, \
            "an unread update was left behind a collapsed panel"


def test_each_feed_row_links_to_the_thing_it_is_about(client, monkeypatch):
    spy_email(monkeypatch)
    with Setup() as s:
        app_module.record_activity(s.agency.id, 'lead_status', "A lead moved",
                                   actor_type='agent', agent_id=s.agent.id,
                                   subject_type='lead', subject_id=s.lead.id,
                                   notify=False)
        app_module.record_activity(s.agency.id, 'appointment_stage', "A viewing moved",
                                   actor_type='agent', agent_id=s.agent.id,
                                   subject_type='appointment', subject_id=s.appt.id,
                                   notify=False)
        login_as_owner(client, s.agency.id)
        html = client.get(f"/admin?agency_id={s.agency.id}").get_data(as_text=True)
        assert f'openModal({s.lead.id})' in html
        assert f'/appointments/{s.agency.id}' in html


# ── failure modes ────────────────────────────────────────────────────

def test_a_broken_mail_provider_does_not_undo_the_status_change(client, monkeypatch):
    """The user asked to close a lead. If Brevo is down, the lead is still
    closed and the feed still says so - the notification is the only thing
    that failed."""
    def explode(*a, **kw):
        raise RuntimeError("Brevo is down")

    monkeypatch.setattr(app_module, "send_email_brevo", explode)
    with Setup() as s:
        login_as_agent(client, s.agent.id)
        res = client.post(f"/agent-update-lead-status/{s.lead.id}",
                          json={"status": "closed"})
        assert res.status_code == 200
        app_module.db.session.refresh(s.lead)
        assert s.lead.lead_status == 'closed'
        assert len(events_for(s.agency.id)) == 1


def test_the_acting_identity_comes_from_the_session_not_the_request(client, monkeypatch):
    """A client-supplied actor name would let anyone put anyone's name
    against anyone's change."""
    sent = spy_email(monkeypatch)
    with Setup() as s:
        login_as_agent(client, s.agent.id)
        client.post(f"/agent-update-lead-status/{s.lead.id}",
                    json={"status": "contacted",
                          "actor_name": "The Owner",
                          "actor_type": "owner"})
        e = events_for(s.agency.id)[0]
        assert e.actor_name == s.agent.name
        assert e.actor_type == 'agent'
        assert [m['to'] for m in sent] == [s.agency.email]
