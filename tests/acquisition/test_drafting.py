"""Phase 5: writing the email, for Moaz to send himself.

Nothing here sends. The engine writes a subject and a body, records
which facts it used, and waits on the Drafts page until he copies it
into his own email.

This text goes to a stranger over his own name, from his own address, so
the safeguards matter more than the prose:

  - only `verified` facts may be quoted. A model's inference about
    someone's business is not something to tell them about themselves;
  - ONE observation. The research found four or five true things; an
    email that recites all of them unsettles people;
  - the AI polish may REMOVE awkwardness and may not ADD anything. If it
    comes back with a number that was not in the draft, the polish is
    thrown away and the hand-filled version is kept - a plain email a
    person wrote beats a nicer one nobody can vouch for;
  - no percentages, no "3x", no guarantees. The same rule as the home
    page, for the same reason;
  - the opt-out line survives everything;
  - and a prospect compliance says we may not contact is not drafted
    for at all.
"""
import json

import pytest

import app as app_module
from acquisition import models, settings
from acquisition.jobs import handlers, runner
from acquisition.services import drafting

db = app_module.db


@pytest.fixture(autouse=True)
def _clean():
    for model in (models.Message, models.Template, models.Job, models.Cost,
                  models.Audit, models.Fact, models.Contact, models.Prospect,
                  models.Market, models.Setting):
        model.query.delete()
    db.session.commit()
    yield
    db.session.rollback()


@pytest.fixture()
def admin_client(client):
    with client.session_transaction() as sess:
        sess['super_admin'] = True
        sess['acq_csrf'] = 'test-csrf-token'
    return client


class FakeFact:
    _next = 1

    def __init__(self, field, value, confidence='verified'):
        self.field = field
        self.value = value
        self.confidence = confidence
        self.source_url = 'https://agency.test/'
        self.id = FakeFact._next
        FakeFact._next += 1


class FakeProspect:
    def __init__(self, name='The Hofman Group Ltd', domain='hofman.test'):
        self.name = name
        self.canonical_domain = domain


def verified_market():
    market = models.Market(city='Test', country='TC',
                           legal_status='verified', outreach_method='email',
                           status='active')
    db.session.add(market)
    db.session.commit()
    return market


def prospect_with(facts, stage='qualified', email='info@agency.test'):
    market = verified_market()
    prospect = models.Prospect(canonical_domain='agency.test',
                               website='https://agency.test',
                               name='Agency Test', stage=stage,
                               market_id=market.id)
    db.session.add(prospect)
    db.session.commit()
    for field, value, confidence in facts:
        db.session.add(models.Fact(
            prospect_id=prospect.id, field=field, value=value,
            confidence=confidence, source_url='https://agency.test/',
            extractor='rules'))
    if email:
        db.session.add(models.Contact(prospect_id=prospect.id, email=email,
                                      is_generic=True, email_check='ok'))
    db.session.commit()
    return prospect


# ── only what we saw ──

def test_an_inferred_fact_is_never_quoted_back_at_them():
    """A model concluding an agency is 'luxury' is not grounds for
    telling them so in a cold email."""
    facts = [FakeFact('luxury_positioning', 'prime villas', 'inferred'),
             FakeFact('price_band', '2 million+', 'inferred')]
    observation, used = drafting.pick_observation(facts, 'agency.test')
    assert observation == ''
    assert used == []


def test_a_verified_fact_is_quoted():
    facts = [FakeFact('chat_kind', 'hosted'),
             FakeFact('chat_widget', 'Intercom')]
    observation, used = drafting.pick_observation(facts, 'agency.test')
    assert 'Intercom' in observation
    assert len(used) == 2, 'the fact ids behind the sentence were not recorded'


def test_only_one_observation_is_used():
    """The research found five things. The email uses one."""
    facts = [FakeFact('chat_kind', 'hosted'), FakeFact('chat_widget', 'Crisp'),
             FakeFact('booking_link', 'https://agency.test/book'),
             FakeFact('languages', 'en, fr, es'),
             FakeFact('enquiry_routes', 'form, email, phone')]
    observation, _ = drafting.pick_observation(facts, 'agency.test')
    assert observation.count('.') <= 2, 'the opening recites a dossier'
    assert 'Crisp' in observation
    assert 'book' not in observation.lower()


def test_a_whatsapp_button_gets_its_own_opening():
    """The one that is nearly the whole pitch: they want to chat, but
    somebody has to be awake."""
    facts = [FakeFact('chat_kind', 'click_to_chat'),
             FakeFact('chat_widget', 'WhatsApp click-to-chat')]
    observation, _ = drafting.pick_observation(facts, 'hofman.test')
    assert 'WhatsApp' in observation
    assert 'night' in observation


def test_a_form_only_agency_gets_the_form_opening():
    facts = [FakeFact('enquiry_routes', 'contact form')]
    observation, _ = drafting.pick_observation(facts, 'agency.test')
    assert 'form' in observation.lower()


