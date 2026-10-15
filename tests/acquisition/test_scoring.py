"""Phase 4: how good a prospect is, and how sure we are about it.

A score nobody can argue with is a score nobody should trust. The rules
these tests hold are the ones that make the number worth reading:

  - fit, reachability and confidence stay SEPARATE. A perfect-fit agency
    with no inbox is a 90 we cannot reach, not a 60 - and those two want
    different things from a person;
  - a fact a model only inferred earns less than one we read in the
    markup, so an enthusiastic model cannot talk a prospect's score up on
    its own say-so;
  - every point comes with a line saying where it came from, and the
    lines add up to the number;
  - scoring NEVER moves a prospect toward outreach. That is a button a
    person presses;
  - and it touches no network and no model, so it is free and can be
    re-run whenever the weights change.
"""
import json

import pytest

import app as app_module
from acquisition import models
from acquisition.jobs import handlers, runner
from acquisition.services import scoring

db = app_module.db


@pytest.fixture(autouse=True)
def _clean():
    for model in (models.Job, models.Cost, models.Audit, models.Score,
                  models.ScoreModel, models.Fact, models.Contact,
                  models.Prospect, models.Market, models.Setting):
        model.query.delete()
    db.session.commit()
    yield
    db.session.rollback()


class FakeFact:
    """The scorer takes rows; it does not care where they came from."""

    def __init__(self, field, value, confidence='verified',
                 source_url='https://agency.test/about'):
        self.field = field
        self.value = value
        self.confidence = confidence
        self.source_url = source_url


class FakeContact:
    def __init__(self, email, is_generic=True, email_check='ok'):
        self.email = email
        self.is_generic = is_generic
        self.email_check = email_check


def facts(**pairs):
    return [FakeFact(field, value) for field, value in pairs.items()]


# ── the shape of the answer ──

def test_three_numbers_come_out_not_one():
    """Collapsing fit, reachability and confidence into a single score
    hides the thing a person most needs to know."""
    result = scoring.score_prospect(facts(chat_widget='Intercom'), [])
    for key in ('fit', 'band', 'reachability', 'confidence', 'breakdown'):
        assert key in result


def test_the_lines_add_up_to_the_number():
    result = scoring.score_prospect(
        facts(chat_widget='Crisp', luxury_positioning='luxury villas',
              booking_link='https://agency.test/book'),
        [FakeContact('info@agency.test')])
    earned = sum(line['points'] for line in result['breakdown'])
    possible = sum(scoring.DEFAULT_WEIGHTS.values())
    assert result['fit'] == int(round(100 * earned / possible))


def test_the_weights_add_up_to_a_hundred():
    """Not required by the arithmetic, but it means a weight reads as
    'how many points out of a hundred', which is how somebody editing
    them in Settings will think about it."""
    assert sum(scoring.DEFAULT_WEIGHTS.values()) == 100


def test_nothing_researched_scores_nothing():
    result = scoring.score_prospect([], [])
    assert result['fit'] == 0
    assert result['band'] == 'weak'
    assert result['confidence'] == 'none'


# ── the decision he made: a widget is an opportunity ──

def test_an_agency_already_running_a_widget_scores_higher():
    """Moaz's call, 15 Oct 2026: they have already decided chat is worth
    paying for, which is the hardest part of the sale."""
    with_widget = scoring.score_prospect(facts(chat_widget='Tawk.to'), [])
    without = scoring.score_prospect(facts(chat_widget=''), [])
    assert with_widget['fit'] > without['fit']


def test_the_reasoning_names_the_widget_so_a_draft_can_use_it():
    result = scoring.score_prospect(facts(chat_widget='Intercom'), [])
    line = [l for l in result['breakdown'] if l['signal'] == 'chat_widget'][0]
    assert 'Intercom' in line['why']
    assert 'worth paying for' in line['why']


def test_an_unrecognised_widget_still_counts():
    """The points are for having one, not for which one. Guessing which
    products are paid would be guessing."""
    known = scoring.score_prospect(facts(chat_widget='Crisp'), [])
    other = scoring.score_prospect(facts(chat_widget='SomeChatThing'), [])
    assert known['fit'] == other['fit']


# ── the decision he made: luxury ranks, it does not filter ──

