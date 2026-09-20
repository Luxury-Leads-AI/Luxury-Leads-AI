"""Timeline capture and the reworked lead score (Moaz's Sept 20 note).

He ran two live chats - one as a seller, one as a buyer - and spotted the
gap: "the AI is not collecting timeline-related data from either the buyer
or the seller, even though the timeline is crucial for assessing lead
quality."

He was right twice over. The bot never asked, AND the score couldn't have
used the answer if it had one. The old scoring was:

    score = 1 + has_name + has_budget + has_phone      (capped at 5)
    + 1 if any of ['asap','urgent','soon','quickly','this week',
                   'this month','within','month','week'] appears
        anywhere the customer typed

Three things wrong with that. The name is the first thing the bot asks,
so every lead had one - a free point that distinguished nobody. The
urgency bonus fired on any sentence containing "month" or "week",
including "I'm on a 12 month lease" and "see you next week", and could
not tell "moving in three weeks" from "maybe in three years". And a
booked viewing - a customer putting their own Saturday evening on the
line - was worth nothing at all.

What's here pins the new rubric, the timeline parsing (including the
phrases that used to be scored as urgent when they mean the opposite),
and the fact that the score now carries its own reasoning.
"""
import json
import uuid

import app as app_module

from test_route_guards import (make_agency, make_agent, login_as_owner,
                               login_as_agent)


# ── helpers ──────────────────────────────────────────────────────────

def chat(*pairs):
    """chat(('assistant', '...'), ('user', '...')) -> history list."""
    return [{"role": role, "content": text} for role, text in pairs]


def asked_timeline(answer):
    return chat(("assistant", "How soon are you looking to move?"),
                ("user", answer))


def score(**lead_data):
    history = lead_data.pop('history', None) or chat(("user", "hi"))
    booking = lead_data.pop('has_booking', False)
    kind = lead_data.pop('lead_type', 'buyer')
    prop = lead_data.pop('seller_property', None)
    base = {'name': None, 'email': None, 'phone': None, 'whatsapp_number': None,
            'budget': None, 'timeline': None, 'budget_inferred': False}
    base.update(lead_data)
    return app_module.score_lead_quality(base, history, has_booking=booking,
                                         lead_type=kind, seller_property=prop)


# ── the bot has to ask ───────────────────────────────────────────────

def test_the_buyer_question_sequence_includes_the_timeline():
    """It sits after the budget and before the email: asking someone when
    they want to move before knowing what they can spend is backwards."""
    src = open(app_module.__file__, encoding='utf-8').read()
    seq = src[src.index('INFORMATION TO COLLECT FROM A BUYER'):]
    seq = seq[:seq.index('NEVER combine location and budget')]
    assert 'Timeline' in seq, "buyer sequence still never asks about timing"
    assert seq.index('Budget ONLY') < seq.index('Timeline ONLY') < seq.index('Email')


def test_the_seller_question_sequence_includes_the_timeline():
    src = open(app_module.__file__, encoding='utf-8').read()
    seq = src[src.index('INFORMATION TO COLLECT FROM THIS OWNER'):]
    seq = seq[:seq.index('If they volunteer several of these')]
    assert 'Timeline' in seq, "seller sequence still never asks about timing"


def test_the_prompt_accepts_no_rush_as_a_complete_answer():
    """A lead-gen bot that pushes for a firmer date than the customer has
    is how a warm lead becomes an annoyed one."""
    src = open(app_module.__file__, encoding='utf-8').read()
    assert 'Never ask a second time' in src
    assert 'never imply urgency they didn' in src


def test_timeline_question_is_recognised_however_it_is_phrased():
    yes = ["How soon are you looking to move?",
           "What's your timeline for this?",
           "When are you hoping to sell?",
           "How quickly do you need to be in?",
           "And what time frame are you working with?"]
    for q in yes:
        assert app_module.is_timeline_question(q), q
    no = ["What's your budget?", "Any particular area in mind?",
          "Best way to reach you - WhatsApp, phone, or email?",
          "Which day works for you?"]
    for q in no:
        assert not app_module.is_timeline_question(q), q


# ── parsing what they said ───────────────────────────────────────────

