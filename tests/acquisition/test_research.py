"""Phase 3: reading a prospect's website, and writing down only what it says.

The engine is about to start telling a stranger what it knows about their
business. Everything in this file exists to keep that honest:

  - a fact must carry the page it came from, and say how it was learned -
    `verified` means we saw it in the markup, `inferred` means a model read
    the text and concluded it;
  - only the agency's own domain counts as evidence about the agency (a
    footer usually carries the web designer's address too);
  - only company inboxes are collected. jean.dupont@agency.fr is personal
    data with a deletion clock and a duty of care; info@agency.fr is a
    business contact. The engine does not take the first kind at all;
  - the model sees text that was already fetched, gets no tools, and is
    asked for a fixed set of keys, so a hostile page can produce a bad
    answer but not an instruction;
  - researching twice updates the facts rather than piling up duplicates;
  - the whole job finishes inside gunicorn's 30 seconds, and a site that
    hangs costs one job, not the batch.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import app as app_module
from acquisition import models, settings
from acquisition.jobs import handlers, runner
from acquisition.services import ai, fetch
from acquisition.services import research as reading

db = app_module.db


@pytest.fixture(autouse=True)
def _clean():
    for model in (models.Job, models.Cost, models.Audit, models.Fact,
                  models.Contact, models.Prospect, models.Market,
                  models.Setting):
        model.query.delete()
    db.session.commit()
    fetch.reset_caches()
    yield
    db.session.rollback()


@pytest.fixture()
def admin(client):
    with client.session_transaction() as sess:
        sess['super_admin'] = True
        sess['acq_csrf'] = 'test-csrf-token'
    return client


# ─────────────────────────────────────────────────────
# A real little agency website
# ─────────────────────────────────────────────────────

HOME = """<!doctype html><html lang="fr">
<head><title>Riviera Estates</title>
<link rel="alternate" hreflang="en" href="/en/">
<link rel="alternate" hreflang="ru" href="/ru/">
<script src="https://client.crisp.chat/l.js"></script>
</head><body>
<nav><a href="/a-propos">Notre agence</a>
     <a href="/contact">Contactez-nous</a>
     <a href="/nos-biens">Nos biens</a>
     <a href="/mentions-legales">Mentions</a>
     <a href="https://www.facebook.com/riviera">Facebook</a></nav>