def test_luxury_positioning_lifts_but_does_not_decide():
    luxury = scoring.score_prospect(facts(luxury_positioning='luxury and prime property'), [])
    plain = scoring.score_prospect(facts(luxury_positioning='family homes'), [])
    assert luxury['fit'] > plain['fit']
    assert plain['fit'] > 0, 'a mid-market agency must still be workable'


def test_a_mid_market_agency_can_still_reach_the_top_band():
    """Ranked, not filtered. An agency that is a good fit on everything
    else is not buried because it does not say 'luxury'."""
    result = scoring.score_prospect(
        facts(chat_widget='Crisp', enquiry_routes='form, email, phone',
              languages='en, fr, es', offices='London, Paris, Nice',
              booking_link='https://a.test/book', whatsapp='+33...',
              property_types='flats, houses, land, commercial',
              price_band='£400k-£900k', luxury_positioning='family homes'),
        [FakeContact('info@agency.test')])
    assert result['band'] == 'strong', result['fit']


def test_a_price_band_in_the_millions_counts_as_the_top_of_the_market():
    millions = scoring.score_prospect(facts(price_band='£2 million and up'), [])
    modest = scoring.score_prospect(facts(price_band='£200k to £400k'), [])
    assert millions['fit'] > modest['fit']
    assert modest['fit'] > 0, 'publishing prices at all is worth something'


# ── the safeguard: an inference is worth less than a sighting ──

def test_an_inferred_fact_earns_less_than_a_verified_one():
    seen = [FakeFact('luxury_positioning', 'prime central London', 'verified')]
    guessed = [FakeFact('luxury_positioning', 'prime central London', 'inferred')]
    assert scoring.score_prospect(seen, [])['fit'] > \
        scoring.score_prospect(guessed, [])['fit']


def test_an_estimate_earns_less_again():
    guessed = [FakeFact('price_band', '£3 million+', 'inferred')]
    estimated = [FakeFact('price_band', '£3 million+', 'estimated')]
    assert scoring.score_prospect(guessed, [])['fit'] > \
        scoring.score_prospect(estimated, [])['fit']


def test_a_model_cannot_talk_a_prospect_into_the_top_band_alone():
    """Every signal inferred, nothing seen. The number has to come back
    saying it is built on sand."""
    everything = [FakeFact(field, 'luxury, yes, several', 'inferred')
                  for field in scoring.DEFAULT_WEIGHTS]
    result = scoring.score_prospect(everything, [])
    assert result['confidence'] == 'low'
    assert result['fit'] < 65, 'an all-inferred prospect reached the top band'


def test_confidence_says_how_much_was_actually_seen():
    seen = [FakeFact(f, 'yes', 'verified') for f in
            ('chat_widget', 'enquiry_routes', 'languages')]
    seen.append(FakeFact('pages_read', 'https://a.test/, https://a.test/about'))
    assert scoring.score_prospect(seen, [])['confidence'] == 'high'


# ── reachability is its own axis ──

def test_a_company_inbox_is_reachable():
    result = scoring.score_prospect(facts(chat_widget='Crisp'),
                                    [FakeContact('info@agency.test')])
    assert result['reachability'] == 'inbox'
    assert 'info@agency.test' in result['reachability_why']


def test_a_form_with_no_inbox_is_not_the_same_as_nothing():
    result = scoring.score_prospect(
        facts(enquiry_routes='contact form'), [])
    assert result['reachability'] == 'form_only'


def test_an_address_that_failed_its_check_is_not_reachable():
    result = scoring.score_prospect(
        facts(chat_widget='Crisp'),
        [FakeContact('info@agency.test', email_check='no_mx')])
    assert result['reachability'] == 'bad_address'


def test_nothing_at_all_says_so():
    assert scoring.score_prospect(facts(chat_widget='Crisp'), [])['reachability'] == 'none'


def test_being_unreachable_does_not_lower_the_fit():
    """The whole point of keeping them apart. A 90 we cannot write to is
    still a 90 - it just needs a different kind of attention."""
    with_inbox = scoring.score_prospect(facts(chat_widget='Crisp'),
                                        [FakeContact('info@a.test')])
    without = scoring.score_prospect(facts(chat_widget='Crisp'), [])
    assert with_inbox['fit'] == without['fit']
    assert with_inbox['reachability'] != without['reachability']


# ── the bands ──