def test_timeline_buckets_the_common_answers():
    cases = {
        "ASAP": 'immediate',
        "as soon as possible really": 'immediate',
        "I need to be in by the end of the month": 'immediate',
        "in 3 weeks": 'immediate',
        "next month": '1_3_months',
        "within the next two months hopefully": '1_3_months',
        "a couple of months": '1_3_months',
        "end of the year": '3_6_months',
        "next year": '6_12_months',
        "within a year": '6_12_months',
        "maybe in a couple of years": 'over_1_year',
        "18 months": 'over_1_year',
    }
    for answer, expected in cases.items():
        bucket, raw = app_module.extract_timeline(asked_timeline(answer))
        assert bucket == expected, f"{answer!r} -> {bucket}, expected {expected}"
        assert raw == answer, "the customer's own words should be kept"


def test_a_range_is_bucketed_on_its_later_end():
    """"3 to 6 months" is someone who might still be shopping in six
    months. An agent planning their week deserves the honest date, not
    the optimistic one."""
    assert app_module.extract_timeline(asked_timeline("3 to 6 months"))[0] == '3_6_months'
    assert app_module.extract_timeline(asked_timeline("6-8 weeks"))[0] == '1_3_months'


def test_browsing_is_not_mistaken_for_urgency():
    """The old keyword list contained 'soon' and 'within'. "No rush, just
    looking" scored the same +1 as "I need to move this week"."""
    for answer in ["just looking for now", "no rush", "we're not in a hurry",
                   "just browsing", "no particular timeline yet"]:
        bucket, _ = app_module.extract_timeline(asked_timeline(answer))
        assert bucket == 'browsing', f"{answer!r} -> {bucket}"
        assert app_module.TIMELINE_POINTS['browsing'] == 0


def test_a_lease_length_is_not_read_as_a_moving_date():
    """This is the false positive the old urgency scan produced: the word
    'month' in a sentence about something else."""
    history = chat(("assistant", "Any particular area in mind?"),
                   ("user", "Manhattan. I'm on a 12 month lease at the moment."))
    bucket, _ = app_module.extract_timeline(history)
    # 12 months is the honest reading of the only duration stated, and it
    # is emphatically NOT 'immediate' the way the old scan had it.
    assert bucket != 'immediate'


def test_the_answer_to_the_question_beats_anything_said_elsewhere():
    history = chat(("user", "hi, I saw you next week at the open house"),
                   ("assistant", "How soon are you looking to move?"),
                   ("user", "honestly not for a couple of years"))
    bucket, raw = app_module.extract_timeline(history)
    assert bucket == 'over_1_year'
    assert raw == "honestly not for a couple of years"


def test_no_timeline_anywhere_returns_nothing_rather_than_guessing():
    history = chat(("assistant", "What's your budget?"), ("user", "about 10M"))
    assert app_module.extract_timeline(history) == (None, None)
    assert app_module.timeline_label(None) == 'Not given'


# ── the score itself ─────────────────────────────────────────────────

def test_timeline_moves_the_score():
    """Same lead, same everything, different answer to one question."""
    common = dict(email="a@b.test", whatsapp_number="+96548721335",
                  budget="10M USD", has_booking=True,
                  history=chat(*[("user", f"m{i}") for i in range(9)]))
    soon, _ = score(timeline='immediate', **common)
    later, _ = score(timeline='over_1_year', **common)
    unknown, _ = score(**common)
    assert soon == 5
    assert later < soon, "a lead moving next year outranked one moving next week"
    assert unknown < soon, "not knowing counted the same as knowing it's urgent"


def test_moaz_chat_2_the_booked_buyer_who_was_never_asked():
    """His live buyer chat, scored. Contactable, funded, booked, engaged -
    but nobody asked when. A strong 4, and a 5 the moment the timeline
    question lands."""
    history = chat(*[("user", t) for t in [
        "hi", "I am Sparrow", "I am looking for a villa", "I want to buy",
        "I prefer NewYork", "Its up to 10M $", "Yes sure",
        "Yes I would love to", "Saturday, September 26", "6PM",
        "vedocim573@dreameg.com", "Email and whatsapp", "+96548721335"]])
    without, reasons = score(email="vedocim573@dreameg.com",
                             whatsapp_number="+96548721335", budget="10 million USD",
                             has_booking=True, history=history)
    assert without == 4, f"expected 4, got {without}: {reasons}"
    with_timeline, _ = score(email="vedocim573@dreameg.com",
                             whatsapp_number="+96548721335", budget="10 million USD",
                             has_booking=True, timeline='immediate', history=history)
    assert with_timeline == 5