def test_the_domain_appears_so_it_reads_as_a_real_look():
    facts = [FakeFact('enquiry_routes', 'contact form')]
    observation, _ = drafting.pick_observation(facts, 'hofman.test')
    assert 'hofman.test' in observation


# ── the claims rules, same as the home page ──

@pytest.mark.parametrize('text,expected', [
    ('we capture 87% of visitors', 'a percentage'),
    ('3x more qualified leads', 'a multiplier like "3x"'),
    ('used by hundreds of agencies', 'a vague large number'),
    ('guaranteed to double your leads', 'a promise'),
    ('most agencies see results', 'a claim about everyone'),
])
def test_a_claim_nobody_can_stand_behind_is_caught(text, expected):
    assert expected in drafting.unsupported_claims(text)


def test_an_honest_email_passes():
    body = drafting.DEFAULT_BODY.format(
        owner_greeting='', observation='I had a look at agency.test.',
        sign_off='Moaz', opt_out=drafting.OPT_OUT_LINE,
        agency='x', agency_short='x', domain='agency.test')
    assert drafting.unsupported_claims(body) == []
    assert drafting.check('A subject', body) == []


def test_the_opt_out_line_is_not_optional():
    problems = drafting.check('subject', 'A body with no way out.')
    assert any('opt-out' in p for p in problems)


def test_an_unfilled_slot_is_caught():
    problems = drafting.check('subject',
                              '{observation}\n' + drafting.OPT_OUT_LINE)
    assert any('slot' in p for p in problems)


# ── the polish may remove, never add ──

def test_a_polish_that_invents_a_number_is_detected():
    original = 'We answer visitors around the clock.'
    polished = 'We answer 94% of visitors around the clock.'
    assert drafting.new_facts_in(polished, original) == ['94']


def test_a_polish_that_only_rewords_is_fine():
    original = 'We answer visitors around the clock.'
    polished = "We're there for visitors at any hour."
    assert drafting.new_facts_in(polished, original) == []


def test_a_number_already_in_the_draft_is_not_an_invention():
    original = 'It takes about 15 minutes to install.'
    polished = 'Install takes 15 minutes.'
    assert drafting.new_facts_in(polished, original) == []


# ── the name in the subject line ──

@pytest.mark.parametrize('given,expected', [
    ('The Hofman Group Ltd', 'The Hofman Group'),
    ('Prime Estates Limited', 'Prime Estates'),
    ('Marbella Homes S.L.', 'Marbella Homes'),
    ('Just A Name', 'Just A Name'),
])
def test_a_legal_suffix_is_dropped_from_the_subject(given, expected):
    assert drafting.short_name(given, 'x.test') == expected


def test_a_missing_name_falls_back_to_the_domain():
    assert drafting.short_name('', 'hofman.test') == 'Hofman'


# ── the job, against the database ──

def test_drafting_writes_a_message_and_sends_nothing(client):
    settings.set('draft_ai', 'off')
    prospect = prospect_with([('chat_kind', 'hosted', 'verified'),
                              ('chat_widget', 'Crisp', 'verified')])
    runner.enqueue('draft', prospect_id=prospect.id, idempotency_key='d1')
    runner.run_for(seconds=5)

    message = models.Message.query.filter_by(prospect_id=prospect.id).first()
    assert message is not None
    assert message.status == 'draft', 'a draft was marked as something else'
    assert message.sent_at is None, 'something set a sent time'
    assert message.provider == 'manual'
    assert 'Crisp' in message.body
    db.session.refresh(prospect)
    assert prospect.stage == 'qualified', 'drafting moved the prospect'


def test_the_facts_behind_the_email_are_recorded(client):
    settings.set('draft_ai', 'off')
    prospect = prospect_with([('chat_kind', 'hosted', 'verified'),
                              ('chat_widget', 'Intercom', 'verified')])
    runner.enqueue('draft', prospect_id=prospect.id, idempotency_key='d2')
    runner.run_for(seconds=5)
    message = models.Message.query.filter_by(prospect_id=prospect.id).first()
    used = json.loads(message.facts_used)
    assert used, 'the draft does not say which facts it used'
    for fact_id in used:
        fact = db.session.get(models.Fact, fact_id)
        assert fact is not None and fact.confidence == 'verified'


def test_an_agency_with_nothing_verified_is_not_drafted_for(client):
    """Better to say there is no honest opening than to invent one."""
    settings.set('draft_ai', 'off')
    prospect = prospect_with([('luxury_positioning', 'prime', 'inferred')])
    runner.enqueue('draft', prospect_id=prospect.id, idempotency_key='d3')
    runner.run_for(seconds=5)
    assert models.Message.query.filter_by(prospect_id=prospect.id).count() == 0
    db.session.refresh(prospect)
    assert prospect.needs_human is True