@pytest.mark.parametrize('fit,expected', [
    (100, 'strong'), (65, 'strong'), (64, 'worth_a_look'),
    (40, 'worth_a_look'), (39, 'weak'), (0, 'weak'),
])
def test_the_bands_fall_where_the_thresholds_say(fit, expected):
    assert scoring.band_for(fit) == expected


def test_the_thresholds_can_be_moved_without_touching_the_code():
    assert scoring.band_for(50, {'strong': 45}) == 'strong'


# ── every point is attributable ──

def test_every_signal_gets_a_line_even_when_it_found_nothing():
    """A blank is information: it tells you what to go and look for."""
    result = scoring.score_prospect(facts(chat_widget='Crisp'), [])
    signals = {line['signal'] for line in result['breakdown']}
    assert signals == set(scoring.DEFAULT_WEIGHTS)
    missing = [l for l in result['breakdown'] if l['signal'] == 'languages'][0]
    assert missing['points'] == 0
    assert missing['why'] == 'nothing found'


def test_a_line_carries_the_page_its_fact_came_from():
    result = scoring.score_prospect(
        [FakeFact('chat_widget', 'Crisp', source_url='https://a.test/contact')], [])
    line = [l for l in result['breakdown'] if l['signal'] == 'chat_widget'][0]
    assert line['source'] == 'https://a.test/contact'


def test_the_breakdown_survives_a_round_trip_through_the_database():
    result = scoring.score_prospect(facts(chat_widget='Crisp'), [])
    back = json.loads(scoring.breakdown_json(result))
    assert back['band'] == result['band']
    assert len(back['lines']) == len(result['breakdown'])


# ── the job, against the real database ──

def test_scoring_writes_a_score_and_leaves_the_stage_alone(client):
    """The decision he made: the engine scores, a person promotes."""
    prospect = models.Prospect(canonical_domain='scoreme.test',
                               website='https://scoreme.test',
                               stage='researched')
    db.session.add(prospect)
    db.session.commit()
    db.session.add(models.Fact(
        prospect_id=prospect.id, field='chat_widget', value='Crisp',
        confidence='verified', source_url='https://scoreme.test/',
        extractor='rules'))
    db.session.commit()

    job = runner.enqueue('score', prospect_id=prospect.id,
                         idempotency_key='score-test-1')
    runner.run_for(seconds=5)
    db.session.refresh(prospect)

    assert prospect.fit_score is not None and prospect.fit_score > 0
    assert prospect.stage == 'researched', 'scoring moved the prospect by itself'
    assert models.Score.query.filter_by(prospect_id=prospect.id).count() == 1


def test_scoring_an_unresearched_prospect_says_so_rather_than_scoring_zero(client):
    prospect = models.Prospect(canonical_domain='unread.test',
                               website='https://unread.test', stage='new')
    db.session.add(prospect)
    db.session.commit()
    runner.enqueue('score', prospect_id=prospect.id, idempotency_key='score-test-2')
    runner.run_for(seconds=5)
    db.session.refresh(prospect)
    assert prospect.fit_score is None


def test_the_weights_are_versioned_so_an_old_score_still_means_something(client):
    """Editing the weights must not silently rewrite what last week's 80
    was based on."""
    first = handlers.active_score_model()
    assert first.version == 1 and first.active
    again = handlers.active_score_model()
    assert again.id == first.id, 'a second call made a second model'


def test_a_reachable_prospect_sorts_above_an_equal_one_we_cannot_reach(client):
    """Priority, not fit, is what 'best fit first' sorts on - so the top
    of the list is the one to work on this morning."""
    rows = []
    for domain, email in (('reach.test', 'info@reach.test'), ('noreach.test', None)):
        prospect = models.Prospect(canonical_domain=domain,
                                   website=f'https://{domain}', stage='researched')
        db.session.add(prospect)
        db.session.commit()
        db.session.add(models.Fact(
            prospect_id=prospect.id, field='chat_widget', value='Crisp',
            confidence='verified', source_url=f'https://{domain}/',
            extractor='rules'))
        if email:
            db.session.add(models.Contact(
                prospect_id=prospect.id, email=email, is_generic=True,
                email_check='ok'))
        db.session.commit()
        runner.enqueue('score', prospect_id=prospect.id,
                       idempotency_key=f'score-{domain}')
        rows.append(prospect)
    runner.run_for(seconds=8)
    for row in rows:
        db.session.refresh(row)
    assert rows[0].fit_score == rows[1].fit_score
    assert rows[0].priority > rows[1].priority