def test_moaz_chat_1_the_seller_with_no_number_and_no_date():
    """His live seller chat. Good property, reachable by email only, no
    timeline: a genuine middling lead, and the score says so."""
    history = chat(*[("user", t) for t in [
        "Hello there", "I am Zulfi", "I want to sell my propery",
        "Its in NewYork", "Its a villa", "It has 6 beds and 6 baths",
        "pool, solar, cinema, gym, BBQ", "My asking is 10M$",
        "pahih80223@duidir.com", "Email And WhatsApp"]])
    s, reasons = score(email="pahih80223@duidir.com", budget="10 million USD",
                       lead_type='seller',
                       seller_property={'location': 'New York',
                                        'price_raw': '10M $'},
                       history=history)
    assert s == 3, f"expected 3, got {s}: {reasons}"
    flat = " ".join(why for _, why in reasons)
    assert "No phone or WhatsApp number" in flat
    assert "Timeline: not given" in flat
    # A seller says an asking price, not a budget.
    assert "Asking price stated" in flat


def test_a_name_alone_no_longer_earns_anything():
    """The bot asks for the name first, so every lead has one. Giving it a
    point meant the floor was 2 for anybody who said hello."""
    s, reasons = score(name="Someone")
    assert s == 1
    assert not any('name' in why.lower() for _, why in reasons)


def test_a_booked_viewing_counts_for_the_buyer():
    base = dict(email="a@b.test", budget="5M")
    assert score(has_booking=True, **base)[0] > score(**base)[0]


def test_a_stated_budget_beats_an_inferred_one():
    stated, _ = score(email="a@b.test", budget="10M USD")
    inferred, reasons = score(email="a@b.test", budget="10M USD", budget_inferred=True)
    assert stated > inferred
    assert any('inferred' in why for _, why in reasons)


def test_a_seller_with_half_a_property_scores_below_a_complete_one():
    base = dict(email="a@b.test", budget="10M", lead_type='seller')
    full, _ = score(seller_property={'location': 'NY', 'price_raw': '10M'}, **base)
    part, _ = score(seller_property={'location': 'NY'}, **base)
    none, _ = score(seller_property={}, **base)
    assert full > part >= none


def test_every_score_carries_its_reasoning():
    """"Why is this a 3?" needs an answer on the card, not an argument
    with a number."""
    s, reasons = score(email="a@b.test", budget="5M", timeline='1_3_months')
    assert len(reasons) == 6, reasons
    for points, why in reasons:
        assert points in ('0', '+1', '+2', '+3'), points
        assert why and why[0].isupper(), why


def test_the_old_entry_point_still_returns_a_bare_number():
    """analyze_lead_quality is called from two places and by nothing else;
    keeping it as a wrapper means the rework isn't also a rename."""
    assert app_module.analyze_lead_quality(
        {'email': 'a@b.test', 'budget': '5M'}, chat(("user", "hi"))) in range(1, 6)


# ── it reaches the database, the dashboard and the exports ───────────

def test_extract_lead_data_returns_the_timeline_with_everything_else():
    history = chat(("user", "I'm Ana, ana@example.test, budget 2M"),
                   ("assistant", "How soon are you looking to move?"),
                   ("user", "within two months"))
    data = app_module.extract_lead_data(999999, history)
    assert data['timeline'] == '1_3_months'
    assert data['timeline_raw'] == "within two months"


