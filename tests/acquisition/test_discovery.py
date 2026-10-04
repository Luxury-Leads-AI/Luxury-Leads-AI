"""Phase 2: finding agencies without typing them in.

Two jobs do the work. `discover` asks one source for candidates in one city;
`confirm_candidate` opens each candidate's website and only then creates a
prospect. The split matters: an AI search can produce a convincing agency
that does not exist, and the cheapest way to find out is to knock on the
door before writing the name down.

What is pinned here:

  - the fetcher refuses every address that is not on the public internet
    (this is the SSRF guard, and it is the most dangerous code in the
    engine: it opens addresses other people chose);
  - robots.txt is obeyed, and a human check is never worked around;
  - candidates that do not answer are discarded, not saved;
  - a site that answers but blocks robots is saved and flagged for a human;
  - nothing is added twice, nothing suppressed comes back, and shared hosts
    (Facebook, portals) never become an agency;
  - the money guards: the monthly search cap and the AI budget both refuse
    before a paid search happens;
  - both outside services are parsed from their real answer shapes, and a
    rename of OpenAI's tool is survivable without a deploy.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

import app as app_module
from acquisition import compliance, models, settings
from acquisition.jobs import runner
from acquisition.providers import discovery
from acquisition.services import ai, fetch, prospects

db = app_module.db


@pytest.fixture(autouse=True)
def _clean():
    for model in (models.Job, models.Cost, models.Audit, models.Suppression,
                  models.Prospect, models.Market, models.Setting):
        model.query.delete()
    db.session.commit()
    fetch.reset_caches()
    yield
    db.session.rollback()


@pytest.fixture()
def market():
    row = models.Market(country='France', city='Nice', language='fr',
                        legal_status='verified', outreach_method='email',
                        status='active', luxury_focus=True,
                        target_type='real estate agency')
    db.session.add(row)
    db.session.commit()
    return row


# ── a real little website to fetch, so the fetcher is tested for real ──

class Site(BaseHTTPRequestHandler):
    routes = {}

    def do_GET(self):                                    # noqa: N802
        status, headers, body = self.routes.get(
            self.path, (404, {'Content-Type': 'text/html'}, b'<h1>no</h1>'))
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def log_message(self, *args):                        # keep the test output clean
        pass


@pytest.fixture(scope='module')
def site():
    Site.routes = {
        '/': (200, {'Content-Type': 'text/html; charset=utf-8'},
              b'<html><head><title>Riviera Estates | Luxury homes in Nice</title>'
              b'</head><body>Villas</body></html>'),
        '/robots.txt': (200, {'Content-Type': 'text/plain'}, b"User-agent: *\nAllow: /\n"),
        '/gone': (404, {'Content-Type': 'text/html'}, b'<h1>gone</h1>'),
        '/wall': (403, {'Content-Type': 'text/html'}, b'<html>Please verify you are human</html>'),
        '/huge': (200, {'Content-Type': 'text/html'}, b'x' * (3 * 1024 * 1024)),
        '/loop': (302, {'Location': '/loop2'}, b''),
        '/loop2': (302, {'Location': '/loop'}, b''),
        '/moved': (302, {'Location': '/'}, b''),
    }
    server = HTTPServer(('127.0.0.1', 0), Site)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture()
def closed_site():
    """A site whose robots.txt says no."""
    Site.routes['/robots.txt'] = (200, {'Content-Type': 'text/plain'},
                                  b"User-agent: *\nDisallow: /\n")
    fetch.reset_caches()
    yield
    Site.routes['/robots.txt'] = (200, {'Content-Type': 'text/plain'},
                                  b"User-agent: *\nAllow: /\n")
    fetch.reset_caches()


# ─────────────────────────────────────────────────────────────
# The fetcher: the guard around the riskiest thing the engine does
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('address, public', [
    ('8.8.8.8', True), ('93.184.216.34', True), ('2606:2800:220:1:248:1893:25c8:1946', True),
    ('127.0.0.1', False), ('10.0.0.5', False), ('192.168.1.1', False),
    ('172.16.0.1', False), ('169.254.169.254', False), ('0.0.0.0', False),
    ('::1', False), ('fd00::1', False), ('224.0.0.1', False), ('not-an-address', False),
])
def test_only_ordinary_public_addresses_are_allowed(address, public):
    assert fetch.is_public_address(address) is public


@pytest.mark.parametrize('url, why', [
    ('file:///etc/passwd', 'http'),
    ('ftp://example.com/x', 'http'),
    ('gopher://example.com', 'http'),
    ('http://localhost:10000/delete-agency/3', 'private'),
    ('http://127.0.0.1/', 'private'),
    ('http://169.254.169.254/latest/meta-data/', 'private'),
    ('https://[::1]/', 'private'),
])
def test_the_fetcher_refuses_addresses_that_are_not_the_public_internet(url, why):
    page = fetch.get(url)
    assert page.ok is False
    assert why in page.error, page.error
    assert page.html == ''


def test_the_metadata_address_is_refused_by_name_as_well():
    """The one address that hands out cloud credentials to anything that
    asks. It is refused as link-local anyway; this is the belt."""
    assert fetch.is_public_address('169.254.169.254') is False
    with pytest.raises(fetch.Blocked):
        fetch.resolve('169.254.169.254')


def test_a_real_page_comes_back_with_its_title(site):
    page = fetch.get(site + '/', allow_private=True)
    assert page.ok is True and page.status == 200
    assert 'Villas' in page.html
    assert fetch.page_title(page.html) == 'Riviera Estates'


def test_robots_is_obeyed_rather_than_argued_with(site, closed_site):
    page = fetch.get(site + '/', allow_private=True)
    assert page.ok is False
    assert page.blocked_by_robots is True
    assert page.html == ''


def test_a_human_check_is_reported_not_worked_around(site):
    page = fetch.get(site + '/wall', allow_private=True)
    assert page.ok is False
    assert page.looks_like_bot_wall is True
    assert 'human check' in page.error


def test_a_huge_page_is_cut_off(site):
    page = fetch.get(site + '/huge', allow_private=True)
    assert len(page.html) <= fetch.MAX_BYTES


def test_redirects_are_followed_but_not_forever(site):
    straight = fetch.get(site + '/moved', allow_private=True)
    assert straight.ok is True and 'Riviera' in straight.html

    looping = fetch.get(site + '/loop', allow_private=True)
    assert looping.ok is False
    assert 'redirect' in looping.error


def test_one_request_per_second_per_site(site, monkeypatch):
    waits = []
    monkeypatch.setattr(fetch.time, 'sleep', lambda seconds: waits.append(seconds))
    fetch.get(site + '/', allow_private=True)
    fetch.get(site + '/', allow_private=True)
    assert waits, "the second request did not wait its turn"


def test_the_fetcher_says_who_it_is_and_how_to_stop_it():
    assert 'LuxuryLeadsAI' in fetch.USER_AGENT
    assert 'http' in fetch.USER_AGENT          # a page explaining the bot
    assert 'stop' in fetch.USER_AGENT.lower()


# ─────────────────────────────────────────────────────────────
# OpenStreetMap
# ─────────────────────────────────────────────────────────────

NOMINATIM_ANSWER = [{
    'place_id': 1, 'osm_type': 'relation', 'osm_id': 170100,
    'boundingbox': ['43.6460', '43.7604', '7.1819', '7.3275'],
    'display_name': 'Nice, Alpes-Maritimes, France',
}]

OVERPASS_ANSWER = {
    'version': 0.6,
    'elements': [
        {'type': 'node', 'id': 1, 'lat': 43.7, 'lon': 7.26, 'tags': {
            'office': 'estate_agent', 'name': 'Riviera Estates',
            'website': 'https://riviera-estates.fr', 'phone': '+33 4 93 00 00 00',
            'addr:housenumber': '12', 'addr:street': 'Promenade des Anglais',
            'addr:postcode': '06000', 'addr:city': 'Nice'}},
        {'type': 'way', 'id': 2, 'center': {'lat': 43.7, 'lon': 7.27}, 'tags': {
            'office': 'estate_agent', 'name': 'Azur Prestige',
            'contact:website': 'http://www.azur-prestige.fr/accueil',
            'contact:phone': '+33 4 93 11 11 11'}},
        {'type': 'node', 'id': 3, 'tags': {
            'office': 'estate_agent', 'name': 'No Website Immobilier'}},
        {'type': 'node', 'id': 4, 'tags': {
            'shop': 'estate_agent', 'name': 'Facebook Only Agency',
            'website': 'https://facebook.com/some-agency'}},
    ],
}


class FakeHTTP:
    """Stands in for httpx: records what was asked, answers what we say."""
    def __init__(self, get_answer=None, post_answer=None, get_status=200, post_status=200):
        self.get_answer, self.post_answer = get_answer, post_answer
        self.get_status, self.post_status = get_status, post_status
        self.gets, self.posts = [], []

    class Response:
        def __init__(self, status, payload):
            self.status_code, self._payload = status, payload
            self.text = payload if isinstance(payload, str) else json.dumps(payload)

        def json(self):
            if isinstance(self._payload, str):
                raise ValueError('not json')
            return self._payload

    def get(self, url, **kwargs):
        self.gets.append((url, kwargs))
        return self.Response(self.get_status, self.get_answer)

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return self.Response(self.post_status, self.post_answer)


def test_openstreetmap_candidates_are_read_from_a_real_answer_shape(market):
    http = FakeHTTP(get_answer=NOMINATIM_ANSWER, post_answer=OVERPASS_ANSWER)
    found = discovery.OSMDiscovery(http=http).search(market, limit=40)

    assert found.error == ''
    assert [c.name for c in found.candidates] == [
        'Riviera Estates', 'Azur Prestige', 'Facebook Only Agency']
    first = found.candidates[0]
    assert first.website == 'https://riviera-estates.fr'
    assert first.phone == '+33 4 93 00 00 00'
    assert '12 Promenade des Anglais 06000 Nice' == first.address
    assert first.source_ref == 'osm:node/1'
    assert found.candidates[1].phone == '+33 4 93 11 11 11'   # contact:phone
    assert 'no website recorded' in found.note                # the skipped one
    assert found.usd == 0.0


def test_the_city_is_looked_up_once_and_remembered(market):
    http = FakeHTTP(get_answer=NOMINATIM_ANSWER, post_answer=OVERPASS_ANSWER)
    provider = discovery.OSMDiscovery(http=http)
    provider.search(market, limit=10)
    provider.search(market, limit=10)
    assert len(http.gets) == 1, "the city was looked up twice"
    assert settings.get(f'osm_bbox:{market.id}') == '43.646,43.7604,7.1819,7.3275'


def test_the_query_asks_for_estate_agents_inside_the_city(market):
    http = FakeHTTP(get_answer=NOMINATIM_ANSWER, post_answer=OVERPASS_ANSWER)
    discovery.OSMDiscovery(http=http).search(market, limit=15)
    query = http.posts[0][1]['data']['data']
    assert 'office"="estate_agent' in query
    assert '43.646,7.1819,43.7604,7.3275' in query          # south,west,north,east
    assert 'out center tags 15;' in query


def test_openstreetmap_is_told_who_is_asking(market):
    """Overpass and Nominatim are volunteer-run and ask for this."""
    http = FakeHTTP(get_answer=NOMINATIM_ANSWER, post_answer=OVERPASS_ANSWER)
    discovery.OSMDiscovery(http=http).search(market, limit=5)
    for _url, kwargs in http.gets + http.posts:
        assert 'LuxuryLeadsAI' in kwargs['headers']['User-Agent']


def test_an_unknown_city_is_reported_plainly(market):
    http = FakeHTTP(get_answer=[], post_answer=OVERPASS_ANSWER)
    found = discovery.OSMDiscovery(http=http).search(market)
    assert 'does not know a city' in found.error
    assert found.candidates == []


def test_a_busy_overpass_is_not_hammered(market):
    http = FakeHTTP(get_answer=NOMINATIM_ANSWER, post_answer='too many requests',
                    post_status=429)
    found = discovery.OSMDiscovery(http=http).search(market)
    assert 'busy' in found.error


# ─────────────────────────────────────────────────────────────
# OpenAI web search
# ─────────────────────────────────────────────────────────────

def openai_answer(text, tokens_in=8200, tokens_out=300):
    return {'id': 'resp_1', 'model': 'gpt-4o-mini',
            'output': [
                {'type': 'web_search_call', 'id': 'ws_1', 'status': 'completed'},
                {'type': 'message', 'role': 'assistant', 'content': [
                    {'type': 'output_text', 'text': text, 'annotations': [
                        {'type': 'url_citation', 'url': 'https://example.fr',
                         'title': 'Agencies in Nice'}]}]}],
            'usage': {'input_tokens': tokens_in, 'output_tokens': tokens_out}}


AGENCIES_JSON = json.dumps({'agencies': [
    {'name': 'Côte d\'Azur Prestige', 'website': 'https://cote-azur-prestige.fr',
     'phone': '+33 4 93 22 22 22', 'address': '5 Rue de France, Nice'},
    {'name': 'Nice Luxury Homes', 'website': 'nice-luxury-homes.fr'},
    {'name': 'No Website Agency'},
]})


def test_an_ai_search_is_read_and_costed(market):
    settings.set('web_search_usd', '0.01')
    http = FakeHTTP(post_answer=openai_answer(AGENCIES_JSON))
    provider = discovery.OpenAIWebSearchDiscovery(api_key='sk-test', http=http)

    found = provider.search(market, limit=10, job_id=7)
    assert found.error == ''
    assert [c.name for c in found.candidates] == ["Côte d'Azur Prestige", 'Nice Luxury Homes']
    assert found.searched == 1

    cost = models.Cost.query.filter_by(purpose='discovery_search').one()
    assert cost.units == 1
    assert cost.input_tokens == 8200
    # the tool charge plus the tokens, both on the bill
    assert cost.usd == pytest.approx(0.01 + ai.estimate_usd('gpt-4o-mini', 8200, 300))
    assert cost.job_id == 7


def test_the_prompt_asks_for_the_right_city_and_bans_portal_pages(market):
    http = FakeHTTP(post_answer=openai_answer(AGENCIES_JSON))
    discovery.OpenAIWebSearchDiscovery(api_key='sk-test', http=http).search(market, limit=12)
    sent = http.posts[0][1]['json']
    assert sent['tools'] == [{'type': 'web_search'}]
    assert 'Nice, France' in sent['input']
    assert 'luxury' in sent['input']
    assert 'Facebook' in sent['input'] and 'portal' in sent['input']
    assert 'JSON only' in sent['input']


def test_json_wrapped_in_chatter_is_still_read(market):
    wrapped = "Here are the agencies I found:\n\n```json\n" + AGENCIES_JSON + "\n```\nHope that helps!"
    http = FakeHTTP(post_answer=openai_answer(wrapped))
    found = discovery.OpenAIWebSearchDiscovery(api_key='sk-test', http=http).search(market)
    assert len(found.candidates) == 2


def test_a_renamed_tool_is_survived_without_a_deploy(market):
    """If OpenAI renames the tool, the engine tries the other spelling and
    remembers which one worked - a setting, not a deploy."""
    class Fussy(FakeHTTP):
        def post(self, url, **kwargs):
            self.posts.append((url, kwargs))
            if kwargs['json']['tools'][0]['type'] == 'web_search':
                return self.Response(400, {'error': {'message': 'Unknown tool web_search'}})
            return self.Response(200, openai_answer(AGENCIES_JSON))

    http = Fussy()
    found = discovery.OpenAIWebSearchDiscovery(api_key='sk-test', http=http).search(market)
    assert found.error == ''
    assert len(found.candidates) == 2
    assert settings.get('web_search_tool_type') == 'web_search_preview'


def test_an_api_error_is_shown_not_swallowed(market):
    http = FakeHTTP(post_answer={'error': {'message': 'You exceeded your quota'}},
                    post_status=429)
    found = discovery.OpenAIWebSearchDiscovery(api_key='sk-test', http=http).search(market)
    assert '429' in found.error and 'quota' in found.error
    assert models.Cost.query.count() == 0


def test_no_key_means_no_call(market):
    http = FakeHTTP(post_answer=openai_answer(AGENCIES_JSON))
    found = discovery.OpenAIWebSearchDiscovery(api_key='', http=http).search(market)
    assert 'No OpenAI key' in found.error
    assert http.posts == []


def test_the_budget_refuses_before_a_paid_search(market):
    settings.set('ai_budget_usd_month', '0.005')
    ai.record_cost('gpt-4o-mini', 'extract', usd=0.004)
    http = FakeHTTP(post_answer=openai_answer(AGENCIES_JSON))
    found = discovery.OpenAIWebSearchDiscovery(api_key='sk-test', http=http).search(market)
    assert 'budget' in found.error.lower()
    assert http.posts == []


# ─────────────────────────────────────────────────────────────
# The discover job
# ─────────────────────────────────────────────────────────────

def fake_found(*candidates, **kwargs):
    return discovery.Found(source='osm', candidates=list(candidates), **kwargs)


def patch_source(monkeypatch, found, costs_money=False):
    class Fake:
        name, label, costs_money = 'osm', 'fake', False
        def search(self, market, limit=20, **kwargs):
            return found
    Fake.costs_money = costs_money
    monkeypatch.setattr(discovery, 'get', lambda name: Fake())


def test_discovery_queues_a_check_for_every_new_candidate(monkeypatch, market):
    patch_source(monkeypatch, fake_found(
        discovery.Candidate(name='A', website='https://a-realty.fr'),
        discovery.Candidate(name='B', website='https://b-realty.fr')))
    runner.enqueue('discover', payload={'market_id': market.id, 'source': 'osm'})
    outcome = runner.run_next()

    assert outcome['ok'] is True
    assert outcome['result']['queued'] == 2
    assert models.Prospect.query.count() == 0, "nothing is saved before it is checked"
    assert models.Job.query.filter_by(type='confirm_candidate', status='queued').count() == 2


def test_candidates_you_already_have_are_counted_not_queued(monkeypatch, market):
    prospects.add('https://a-realty.fr', market_id=market.id)
    patch_source(monkeypatch, fake_found(
        discovery.Candidate(name='A', website='http://www.a-realty.fr/contact'),
        discovery.Candidate(name='B', website='https://b-realty.fr')))
    runner.enqueue('discover', payload={'market_id': market.id, 'source': 'osm'})
    result = runner.run_next()['result']
    assert result == dict(result, queued=1, already_had=1)


def test_a_suppressed_domain_never_comes_back(monkeypatch, market):
    compliance.suppress(domain='stop-it.fr', reason='they asked')
    patch_source(monkeypatch, fake_found(
        discovery.Candidate(name='Stop', website='https://stop-it.fr')))
    runner.enqueue('discover', payload={'market_id': market.id, 'source': 'osm'})
    result = runner.run_next()['result']
    assert result['queued'] == 0 and result['unusable'] == 1


def test_portal_and_social_pages_are_not_agencies(monkeypatch, market):
    patch_source(monkeypatch, fake_found(
        discovery.Candidate(name='FB', website='https://facebook.com/agency'),
        discovery.Candidate(name='Portal', website='https://www.rightmove.co.uk/x'),
        discovery.Candidate(name='Real', website='https://real-agency.fr')))
    runner.enqueue('discover', payload={'market_id': market.id, 'source': 'osm'})
    result = runner.run_next()['result']
    assert result['queued'] == 1 and result['unusable'] == 2


def test_the_monthly_search_cap_stops_paid_searches(monkeypatch, market):
    settings.set('web_search_cap_month', '2')
    for _ in range(2):
        ai.record_cost('gpt-4o-mini', 'discovery_search', units=1, usd=0.01)
    patch_source(monkeypatch, fake_found(
        discovery.Candidate(name='A', website='https://a-realty.fr')), costs_money=True)
    runner.enqueue('discover', payload={'market_id': market.id, 'source': 'openai'})
    result = runner.run_next()['result']
    assert 'cap' in result['error']
    assert models.Job.query.filter_by(type='confirm_candidate').count() == 0


def test_a_free_source_is_not_capped(monkeypatch, market):
    settings.set('web_search_cap_month', '1')
    ai.record_cost('gpt-4o-mini', 'discovery_search', units=5, usd=0.05)
    patch_source(monkeypatch, fake_found(
        discovery.Candidate(name='A', website='https://a-realty.fr')), costs_money=False)
    runner.enqueue('discover', payload={'market_id': market.id, 'source': 'osm'})
    assert runner.run_next()['result']['queued'] == 1


def test_a_source_that_fails_says_why(monkeypatch, market):
    patch_source(monkeypatch, discovery.Found(source='osm', error='Overpass answered 504'))
    runner.enqueue('discover', payload={'market_id': market.id, 'source': 'osm'})
    assert runner.run_next()['result']['error'] == 'Overpass answered 504'


# ─────────────────────────────────────────────────────────────
# Confirming a candidate: the step that keeps invented agencies out
# ─────────────────────────────────────────────────────────────

def confirm(market, website, name='', **extra):
    runner.enqueue('confirm_candidate', payload={
        'market_id': market.id, 'source': 'osm',
        'candidate': {'website': website, 'name': name, **extra}})
    return runner.run_next()


@pytest.fixture()
def door(monkeypatch):
    """Lets a test say what the candidate's website does when we knock.

    The fetcher itself is tested for real against the little site above;
    here the question is what the job does with each kind of answer.
    """
    holder = {'page': fetch.Page(url='https://x/', ok=True, status=200,
                                 html='<title>Riviera Estates | Nice</title>')}
    monkeypatch.setattr(fetch, 'site_answers',
                        lambda domain, allow_private=False: holder['page'])
    return holder


def test_a_real_site_becomes_a_prospect_with_its_own_name(door, market):
    outcome = confirm(market, 'https://www.riviera-estates.fr/about')
    assert outcome['ok'] is True
    prospect = models.Prospect.query.one()
    assert prospect.canonical_domain == 'riviera-estates.fr'
    assert prospect.name == 'Riviera Estates'          # from the page title
    assert prospect.source == 'osm'
    assert prospect.needs_human is False
    assert prospect.market_id == market.id


def test_a_name_from_the_source_is_kept(door, market):
    confirm(market, 'https://azur-prestige.fr', name='Azur Prestige')
    assert models.Prospect.query.one().name == 'Azur Prestige'


def test_an_invented_agency_is_thrown_away(door, market):
    """The whole reason confirmation exists: a search can produce a name and
    a website that simply do not exist."""
    door['page'] = fetch.Page(url='https://nope/', ok=False, status=0,
                              error='ConnectError: name does not resolve')
    outcome = confirm(market, 'https://this-agency-does-not-exist.fr/')
    assert outcome['ok'] is True
    assert 'discarded' in outcome['result']
    assert models.Prospect.query.count() == 0


def test_a_site_that_blocks_robots_is_kept_but_flagged(door, market):
    door['page'] = fetch.Page(url='https://shy.fr/', ok=False, blocked_by_robots=True,
                              error="that site's robots.txt asks us not to read this page")
    outcome = confirm(market, 'https://shy-agency.fr')
    prospect = models.Prospect.query.one()
    assert outcome['result']['needs_human'] is True
    assert prospect.needs_human is True
    assert 'robots' in prospect.needs_human_reason


def test_a_site_behind_a_human_check_is_kept_but_flagged(door, market):
    door['page'] = fetch.Page(url='https://walled.fr/', ok=False, status=403,
                              looks_like_bot_wall=True)
    confirm(market, 'https://walled-agency.fr')
    prospect = models.Prospect.query.one()
    assert prospect.needs_human is True
    assert 'human check' in prospect.needs_human_reason


def test_confirming_the_same_candidate_twice_adds_one_prospect(door, market):
    confirm(market, 'https://riviera-estates.fr')
    confirm(market, 'http://www.riviera-estates.fr/contact')
    assert models.Prospect.query.count() == 1


def test_the_job_really_does_open_the_website(market, monkeypatch):
    """Belt and braces: the job asks the fetcher, not something else."""
    asked = []
    monkeypatch.setattr(fetch, 'site_answers',
                        lambda domain, allow_private=False: asked.append(domain) or
                        fetch.Page(ok=True, status=200, html='<title>Hi</title>'))
    confirm(market, 'https://knocked-on.fr')
    assert asked == ['knocked-on.fr']


def test_the_same_candidate_is_never_queued_twice(monkeypatch, market):
    patch_source(monkeypatch, fake_found(
        discovery.Candidate(name='A', website='https://a-realty.fr')))
    for _ in range(2):
        runner.enqueue('discover', payload={'market_id': market.id, 'source': 'osm'})
        runner.run_next()
    assert models.Job.query.filter_by(type='confirm_candidate').count() == 1


# ─────────────────────────────────────────────────────────────
# The screen
# ─────────────────────────────────────────────────────────────

@pytest.fixture()
def admin(client):
    with client.session_transaction() as sess:
        sess['super_admin'] = True
        sess['acq_csrf'] = 'test-csrf-token'
    return client


def form(**fields):
    fields.setdefault('csrf_token', 'test-csrf-token')
    return fields


def test_the_screen_queues_a_search(admin, market):
    settings.set('discovery_enabled', 'manual,osm')
    admin.post('/owner/acquisition/prospects/discover',
               data=form(market_id=market.id, source='osm', limit=30))
    job = models.Job.query.filter_by(type='discover').one()
    assert json.loads(job.payload) == {'market_id': market.id, 'source': 'osm', 'limit': 30}
    assert models.Audit.query.filter_by(action='discovery_queued').count() == 1


def test_a_source_switched_off_cannot_be_run_from_the_screen(admin, market):
    settings.set('discovery_enabled', 'manual,osm')
    response = admin.post('/owner/acquisition/prospects/discover',
                          data=form(market_id=market.id, source='openai'))
    assert 'switched+off' in response.headers['Location']
    assert models.Job.query.count() == 0


def test_the_search_form_only_offers_sources_that_are_on(admin, market):
    settings.set('discovery_enabled', 'manual,osm')
    html = admin.get('/owner/acquisition/prospects').get_data(as_text=True)
    assert 'OpenStreetMap (free)' in html
    assert 'AI web search' not in html

    settings.set('discovery_enabled', 'manual,osm,openai')
    html = admin.get('/owner/acquisition/prospects').get_data(as_text=True)
    assert 'AI web search' in html


def test_today_shows_what_the_last_search_found(admin, market, monkeypatch):
    patch_source(monkeypatch, fake_found(
        discovery.Candidate(name='A', website='https://a-realty.fr')))
    runner.enqueue('discover', payload={'market_id': market.id, 'source': 'osm'})
    runner.run_next()
    html = admin.get('/owner/acquisition/').get_data(as_text=True)
    assert 'Last searches' in html
    assert 'Nice, France' in html