def test_scoring_costs_nothing_and_asks_nobody(monkeypatch):
    """No network, no model. If that ever changes, the cost page and the
    budget have to learn about it first."""
    import inspect
    source = inspect.getsource(scoring)
    for forbidden in ('httpx', 'requests', 'openai', 'fetch', 'urlopen'):
        assert forbidden not in source, \
            f"scoring.py reaches for {forbidden} - it is meant to be offline"


def test_changing_the_weights_makes_a_new_version_not_a_rewrite(client):
    """An old score has to keep meaning what it meant. If editing the
    weights rewrote the active row, last week's 80 would be a number
    nobody could account for."""
    from acquisition import settings
    first = handlers.active_score_model()
    settings.set('score_weights_json', json.dumps({'chat_widget': 40}))
    second = handlers.active_score_model()
    assert second.version == first.version + 1
    assert second.id != first.id
    assert json.loads(second.weights)['chat_widget'] == 40
    db.session.refresh(first)
    assert first.active is False, 'the old version is still marked active'
    assert json.loads(first.weights)['chat_widget'] == \
        scoring.DEFAULT_WEIGHTS['chat_widget'], 'the old weights were rewritten'


def test_rescoring_with_unchanged_weights_does_not_pile_up_versions(client):
    first = handlers.active_score_model()
    assert handlers.active_score_model().id == first.id
    assert handlers.active_score_model().id == first.id


def test_nonsense_in_the_weights_setting_falls_back_rather_than_scoring_zero(client):
    from acquisition import settings
    settings.set('score_weights_json', 'not json at all')
    weights, _ = handlers.wanted_weights()
    assert weights == scoring.DEFAULT_WEIGHTS


def test_an_unknown_weight_key_is_ignored(client):
    """A typo in Settings must not invent a signal the scorer does not
    know how to read."""
    from acquisition import settings
    settings.set('score_weights_json',
                       json.dumps({'chat_widget': 30, 'vibes': 99}))
    weights, _ = handlers.wanted_weights()
    assert weights['chat_widget'] == 30
    assert 'vibes' not in weights


# ── the bug Moaz found by opening a real website ──
#
# Research records `chat_widget` as the literal string 'none seen' when a
# site has no chat. The scorer had no case for it, so it fell through to
# the default and awarded the full 22 points - the largest signal in the
# model - for having no chat at all, with the reasoning line reading
# "already runs a chat widget (none seen)". The damage was not the
# inflated number but that the signal was CONSTANT across every
# prospect, so the biggest weight contributed nothing to the ranking.
#
# The test meant to cover this compared 'Tawk.to' against an EMPTY
# string, which research never writes. A test using a value the system
# cannot produce proves nothing. These use the real ones.

SENTINELS = ('none seen', 'none', 'None', '')


@pytest.mark.parametrize('sentinel', SENTINELS)
def test_no_chat_earns_no_chat_points(sentinel):
    result = scoring.score_prospect(
        [FakeFact('chat_widget', sentinel), FakeFact('chat_kind', 'none')], [])
    line = [l for l in result['breakdown'] if l['signal'] == 'chat_widget'][0]
    assert line['points'] == 0, \
        f"an agency with no chat earned {line['points']} points for it"


@pytest.mark.parametrize('sentinel', SENTINELS)
def test_no_signal_anywhere_is_ever_read_as_a_yes(sentinel):
    """The same mistake could be made in any branch. Sweep all of them -
    which is how the identical hole in enquiry_routes was found."""
    for field in scoring.DEFAULT_WEIGHTS:
        result = scoring.score_prospect([FakeFact(field, sentinel)], [])
        line = [l for l in result['breakdown'] if l['signal'] == field][0]
        assert line['points'] == 0, \
            f"{field}={sentinel!r} earned {line['points']} points"


def test_a_blank_says_what_is_missing_rather_than_shrugging():
    result = scoring.score_prospect([FakeFact('price_band', 'none seen')], [])
    line = [l for l in result['breakdown'] if l['signal'] == 'price_band'][0]
    assert line['why'] == 'no prices published'