def test_a_prospect_we_may_not_contact_is_not_drafted_for(client):
    settings.set('draft_ai', 'off')
    prospect = prospect_with([('chat_kind', 'hosted', 'verified'),
                              ('chat_widget', 'Crisp', 'verified')])
    prospect.do_not_contact = True
    prospect.do_not_contact_reason = 'they asked us not to'
    db.session.commit()
    runner.enqueue('draft', prospect_id=prospect.id, idempotency_key='d4')
    runner.run_for(seconds=5)
    assert models.Message.query.filter_by(prospect_id=prospect.id).count() == 0


def test_an_unverified_market_is_not_drafted_for(client):
    """The gate that has stood in front of outreach since Phase 1."""
    settings.set('draft_ai', 'off')
    prospect = prospect_with([('chat_kind', 'hosted', 'verified'),
                              ('chat_widget', 'Crisp', 'verified')])
    prospect.market.legal_status = 'needs_verification'
    db.session.commit()
    runner.enqueue('draft', prospect_id=prospect.id, idempotency_key='d5')
    runner.run_for(seconds=5)
    assert models.Message.query.filter_by(prospect_id=prospect.id).count() == 0


def test_drafting_twice_replaces_rather_than_piling_up(client):
    settings.set('draft_ai', 'off')
    prospect = prospect_with([('chat_kind', 'hosted', 'verified'),
                              ('chat_widget', 'Crisp', 'verified')])
    for n in (1, 2):
        runner.enqueue('draft', prospect_id=prospect.id,
                       idempotency_key=f'd6-{n}')
        runner.run_for(seconds=5)
    assert models.Message.query.filter_by(prospect_id=prospect.id,
                                          status='draft').count() == 1


def test_the_template_is_versioned(client):
    first = handlers.active_template()
    assert first.version == 1 and first.active
    assert handlers.active_template().id == first.id


# ── the page he will actually live in ──

def test_the_drafts_page_shows_what_is_waiting(admin_client):
    settings.set('draft_ai', 'off')
    prospect = prospect_with([('chat_kind', 'hosted', 'verified'),
                              ('chat_widget', 'Crisp', 'verified')])
    runner.enqueue('draft', prospect_id=prospect.id, idempotency_key='d7')
    runner.run_for(seconds=5)
    body = admin_client.get('/owner/acquisition/drafts').get_data(as_text=True)
    assert 'Crisp' in body
    assert 'info@agency.test' in body, 'the address to send to is not shown'
    assert 'I sent this' in body


def test_marking_one_sent_moves_the_prospect_on(admin_client):
    settings.set('draft_ai', 'off')
    prospect = prospect_with([('chat_kind', 'hosted', 'verified'),
                              ('chat_widget', 'Crisp', 'verified')])
    runner.enqueue('draft', prospect_id=prospect.id, idempotency_key='d8')
    runner.run_for(seconds=5)
    message = models.Message.query.filter_by(prospect_id=prospect.id).first()
    admin_client.post(f'/owner/acquisition/drafts/{message.id}/sent',
                      data={'csrf_token': 'test-csrf-token'}, follow_redirects=True)
    db.session.refresh(message)
    db.session.refresh(prospect)
    assert message.status == 'sent'
    assert message.sent_at is not None
    assert prospect.stage == 'contacted', \
        'a sent email did not move the prospect, so it could be sent twice'


def test_drafting_costs_nothing_when_the_polish_is_off(client):
    settings.set('draft_ai', 'off')
    before = models.Cost.query.count()
    prospect = prospect_with([('chat_kind', 'hosted', 'verified'),
                              ('chat_widget', 'Crisp', 'verified')])
    runner.enqueue('draft', prospect_id=prospect.id, idempotency_key='d9')
    runner.run_for(seconds=5)
    assert models.Cost.query.count() == before


def test_nothing_in_drafting_sends_an_email():
    """The one line that must stay true until there is a mailbox."""
    import inspect
    from acquisition.services import drafting as module
    source = inspect.getsource(module)
    for forbidden in ('smtp', 'sendgrid', 'brevo', 'send_email', 'requests.post'):
        assert forbidden not in source.lower(), \
            f"drafting.py reaches for {forbidden}"


def test_no_paragraph_is_hard_wrapped():
    """Hard breaks at 70 characters look tidy in an editor and look
    broken in Gmail, which re-wraps to the reader's window and leaves
    the old breaks mid-sentence. This is pasted into a real email
    client, so every paragraph is one line."""
    for paragraph in drafting.DEFAULT_BODY.split('\n\n'):
        if paragraph.strip().startswith('{sign_off'):
            continue          # a sign-off is genuinely several lines
        lines = [line for line in paragraph.split('\n') if line.strip()]
        assert len(lines) <= 1, \
            f"this paragraph is hard-wrapped and will break in Gmail: {lines[0][:50]}"