def test_the_lead_row_can_store_a_timeline_and_its_reasoning():
    agency = make_agency()
    lead = app_module.Lead(
        agency_id=agency.id, name="Ana", email=f"{uuid.uuid4().hex[:8]}@example.test",
        timeline='1_3_months', timeline_raw="within two months",
        quality_reasons=json.dumps([["+2", "Budget stated: 2M"]]))
    app_module.db.session.add(lead)
    app_module.db.session.commit()
    try:
        app_module.db.session.refresh(lead)
        assert lead.timeline == '1_3_months'
        assert lead.timeline_raw == "within two months"
        assert json.loads(lead.quality_reasons)[0][1].startswith("Budget stated")
    finally:
        app_module.db.session.delete(lead)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_lead_detail_serves_the_timeline_and_the_breakdown(client):
    agency = make_agency()
    lead = app_module.Lead(
        agency_id=agency.id, name="Ana", email=f"{uuid.uuid4().hex[:8]}@example.test",
        timeline='immediate', timeline_raw="ASAP",
        quality_reasons=json.dumps([["+3", "Timeline: Immediately / within a month"]]))
    app_module.db.session.add(lead)
    app_module.db.session.commit()
    try:
        login_as_owner(client, agency.id)
        d = client.get(f"/get-lead-detail/{lead.id}").get_json()
        assert d['timeline'] == 'Immediately / within a month'
        assert d['timeline_raw'] == 'ASAP'
        assert d['quality_reasons'] == [
            {"points": "+3", "reason": "Timeline: Immediately / within a month"}]
        assert d['lead_type'] == 'buyer'
    finally:
        app_module.db.session.delete(lead)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_a_lead_captured_before_this_says_so_instead_of_inventing_reasons(client):
    agency = make_agency()
    lead = app_module.Lead(agency_id=agency.id, name="Old",
                            email=f"{uuid.uuid4().hex[:8]}@example.test",
                            intent_score=4)
    app_module.db.session.add(lead)
    app_module.db.session.commit()
    try:
        login_as_owner(client, agency.id)
        d = client.get(f"/get-lead-detail/{lead.id}").get_json()
        assert d['quality_reasons'] == []
        assert d['timeline'] == 'Not given'
    finally:
        app_module.db.session.delete(lead)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_the_lead_email_to_the_owner_states_the_timeline(monkeypatch):
    sent = []
    monkeypatch.setattr(app_module, 'send_email_brevo',
                        lambda to, subject, body: sent.append(
                            {'to': to, 'subject': subject, 'body': body}) or True)
    agency = make_agency()
    lead = app_module.Lead(
        agency_id=agency.id, name="Ana", email="ana@example.test",
        budget="2M USD", intent_score=4, timeline='1_3_months',
        timeline_raw="within two months",
        quality_reasons=json.dumps([["+2", "Budget stated: 2M USD"],
                                    ["+2", "Timeline: 1-3 months"]]))
    app_module.db.session.add(lead)
    app_module.db.session.commit()
    try:
        app_module.send_lead_email(agency, lead)
        assert len(sent) == 1
        body = sent[0]['body']
        assert '1-3 months' in body
        assert 'within two months' in body, "the customer's own words are missing"
        assert 'Budget stated: 2M USD' in body, "the score has no working shown"
    finally:
        app_module.db.session.delete(lead)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_the_excel_export_has_a_timeline_column(client):
    agency = make_agency()
    lead = app_module.Lead(agency_id=agency.id, name="Ana",
                            email=f"{uuid.uuid4().hex[:8]}@example.test",
                            timeline='immediate', lead_type='seller')
    app_module.db.session.add(lead)
    app_module.db.session.commit()
    try:
        login_as_owner(client, agency.id)
        res = client.get(f"/export/{agency.id}")
        assert res.status_code == 200
        import io as _io
        from openpyxl import load_workbook
        ws = load_workbook(_io.BytesIO(res.data)).active
        header = [c.value for c in ws[1]]
        assert 'Timeline' in header
        assert 'Type' in header
        row = [c.value for c in ws[2]]
        assert row[header.index('Timeline')] == 'Immediately / within a month'
        assert row[header.index('Type')] == 'Seller'
    finally:
        app_module.db.session.delete(lead)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


# ── the dashboards show it ───────────────────────────────────────────

def test_both_lead_tables_show_a_timeline_column(client):
    agency = make_agency()
    agent = make_agent(agency.id)
    lead = app_module.Lead(agency_id=agency.id, agent_id=agent.id, name="Ana",
                            email=f"{uuid.uuid4().hex[:8]}@example.test",
                            timeline='immediate', timeline_raw="ASAP")
    app_module.db.session.add(lead)
    app_module.db.session.commit()
    try:
        login_as_owner(client, agency.id)
        html = client.get(f"/admin?agency_id={agency.id}").get_data(as_text=True)
        assert 'Timeline' in html
        assert 'Immediately / within a month' in html
        assert 'title="ASAP"' in html, "the customer's own words aren't surfaced"

        login_as_agent(client, agent.id)
        html = client.get(f"/agent-dashboard/{agent.id}").get_data(as_text=True)
        assert 'Timeline' in html
        assert 'Immediately / within a month' in html
    finally:
        app_module.db.session.delete(lead)
        app_module.db.session.delete(agent)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_a_lead_with_no_timeline_reads_as_not_given_not_as_blank(client):
    agency = make_agency()
    lead = app_module.Lead(agency_id=agency.id, name="Ana",
                            email=f"{uuid.uuid4().hex[:8]}@example.test")
    app_module.db.session.add(lead)
    app_module.db.session.commit()
    try:
        login_as_owner(client, agency.id)
        html = client.get(f"/admin?agency_id={agency.id}").get_data(as_text=True)
        assert 'Not given' in html
    finally:
        app_module.db.session.delete(lead)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()