def test_the_reasoning_never_claims_something_the_fact_denies():
    """'already runs a chat widget (none seen)' is how this was spotted -
    a line a person could read and know was wrong."""
    result = scoring.score_prospect(
        [FakeFact('chat_widget', 'none seen'), FakeFact('chat_kind', 'none')], [])
    line = [l for l in result['breakdown'] if l['signal'] == 'chat_widget'][0]
    assert 'none seen' not in line['why']
    assert 'already runs' not in line['why']


def test_an_agency_with_no_chat_ranks_below_one_with_a_whatsapp_button():
    """The ordering that was wrong: these two scored the same 22."""
    nothing = scoring.score_prospect(
        [FakeFact('chat_widget', 'none seen'), FakeFact('chat_kind', 'none')], [])
    button = scoring.score_prospect(
        [FakeFact('chat_widget', 'WhatsApp click-to-chat'),
         FakeFact('chat_kind', 'click_to_chat')], [])
    hosted = scoring.score_prospect(
        [FakeFact('chat_widget', 'Intercom'), FakeFact('chat_kind', 'hosted')], [])
    assert nothing['fit'] < button['fit'] < hosted['fit']


# ── click-to-chat: the WhatsApp button on thehofmangroup.com ──

def test_a_whatsapp_button_counts_as_chat():
    """It looks identical to a visitor - a round button in the corner -
    and the engine was calling it 'no chat'."""
    result = scoring.score_prospect(
        [FakeFact('chat_widget', 'WhatsApp click-to-chat'),
         FakeFact('chat_kind', 'click_to_chat')], [])
    line = [l for l in result['breakdown'] if l['signal'] == 'chat_widget'][0]
    assert line['points'] > 0


def test_a_whatsapp_button_is_worth_less_than_paid_software():
    """They want to chat, but nobody is paying for software and nothing
    answers overnight. Less proof of budget, and a different pitch."""
    button = scoring.score_prospect(
        [FakeFact('chat_widget', 'WhatsApp click-to-chat'),
         FakeFact('chat_kind', 'click_to_chat')], [])
    hosted = scoring.score_prospect(
        [FakeFact('chat_widget', 'Intercom'), FakeFact('chat_kind', 'hosted')], [])
    assert button['fit'] < hosted['fit']


def test_the_reasoning_gives_phase_five_a_different_thing_to_say():
    """The whole reason for splitting the two kinds: the opening line of
    an email to each of these agencies is not the same sentence."""
    button = scoring.score_prospect(
        [FakeFact('chat_widget', 'WhatsApp click-to-chat'),
         FakeFact('chat_kind', 'click_to_chat')], [])
    why = [l for l in button['breakdown'] if l['signal'] == 'chat_widget'][0]['why']
    assert 'awake' in why, 'the line says nothing a draft could use'
    assert 'worth paying for' not in why, 'that is the hosted-widget line'


def test_a_hosted_widget_wins_when_a_site_has_both():
    """An agency running Intercom and also showing a WhatsApp link is an
    Intercom customer, and should be written to as one."""
    from acquisition.services import research as reading
    markup = ('<script src="https://widget.intercom.io/widget/abc"></script>'
              '<a href="https://wa.me/15551234">Chat on WhatsApp</a>')
    assert reading.chat_on(markup) == ('Intercom', 'hosted')


@pytest.mark.parametrize('markup,expected', [
    ('<a href="https://wa.me/17868633151?text=Hi">WhatsApp</a>',
     ('WhatsApp click-to-chat', 'click_to_chat')),
    ('<a href="https://api.whatsapp.com/send?phone=1786">chat</a>',
     ('WhatsApp click-to-chat', 'click_to_chat')),
    ('<a href="https://m.me/someagency">Messenger</a>',
     ('Facebook Messenger click-to-chat', 'click_to_chat')),
    ('<p>no chat here</p>', ('', 'none')),
])
def test_click_to_chat_is_recognised_in_its_usual_shapes(markup, expected):
    from acquisition.services import research as reading
    assert reading.chat_on(markup) == expected


def test_research_records_the_kind_so_scoring_need_not_read_prose(client):
    """chat_kind is its own fact. Parsing 'WhatsApp click-to-chat' out of
    a sentence is the sort of thing that breaks when wording changes."""
    from acquisition.jobs.handlers import FACT_LABELS
    assert 'chat_kind' in FACT_LABELS