<p>Villas d'exception sur la Cote d'Azur depuis 1987.</p>
<a href="https://wa.me/33600000000">WhatsApp</a>
</body></html>"""

CONTACT = """<!doctype html><html lang="fr"><body>
<h1>Contact</h1>
<a href="mailto:info@riviera-estates.test">info@riviera-estates.test</a>
<a href="mailto:jean.dupont@riviera-estates.test">Jean Dupont</a>
<a href="mailto:studio@webdesigner.test">site by studio</a>
<a href="tel:+33 4 93 00 00 00">+33 4 93 00 00 00</a>
<a href="https://calendly.com/riviera/viewing">Book a viewing</a>
<form action="/send"><input name="email"><textarea name="message"></textarea></form>
</body></html>"""

ABOUT = """<!doctype html><html lang="fr"><body>
<h1>Notre agence</h1><p>Trois bureaux: Nice, Cannes, Monaco.</p>
</body></html>"""

LISTINGS = """<!doctype html><html lang="fr"><body>
<h1>Nos biens</h1><p>Villas et appartements de 2 a 15 millions d'euros.</p>
</body></html>"""


class Site(BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):                                    # noqa: N802
        status, body = self.routes.get(self.path, (404, b'<h1>no</h1>'))
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope='module')
def site():
    Site.routes = {
        '/': (200, HOME.encode()),
        '/robots.txt': (200, b"User-agent: *\nAllow: /\n"),
        '/contact': (200, CONTACT.encode()),
        '/a-propos': (200, ABOUT.encode()),
        '/nos-biens': (200, LISTINGS.encode()),
    }
    server = HTTPServer(('127.0.0.1', 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


@pytest.fixture()
def prospect(site, monkeypatch):
    """A prospect whose 'domain' is the little site above.

    canonical_domain refuses an address with a port, so the domain is a
    name and the fetcher is pointed at the loopback behind it - the same
    trick the Phase 2 tests use.
    """
    market = models.Market(country='France', city='Nice', language='fr',
                           legal_status='verified', outreach_method='email',
                           status='active')
    db.session.add(market)
    db.session.commit()
    row = models.Prospect(canonical_domain='riviera-estates.test',
                          name='Riviera Estates', market_id=market.id,
                          website='https://riviera-estates.test', stage='new')
    db.session.add(row)
    db.session.commit()

    port = site.server_port
    real_get = fetch.get

    def local_get(url, **kwargs):
        moved = url.replace('https://riviera-estates.test',
                            f'http://127.0.0.1:{port}')
        moved = moved.replace('http://riviera-estates.test',
                              f'http://127.0.0.1:{port}')
        kwargs['allow_private'] = True
        page = real_get(moved, **kwargs)
        # Hand back the public-looking address, so link-following and the
        # recorded source pages are the ones a real run would use.
        page.url = url
        page.final_url = url
        return page

    monkeypatch.setattr(fetch, 'get', local_get)
    # The real one-request-per-second politeness is tested for real in
    # test_the_crawl_is_polite_and_still_fits_the_budget below. Holding it
    # here too would add a minute and a half to every suite run and prove
    # the same thing twenty times.
    monkeypatch.setattr(fetch, 'PER_HOST_DELAY', 0.01)
    return row


# ─────────────────────────────────────────────────────
# What the markup gives up for free
# ─────────────────────────────────────────────────────

def test_the_pages_worth_opening_are_picked_in_order():
    links = reading.page_links(HOME, 'https://riviera-estates.test/',
                               'riviera-estates.test', limit=4)
    assert links == ['https://riviera-estates.test/contact',
                     'https://riviera-estates.test/a-propos',
                     'https://riviera-estates.test/nos-biens']


def test_somebody_elses_site_is_not_evidence_about_this_agency():
    links = reading.page_links(HOME, 'https://riviera-estates.test/',
                               'riviera-estates.test', limit=8)
    assert not any('facebook' in link for link in links)


def test_only_one_page_of_each_kind_is_opened():
    html = ('<a href="/contact">a</a><a href="/contact-us">b</a>'
            '<a href="/nous-contacter">c</a>')
    links = reading.page_links(html, 'https://x.test/', 'x.test', limit=4)
    assert len(links) == 1


def test_the_web_designers_address_is_not_the_agencys_address():
    found = reading.emails_in(CONTACT, 'riviera-estates.test')
    assert 'studio@webdesigner.test' not in found
    assert 'info@riviera-estates.test' in found


@pytest.mark.parametrize('address, generic', [
    ('info@a.test', True), ('contact@a.test', True), ('hello@a.test', True),
    ('bonjour@a.test', True), ('info.paris@a.test', True),
    ('sales-team@a.test', True),
    ('jean.dupont@a.test', False), ('j.dupont@a.test', False),
    ('marie@a.test', False),
])
def test_a_company_inbox_is_told_apart_from_a_person(address, generic):
    assert reading.is_generic(address) is generic


def test_only_numbers_the_site_marked_as_phone_numbers_count():
    """A number loose in the text is as likely to be a price."""
    html = '<a href="tel:+33 4 93 00 00 00">call</a><p>Prix: 2 500 000 EUR</p>'
    assert reading.phones_in(html) == ['+33 4 93 00 00 00']


def test_whatsapp_is_found_where_it_is_linked():
    assert reading.whatsapp_in(HOME) == ['https://wa.me/33600000000']


@pytest.mark.parametrize('markup, expected', [
    ('<script src="https://client.crisp.chat/l.js">', 'Crisp'),
    ('<script src="https://widget.intercom.io/widget/abc">', 'Intercom'),
    ('<script src="https://embed.tawk.to/123/default">', 'Tawk.to'),
    ('<div class="chat-bubble">Chat with us</div>', ''),
])
def test_a_chat_widget_is_recognised_by_its_own_script(markup, expected):
    """Not by a picture of a speech bubble - by the script it loads."""
    assert reading.widget_in(markup) == expected


def test_a_contact_form_and_a_booking_link_are_both_noticed():
    assert reading.has_contact_form(CONTACT) is True
    assert 'calendly.com' in reading.booking_link(
        CONTACT, 'https://riviera-estates.test/contact', 'riviera-estates.test')


def test_languages_come_from_the_markup_not_from_guessing():
    assert reading.languages_in(HOME) == ['fr', 'en', 'ru']


def test_a_widgets_script_cannot_be_quoted_back_as_the_agencys_prose():
    text = reading.visible_text(HOME)
    assert 'crisp.chat' not in text
    assert "Villas d'exception" in text


def test_the_routes_read_in_the_order_a_person_would_say_them():
    routes = reading.routes_from({'whatsapp': ['x'], 'phones': ['y'],
                                  'form': True, 'emails': ['a@b.test'],
                                  'booking': 'z'})
    assert routes == ['WhatsApp', 'phone', 'contact form', 'email',
                      'booking link']


# ─────────────────────────────────────────────────────
# The job, against a site that really answers
# ─────────────────────────────────────────────────────

def run_research(prospect):
    settings.set('research_ai', 'off')
    job = runner.enqueue('research', prospect_id=prospect.id)
    outcome = runner.run_next()
    return outcome, db.session.get(models.Job, job.id)


def facts_of(prospect):
    return {fact.field: fact for fact in
            models.Fact.query.filter_by(prospect_id=prospect.id).all()}


def test_reading_a_site_writes_down_what_it_says(prospect):
    outcome, _job = run_research(prospect)
    assert outcome['ok'] is True, outcome.get('error')

    found = facts_of(prospect)
    assert found['chat_widget'].value == 'Crisp'
    assert found['phone'].value == '+33 4 93 00 00 00'
    assert found['whatsapp'].value == 'https://wa.me/33600000000'
    assert 'calendly.com' in found['booking_link'].value
    assert found['languages'].value == 'fr, en, ru'
    assert 'WhatsApp' in found['enquiry_routes'].value
    assert 'contact form' in found['enquiry_routes'].value


def test_every_fact_can_show_the_page_it_came_from(prospect):
    run_research(prospect)
    for fact in models.Fact.query.filter_by(prospect_id=prospect.id).all():
        assert fact.source_url, f"{fact.field} has no source page"
        assert fact.source_url.startswith('http')
        assert fact.confidence in models.CONFIDENCE_LEVELS


def test_what_the_rules_saw_is_marked_verified_not_guessed(prospect):
    run_research(prospect)
    found = facts_of(prospect)
    assert found['chat_widget'].confidence == 'verified'
    assert found['chat_widget'].extractor == 'rules'


def test_the_company_inbox_is_kept_and_the_person_is_not(prospect):
    run_research(prospect)
    addresses = [c.email for c in
                 models.Contact.query.filter_by(prospect_id=prospect.id).all()]
    assert addresses == ['info@riviera-estates.test']
    assert 'jean.dupont@riviera-estates.test' not in addresses
    assert 'studio@webdesigner.test' not in addresses


def test_a_kept_inbox_has_a_deletion_clock_and_a_source(prospect):
    run_research(prospect)
    contact = models.Contact.query.filter_by(prospect_id=prospect.id).first()
    assert contact.is_generic is True
    assert contact.personal_data_expires_at is not None
    assert contact.source_url.endswith('/contact')


def test_it_reads_more_than_the_home_page(prospect):
    outcome, _job = run_research(prospect)
    assert outcome['result']['pages'] >= 3
    read = facts_of(prospect)['pages_read'].value
    assert '/contact' in read
    assert '/a-propos' in read


def test_the_prospect_moves_on_a_stage(prospect):
    run_research(prospect)
    assert db.session.get(models.Prospect, prospect.id).stage == 'researched'


def test_researching_twice_updates_rather_than_piles_up(prospect):
    run_research(prospect)
    first = models.Fact.query.filter_by(prospect_id=prospect.id).count()
    models.Job.query.delete()
    db.session.commit()
    run_research(prospect)
    assert models.Fact.query.filter_by(prospect_id=prospect.id).count() == first
    assert models.Contact.query.filter_by(prospect_id=prospect.id).count() == 1


def test_the_free_checks_cost_nothing(prospect):
    run_research(prospect)
    assert models.Cost.query.count() == 0


def test_nothing_runs_while_everything_is_stopped(prospect):
    settings.set('kill_switch_all', 'on')
    job = runner.enqueue('research', prospect_id=prospect.id)
    runner.run_next()
    assert db.session.get(models.Job, job.id).status == 'queued'


# ─────────────────────────────────────────────────────
# The paid half
# ─────────────────────────────────────────────────────

class FakeOpenAI:
    def __init__(self, content, prompt_tokens=1200, completion_tokens=120):
        self.content = content
        self.prompt_tokens, self.completion_tokens = prompt_tokens, completion_tokens
        self.calls = []
        self.chat = type('chat', (), {'completions': self})()

    def create(self, **kwargs):
        self.calls.append(kwargs)
        message = type('m', (), {'content': self.content})()
        choice = type('c', (), {'message': message})()
        usage = type('u', (), {'prompt_tokens': self.prompt_tokens,
                               'completion_tokens': self.completion_tokens})()
        return type('r', (), {'choices': [choice], 'usage': usage})()


AI_ANSWER = json.dumps({
    'luxury': 'yes',
    'price_band': '2m to 15m EUR',
    'property_types': ['villas', 'apartments'],
    'languages': ['fr', 'en'],
    'offices': 3,
    'one_line': 'Cote d Azur villas since 1987, three offices on the coast',
})


@pytest.fixture()
def with_ai():
    settings.set('research_ai', 'on')
    fake = FakeOpenAI(AI_ANSWER)
    ai.set_client(fake)
    yield fake
    ai.set_client(app_module.client)


def test_the_ai_pass_adds_the_judgement_calls(prospect, with_ai):
    runner.enqueue('research', prospect_id=prospect.id)
    outcome = runner.run_next()
    assert outcome['ok'] is True, outcome.get('error')

    found = facts_of(prospect)
    assert found['luxury_positioning'].value == 'yes'
    assert found['price_band'].value == '2m to 15m EUR'
    assert found['property_types'].value == 'villas, apartments'
    assert 'Cote d Azur' in found['what_they_say'].value
    assert found['offices'].value == '3'


def test_what_the_model_concluded_is_never_marked_verified(prospect, with_ai):
    runner.enqueue('research', prospect_id=prospect.id)
    runner.run_next()
    found = facts_of(prospect)
    assert found['luxury_positioning'].confidence == 'inferred'
    assert found['luxury_positioning'].extractor != 'rules'


def test_the_markup_wins_over_the_model_on_languages(prospect, with_ai):
    """The page declared fr, en and ru. The model said fr and en. We keep
    what the page said, because we saw it."""
    runner.enqueue('research', prospect_id=prospect.id)
    runner.run_next()
    languages = facts_of(prospect)['languages']
    assert languages.value == 'fr, en, ru'
    assert languages.confidence == 'verified'


def test_the_model_is_given_no_tools_and_told_the_page_is_untrusted(prospect, with_ai):
    runner.enqueue('research', prospect_id=prospect.id)
    runner.run_next()
    sent = with_ai.calls[0]
    assert 'tools' not in sent
    assert 'untrusted' in sent['messages'][0]['content'].lower()
    assert sent['response_format'] == {'type': 'json_object'}


def test_the_read_is_costed_against_the_prospect(prospect, with_ai):
    runner.enqueue('research', prospect_id=prospect.id)
    runner.run_next()
    cost = models.Cost.query.first()
    assert cost.prospect_id == prospect.id
    assert cost.purpose == 'research_read'
    assert cost.usd > 0


def test_one_agency_cannot_eat_the_budget(prospect, with_ai):
    """The per-prospect cap refuses the second read."""
    settings.set('research_cost_cap_usd', '0.0001')
    ai.record_cost('gpt-4o-mini', 'research_read', usd=0.01,
                   prospect_id=prospect.id)
    runner.enqueue('research', prospect_id=prospect.id)
    outcome = runner.run_next()

    assert with_ai.calls == [], 'it paid despite the cap'
    assert 'cap' in outcome['result']['note']
    assert facts_of(prospect)['chat_widget'].value == 'Crisp'   # rules still ran


def test_the_ai_half_can_be_switched_off_entirely(prospect, with_ai):
    settings.set('research_ai', 'off')
    runner.enqueue('research', prospect_id=prospect.id)
    outcome = runner.run_next()
    assert with_ai.calls == []
    assert 'switched off' in outcome['result']['note']
    assert 'chat_widget' in facts_of(prospect)


def test_a_model_that_answers_nonsense_changes_nothing(prospect):
    settings.set('research_ai', 'on')
    ai.set_client(FakeOpenAI('I am not JSON at all'))
    try:
        runner.enqueue('research', prospect_id=prospect.id)
        outcome = runner.run_next()
    finally:
        ai.set_client(app_module.client)
    assert outcome['ok'] is True
    assert 'luxury_positioning' not in facts_of(prospect)
    assert facts_of(prospect)['chat_widget'].value == 'Crisp'


# ─────────────────────────────────────────────────────
# Sites that will not cooperate
# ─────────────────────────────────────────────────────

def test_a_site_that_says_no_to_robots_is_left_for_a_person(prospect):
    Site.routes['/robots.txt'] = (200, b"User-agent: *\nDisallow: /\n")
    fetch.reset_caches()
    try:
        settings.set('research_ai', 'off')
        job = runner.enqueue('research', prospect_id=prospect.id)
        outcome = runner.run_next()
    finally:
        Site.routes['/robots.txt'] = (200, b"User-agent: *\nAllow: /\n")
        fetch.reset_caches()

    row = db.session.get(models.Prospect, prospect.id)
    assert row.needs_human is True
    assert db.session.get(models.Job, job.id).status == 'problem'
    assert 'by hand' in outcome['error']


def test_a_site_that_does_not_answer_is_tried_again_later(prospect, monkeypatch):
    monkeypatch.setattr(fetch, 'get', lambda url, **kwargs: fetch.Page(
        url=url, ok=False, error='ConnectTimeout: timed out'))
    settings.set('research_ai', 'off')
    job = runner.enqueue('research', prospect_id=prospect.id)
    runner.run_next()

    row = db.session.get(models.Job, job.id)
    assert row.status == 'queued'                 # waiting, not written off
    assert db.session.get(models.Prospect, prospect.id).needs_human is True


def test_one_job_cannot_outlast_the_servers_request_limit():
    """gunicorn stops a request at 30 seconds."""
    assert handlers.RESEARCH_BUDGET_SECONDS <= 25


def test_the_crawl_is_polite_and_still_fits_the_budget(prospect, monkeypatch):
    """One request a second to somebody else's server, five pages, and the
    whole job still has to come back inside gunicorn's limit. This is the
    one test that pays the real delay."""
    import time as clock
    monkeypatch.setattr(fetch, 'PER_HOST_DELAY', 1.0)
    settings.set('research_ai', 'off')

    runner.enqueue('research', prospect_id=prospect.id)
    started = clock.monotonic()
    outcome = runner.run_next()
    took = clock.monotonic() - started

    assert outcome['ok'] is True
    assert outcome['result']['pages'] >= 3
    assert took >= 2.0, 'it did not wait between requests'
    assert took < handlers.RESEARCH_BUDGET_SECONDS


# ─────────────────────────────────────────────────────
# The screens
# ─────────────────────────────────────────────────────

def test_research_can_be_queued_for_one_agency(admin, prospect):
    admin.post(f'/owner/acquisition/prospects/{prospect.id}/update',
               data={'csrf_token': 'test-csrf-token', 'action': 'research'})
    assert models.Job.query.filter_by(type='research',
                                      prospect_id=prospect.id).count() == 1


def test_research_can_be_queued_for_every_new_agency(admin, prospect):
    db.session.add(models.Prospect(canonical_domain='second.test', stage='new'))
    db.session.add(models.Prospect(canonical_domain='done.test', stage='researched'))
    db.session.commit()

    admin.post('/owner/acquisition/prospects/research-all',
               data={'csrf_token': 'test-csrf-token'})

    queued = [job.prospect_id for job in models.Job.query.filter_by(type='research').all()]
    assert len(queued) == 2, 'it queued an agency that was already read'


def test_pressing_it_twice_does_not_queue_the_same_agency_twice(admin, prospect):
    for _ in range(2):
        admin.post('/owner/acquisition/prospects/research-all',
                   data={'csrf_token': 'test-csrf-token'})
    assert models.Job.query.filter_by(type='research').count() == 1


def test_an_agency_marked_do_not_contact_is_left_alone(admin, prospect):
    prospect.do_not_contact = True
    db.session.commit()
    admin.post('/owner/acquisition/prospects/research-all',
               data={'csrf_token': 'test-csrf-token'})
    assert models.Job.query.filter_by(type='research').count() == 0


def test_the_screen_explains_what_verified_means(admin, prospect):
    run_research(prospect)
    page = admin.get(f'/owner/acquisition/prospects/{prospect.id}').get_data(as_text=True)
    assert 'Chat widget' in page          # the label, not the field name
    assert 'Crisp' in page
    assert 'saw it in the page' in page   # what verified means
    assert 'company inbox' in page
