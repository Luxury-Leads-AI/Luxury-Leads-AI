"""Phase 2.1: getting out of the building.

The first real search on Render came back with one sentence - "ConnectError:
[Errno 101] Network is unreachable" - and that sentence is the problem this
file exists to stop repeating.

A hostname usually has several addresses, some IPv4 and some IPv6, and a
server does not necessarily have a route to all of them. Python's
create_connection tries them in turn and then raises only the LAST error, so
the message you are left with describes one address and says nothing about
the others. If IPv4 worked you would not be reading the message at all; if
IPv4 failed too, its reason has been thrown away.

So three things are pinned here:

  - every address a name has is tried, IPv4 first, because every hosting
    provider gives a container an IPv4 route and plenty hand out an IPv6
    address with nothing behind it;
  - when nothing answers, the error names every address and what each one
    said, not just the last;
  - a job that comes back with a problem is not painted green. It ran, it
    reported bad news, and the queue says so - a tick over "Network is
    unreachable" is how a dead engine goes unnoticed for a week.

Plus the screen that answers the question directly: Jobs -> Check the
connection, which opens each address on its own and shows every answer.
"""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

import app as app_module
from acquisition import models, settings
from acquisition.jobs import registry, runner
from acquisition.providers import discovery
from acquisition.services import fetch

db = app_module.db


@pytest.fixture(autouse=True)
def _clean():
    for model in (models.Job, models.Cost, models.Audit, models.Prospect,
                  models.Market, models.Setting):
        model.query.delete()
    db.session.commit()
    fetch.reset_caches()
    yield
    db.session.rollback()


@pytest.fixture()
def market():
    row = models.Market(country='France', city='Nice', language='fr',
                        legal_status='verified', outreach_method='email',
                        status='active', target_type='real estate agency')
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture()
def admin(client):
    with client.session_transaction() as sess:
        sess['super_admin'] = True
        sess['acq_csrf'] = 'test-csrf-token'
    return client


# ── a fake httpx that answers at one address and nowhere else ──

class FakeResponse:
    def __init__(self, status=200, text='<title>Riviera</title>', headers=None):
        self.status_code = status
        self.text = text
        self.content = text.encode('utf-8')
        self.encoding = 'utf-8'
        self.headers = headers or {'content-type': 'text/html'}
        self.is_redirect = False

    def json(self):
        return json.loads(self.text)


def only_answers_at(address, seen=None, status=200):
    """A client_factory whose connections work at one address only.

    This is a server with no IPv6 route, in a test: the dead address raises
    the same ConnectError httpx raises, instantly.
    """
    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def request(self, method, url, **kwargs):
            if seen is not None:
                seen.append(url)
            if address and address in url:
                return FakeResponse(status=status)
            raise httpx.ConnectError('[Errno 101] Network is unreachable')

    return Client


TWO_ADDRESSES = ['2a04:4e42::347', '151.101.1.91']


@pytest.fixture()
def ipv6_first(monkeypatch):
    """A name whose IPv6 address is the one the engine would try first if
    nobody had thought about it."""
    monkeypatch.setattr(fetch, 'resolve',
                        lambda host, allow_private=False: list(TWO_ADDRESSES))


# ─────────────────────────────────────────────────────
# Which address gets tried, and in what order
# ─────────────────────────────────────────────────────

def test_ipv4_addresses_are_tried_before_ipv6():
    mixed = ['2a04:4e42::347', '151.101.1.91', '2606:4700:7::f3', '93.184.216.34']
    ordered = fetch.order_addresses(mixed)
    assert [':' in address for address in ordered] == [False, False, True, True]


def test_the_order_is_the_same_every_time():
    """A retry that picks a different address is a bug you cannot reproduce."""
    mixed = ['2a04:4e42::347', '151.101.1.91', '2606:4700:7::f3', '93.184.216.34']
    assert fetch.order_addresses(mixed) == fetch.order_addresses(list(reversed(mixed)))


def test_resolve_puts_ipv4_first_whatever_dns_hands_back(monkeypatch):
    def getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, '', ('2a04:4e42::347', 0, 0, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('151.101.1.91', 0))]

    monkeypatch.setattr(fetch.socket, 'getaddrinfo', getaddrinfo)
    assert fetch.resolve('nominatim.example.org') == ['151.101.1.91', '2a04:4e42::347']


def test_a_name_with_one_private_address_is_still_refused(monkeypatch):
    """Ordering must not quietly let a private address through: one bad
    address poisons the name, however the rest sort."""
    def getaddrinfo(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 0))]

    monkeypatch.setattr(fetch.socket, 'getaddrinfo', getaddrinfo)
    with pytest.raises(fetch.Blocked):
        fetch.resolve('sneaky.example.org')


# ─────────────────────────────────────────────────────
# One dead address must not end the attempt
# ─────────────────────────────────────────────────────

def test_a_dead_address_falls_through_to_the_live_one(monkeypatch, ipv6_first):
    seen = []
    monkeypatch.setattr(fetch, 'client_factory', only_answers_at('151.101.1.91', seen))

    page = fetch.get('https://nominatim.example.org/', obey_robots=False)

    assert page.ok, page.error
    assert page.ip == '151.101.1.91'
    assert any('2a04' in url for url in seen), 'the IPv6 address was never tried'


def test_when_nothing_answers_the_error_names_every_address(monkeypatch, ipv6_first):
    monkeypatch.setattr(fetch, 'client_factory', only_answers_at(None))

    page = fetch.get('https://nominatim.example.org/', obey_robots=False)

    assert not page.ok
    for address in TWO_ADDRESSES:
        assert address in page.error, f"{address} is missing from: {page.error}"
    assert len(page.tried) == 2


def test_a_chosen_service_also_tries_every_address(monkeypatch, ipv6_first):
    monkeypatch.setattr(fetch, 'client_factory', only_answers_at('151.101.1.91'))
    response = fetch.request('GET', 'https://nominatim.example.org/search')
    assert response.status_code == 200


def test_unreachable_carries_the_whole_story(monkeypatch, ipv6_first):
    monkeypatch.setattr(fetch, 'client_factory', only_answers_at(None))

    with pytest.raises(fetch.Unreachable) as caught:
        fetch.request('GET', 'https://nominatim.example.org/search')

    assert caught.value.host == 'nominatim.example.org'
    assert len(caught.value.tried) == 2
    assert 'Network is unreachable' in str(caught.value)


# ─────────────────────────────────────────────────────
# What the person reads when it goes wrong
# ─────────────────────────────────────────────────────

class DeadService:
    """Every call fails the way Render failed."""

    def __init__(self, host='nominatim.openstreetmap.org'):
        self.host = host

    def _die(self, *args, **kwargs):
        raise fetch.Unreachable(self.host, [
            ('2a04:4e42::347', 'ConnectError: [Errno 101] Network is unreachable'),
            ('151.101.1.91', 'ConnectTimeout: timed out')])

    get = _die
    post = _die


def test_a_failed_search_says_which_service_and_which_addresses(market):
    found = discovery.OSMDiscovery(http=DeadService()).search(market, limit=5)

    assert 'Nominatim' in found.error
    assert '2a04:4e42::347' in found.error
    assert '151.101.1.91' in found.error
    assert 'Check the connection' in found.error


def test_the_openai_search_explains_itself_too(market, monkeypatch):
    monkeypatch.setattr(settings, 'get_float', lambda key, default=0.0: 0.0)
    provider = discovery.OpenAIWebSearchDiscovery(api_key='sk-test',
                                                  http=DeadService('api.openai.com'))
    found = provider.search(market, limit=5)
    assert 'OpenAI' in found.error
    assert 'api.openai.com' in found.error


# ─────────────────────────────────────────────────────
# A job that reports a problem is not a job that worked
# ─────────────────────────────────────────────────────

@pytest.fixture()
def sulking_handler():
    """A handler that runs fine and comes back with bad news."""
    def sulk(job, payload):
        return {'error': 'could not reach Nominatim at nominatim.openstreetmap.org',
                'source': 'osm', 'market': 'Nice, France'}

    registry.register('test_sulk', sulk)
    yield 'test_sulk'
    registry._HANDLERS.pop('test_sulk', None)


def test_a_job_that_comes_back_with_a_problem_is_not_painted_green(sulking_handler):
    job = runner.enqueue(sulking_handler)
    outcome = runner.run_next()

    assert outcome['ran'] is True
    assert outcome['ok'] is False
    assert outcome['problem'] is True
    assert db.session.get(models.Job, job.id).status == 'problem'


def test_the_problem_is_kept_where_the_screens_look(sulking_handler):
    job = runner.enqueue(sulking_handler)
    runner.run_next()

    row = db.session.get(models.Job, job.id)
    assert 'could not reach Nominatim' in row.last_error
    assert json.loads(row.result)['source'] == 'osm'      # the result is still there


def test_a_problem_is_not_retried_on_its_own(sulking_handler):
    """It is not a crash. Retrying a service that will not answer just
    burns the queue; the person decides when the cause is fixed."""
    runner.enqueue(sulking_handler)
    runner.run_next()
    assert runner.queue_depth() == 0


def test_a_handler_that_says_nothing_is_still_a_plain_success():
    def quiet(job, payload):
        return {'queued': 3, 'error': ''}

    registry.register('test_quiet', quiet)
    try:
        job = runner.enqueue('test_quiet')
        outcome = runner.run_next()
        assert outcome['ok'] is True
        assert db.session.get(models.Job, job.id).status == 'done'
    finally:
        registry._HANDLERS.pop('test_quiet', None)


def test_the_jobs_screen_shows_the_problem_and_offers_a_retry(admin, sulking_handler):
    runner.enqueue(sulking_handler)
    runner.run_next()

    page = admin.get('/owner/acquisition/jobs').get_data(as_text=True)
    assert 'tag warn' in page
    assert 'problem' in page
    assert 'Try again' in page


def test_a_search_that_failed_still_appears_on_today(admin, market):
    """The old screen only listed searches marked done, so a failed one
    vanished - which is exactly when you want to see it."""
    def dead_discover(job, payload):
        return {'error': 'could not reach Nominatim', 'source': 'osm',
                'market': 'Nice, France', 'found': 0, 'queued': 0}

    registry.register('discover', dead_discover)
    try:
        job = runner.enqueue('discover', payload={'market_id': market.id})
        runner.run_next()
        assert db.session.get(models.Job, job.id).status == 'problem'
        page = admin.get('/owner/acquisition/').get_data(as_text=True)
        assert 'could not reach Nominatim' in page
    finally:
        from acquisition.jobs import handlers                  # noqa: F401
        registry.register('discover', handlers.discover)


# ─────────────────────────────────────────────────────
# The screen that answers the question
# ─────────────────────────────────────────────────────

def test_the_connection_screen_needs_the_super_admin_session(client):
    response = client.get('/owner/acquisition/connection')
    assert response.status_code in (302, 401)


def test_the_connection_screen_does_nothing_until_you_press_it(admin):
    page = admin.get('/owner/acquisition/connection').get_data(as_text=True)
    assert 'Check now' in page
    assert 'Address' not in page            # no table before anything has run


def test_the_check_shows_every_address_and_what_it_said(admin, monkeypatch):
    def report(url, **kwargs):
        return {'url': url, 'host': 'nominatim.openstreetmap.org', 'ok': True,
                'status': 200, 'error': '', 'detail': '', 'ms': 120,
                'extra_addresses': 0,
                'addresses': [
                    {'address': '151.101.1.91', 'family': 'IPv4', 'ok': True,
                     'detail': 'answered', 'ms': 30},
                    {'address': '2a04:4e42::347', 'family': 'IPv6', 'ok': False,
                     'detail': 'OSError: [Errno 101] Network is unreachable',
                     'ms': 0}]}

    monkeypatch.setattr(fetch, 'connection_report', report)
    page = admin.post('/owner/acquisition/connection',
                      data={'csrf_token': 'test-csrf-token'}).get_data(as_text=True)

    assert '151.101.1.91' in page
    assert '2a04:4e42::347' in page
    assert 'Network is unreachable' in page
    assert 'IPv4 yes, IPv6 no' in page       # and what that means


def test_the_check_tests_the_servers_the_settings_point_at(admin, monkeypatch):
    settings.set('osm_overpass_url', 'https://overpass.example.org/api/interpreter')
    asked = []

    def report(url, **kwargs):
        asked.append(url)
        return {'url': url, 'host': '', 'ok': True, 'status': 200, 'error': '',
                'detail': '', 'ms': 1, 'addresses': [], 'extra_addresses': 0}

    monkeypatch.setattr(fetch, 'connection_report', report)
    admin.post('/owner/acquisition/connection', data={'csrf_token': 'test-csrf-token'})

    assert 'https://overpass.example.org/api/interpreter' in asked
    assert any('nominatim' in url for url in asked)


def test_the_check_is_reachable_from_the_jobs_screen(admin):
    page = admin.get('/owner/acquisition/jobs').get_data(as_text=True)
    assert '/owner/acquisition/connection' in page


# ── the report itself, against a real socket ──

class Tiny(BaseHTTPRequestHandler):
    def do_GET(self):                                    # noqa: N802
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.end_headers()
        self.wfile.write(b'<html><title>up</title></html>')

    def log_message(self, *args):
        pass


@pytest.fixture(scope='module')
def tiny_site():
    server = HTTPServer(('127.0.0.1', 0), Tiny)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def test_the_report_separates_the_address_that_works_from_the_one_that_does_not(
        tiny_site, monkeypatch):
    """The whole point: one line per address, so "unreachable" stops being
    a single sentence about a single address."""
    monkeypatch.setattr(fetch, 'resolve',
                        lambda host, allow_private=False: ['192.0.2.1', '127.0.0.1'])

    report = fetch.connection_report(tiny_site + '/', timeout=1)

    rows = {row['address']: row for row in report['addresses']}
    assert rows['127.0.0.1']['ok'] is True
    assert rows['192.0.2.1']['ok'] is False
    assert report['ok'] is True
    assert report['status'] == 200


def test_a_name_that_does_not_resolve_says_so_rather_than_hanging(monkeypatch):
    def gaierror(host, port, *args, **kwargs):
        raise socket.gaierror(-2, 'Name or service not known')

    monkeypatch.setattr(fetch.socket, 'getaddrinfo', gaierror)
    report = fetch.connection_report('https://not-a-real-host.example/')
    assert report['ok'] is False
    assert 'does not resolve' in report['error']
    assert report['addresses'] == []


def test_the_report_does_not_spend_money_or_write_anything(tiny_site, monkeypatch):
    monkeypatch.setattr(fetch, 'resolve', lambda host, allow_private=False: ['127.0.0.1'])
    fetch.connection_report(tiny_site + '/', timeout=1)
    assert models.Cost.query.count() == 0
    assert models.Job.query.count() == 0


# ─────────────────────────────────────────────────────
# The boundary still holds
# ─────────────────────────────────────────────────────

def test_discovery_goes_through_the_guarded_fetcher_not_raw_httpx():
    """Raw httpx in a provider would quietly skip the address handling,
    the rate limit and the SSRF guard - which is how this bug arrives
    again by a different door."""
    assert isinstance(discovery.OSMDiscovery().http, fetch.Http)
    assert isinstance(discovery.OpenAIWebSearchDiscovery(api_key='x').http, fetch.Http)


def test_a_chosen_service_is_not_asked_for_permission_by_robots(monkeypatch):
    """robots.txt governs crawling someone's website. An API we are a
    client of is not that, and asking would double every request."""
    calls = []
    monkeypatch.setattr(fetch, 'robots_allows',
                        lambda *args, **kwargs: calls.append(args) or True)
    monkeypatch.setattr(fetch, 'resolve',
                        lambda host, allow_private=False: ['151.101.1.91'])
    monkeypatch.setattr(fetch, 'client_factory', only_answers_at('151.101.1.91'))

    fetch.request('GET', 'https://nominatim.example.org/search')
    assert calls == []


def test_an_address_that_answers_but_blocks_the_request_says_so(monkeypatch, tiny_site):
    """The hardest case to read from a job's error: the connection is
    accepted and the request still does not get through. That is a proxy
    or a firewall in between, not a dead service, and it needs saying."""
    monkeypatch.setattr(fetch, 'resolve', lambda host, allow_private=False: ['127.0.0.1'])
    monkeypatch.setattr(fetch, 'client_factory', only_answers_at(None))

    report = fetch.connection_report(tiny_site + '/', timeout=1)

    assert report['ok'] is False
    assert report['tcp_ok'] is True
    assert report['verdict'] == 'blocked'
    assert 'did not get through' in report['summary']


def test_nothing_accepting_a_connection_is_called_what_it_is(monkeypatch):
    monkeypatch.setattr(fetch, 'resolve', lambda host, allow_private=False: ['192.0.2.1'])
    report = fetch.connection_report('https://nowhere.example/', timeout=1)
    assert report['tcp_ok'] is False
    # Whether an unroutable address times out or is refused depends on the
    # network this test runs on; both are "nothing to talk to".
    assert report['verdict'] in ('no_route', 'refused')


def test_a_refusal_is_told_apart_from_a_dead_address(monkeypatch):
    """The distinction that matters on Render: a refusal means the service is
    up and turning this server away - which is what a public OpenStreetMap
    server does to cloud hosting - while nothing at all means no route."""
    class Refusing(socket.socket):
        def connect(self, address):
            raise ConnectionRefusedError(111, 'Connection refused')

    monkeypatch.setattr(fetch, 'resolve',
                        lambda host, allow_private=False: ['203.0.113.7'])
    monkeypatch.setattr(fetch.socket, 'socket',
                        lambda *args, **kwargs: Refusing())

    report = fetch.connection_report('https://turned-away.example/', timeout=1)

    assert report['verdict'] == 'refused'
    assert 'turning this server away' in report['summary']


def test_the_check_stops_when_its_time_is_up(monkeypatch):
    """A page that takes longer than gunicorn's 30 seconds is a page that
    never answers, so the check gives up rather than overrunning."""
    import time as clock
    monkeypatch.setattr(fetch, 'resolve',
                        lambda host, allow_private=False: ['192.0.2.1', '192.0.2.2'])
    report = fetch.connection_report('https://nowhere.example/', timeout=1,
                                     deadline=clock.monotonic() - 1)
    assert report['addresses'] == []
    assert report['extra_addresses'] == 2


# ─────────────────────────────────────────────────────
# Trying every address is only safe if trying them is bounded
# ─────────────────────────────────────────────────────

def test_at_most_three_addresses_are_tried(monkeypatch):
    """A name with eight addresses must not mean eight timeouts."""
    seen = []
    monkeypatch.setattr(fetch, 'resolve', lambda host, allow_private=False:
                        ['1.1.1.1', '2.2.2.2', '3.3.3.3', '4.4.4.4'])
    monkeypatch.setattr(fetch, 'client_factory', only_answers_at(None, seen))

    page = fetch.get('https://many.example/', obey_robots=False)

    assert len(seen) == fetch.MAX_ADDRESS_ATTEMPTS
    assert not page.ok


def test_a_slow_address_cannot_spend_the_next_ones_time(monkeypatch):
    """gunicorn kills a request at 30 seconds. Three addresses that each
    take their full timeout would be well past that, so the budget stops
    the attempt before the next one starts."""
    import time as clock

    class Slow:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def request(self, method, url, **kwargs):
            clock.sleep(0.3)
            raise httpx.ConnectTimeout('timed out')

    monkeypatch.setattr(fetch, 'resolve', lambda host, allow_private=False:
                        ['1.1.1.1', '2.2.2.2', '3.3.3.3'])
    monkeypatch.setattr(fetch, 'client_factory', Slow)

    page = fetch.get('https://slow.example/', obey_robots=False, budget=0.4)

    assert 'ran out of time' in page.tried[-1][1]
    assert len(page.tried) < 4


def test_a_search_cannot_outlast_the_servers_request_limit(market):
    """Nominatim then Overpass, worst case, must still fit in the 30
    seconds gunicorn allows a request."""
    spent = []

    class Recorder:
        class Response:
            def __init__(self, payload):
                self.status_code = 200
                self._payload = payload
                self.text = json.dumps(payload)

            def json(self):
                return self._payload

        def get(self, url, **kwargs):
            spent.append(kwargs)
            return self.Response([{'boundingbox': ['43.6', '43.8', '7.1', '7.4']}])

        def post(self, url, **kwargs):
            spent.append(kwargs)
            return self.Response({'elements': []})

    discovery.OSMDiscovery(http=Recorder()).search(market, limit=10)

    assert spent, 'the provider made no calls'
    worst = sum(call.get('budget') or call.get('timeout') or 0 for call in spent)
    assert worst <= 27, f"a single search could take {worst} seconds"


# ─────────────────────────────────────────────────────
# One Overpass server refusing must not end the search
# ─────────────────────────────────────────────────────
#
# What Render actually hit: the main public Overpass server refuses
# connections from cloud hosting (it blocked whole AWS and Azure ranges in
# October 2025 after being abused from them). The engine must ask another
# server rather than stop - and must never pretend to be somewhere else.

NOMINATIM_ROWS = [{'boundingbox': ['43.6460', '43.7604', '7.1819', '7.3275']}]
OVERPASS_ROWS = {'elements': [{'type': 'node', 'id': 1, 'tags': {
    'office': 'estate_agent', 'name': 'Riviera Estates',
    'website': 'https://riviera-estates.fr'}}]}


class Servers:
    """Answers for some Overpass servers and refuses the rest."""

    class Response:
        def __init__(self, payload, status=200):
            self.status_code = status
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

    def __init__(self, answering=(), status=200):
        self.answering = set(answering)
        self.status = status
        self.asked = []

    def get(self, url, **kwargs):
        return self.Response(NOMINATIM_ROWS)

    def post(self, url, **kwargs):
        self.asked.append(url)
        if url in self.answering:
            return self.Response(OVERPASS_ROWS, self.status)
        raise fetch.Unreachable(url.split('/')[2], [
            ('162.55.144.139', 'ConnectError: [Errno 111] Connection refused'),
            ('2a01:4f8:261:3c4f::2', 'ConnectError: [Errno 101] Network is unreachable')])


def test_a_refused_server_moves_the_search_to_the_next_one(market):
    second = discovery.DEFAULT_OVERPASS_URLS[1]
    http = Servers(answering=[second])

    found = discovery.OSMDiscovery(http=http).search(market, limit=10)

    assert found.error == '', found.error
    assert [c.name for c in found.candidates] == ['Riviera Estates']
    assert len(http.asked) == 2, 'it gave up instead of trying the next server'
    assert 'answered by' in found.note


def test_the_server_that_worked_is_tried_first_next_time(market):
    """Otherwise every search pays for the dead server all over again."""
    second = discovery.DEFAULT_OVERPASS_URLS[1]
    discovery.OSMDiscovery(http=Servers(answering=[second])).search(market, limit=10)
    assert settings.get('osm_overpass_last_good') == second

    again = Servers(answering=[second])
    discovery.OSMDiscovery(http=again).search(market, limit=10)
    assert again.asked == [second]


def test_when_no_server_answers_the_error_names_each_one(market):
    http = Servers(answering=[])
    found = discovery.OSMDiscovery(http=http).search(market, limit=10)

    for url in discovery.DEFAULT_OVERPASS_URLS:
        assert url in found.error
    assert 'Connection refused' in found.error
    assert 'cloud hosting' in found.error      # what a refusal usually means
    assert 'Settings' in found.error           # and what to do about it


def test_the_servers_to_try_come_from_settings_in_order(market):
    settings.set('osm_overpass_url',
                 'https://first.example/api/interpreter , https://second.example/api/interpreter')
    http = Servers(answering=['https://second.example/api/interpreter'])

    discovery.OSMDiscovery(http=http).search(market, limit=10)

    assert http.asked == ['https://first.example/api/interpreter',
                          'https://second.example/api/interpreter']


def test_those_servers_can_actually_be_edited_on_the_settings_screen(admin):
    """The error message tells him to change this in Settings, so it has to
    be there. It was not - the two OpenStreetMap addresses existed as
    settings but were not on the screen."""
    keys = [key for key, _label in settings.EDITABLE]
    assert 'osm_overpass_url' in keys
    assert 'osm_nominatim_url' in keys

    page = admin.get('/owner/acquisition/settings').get_data(as_text=True)
    assert 'osm_overpass_url' in page


def test_the_search_stops_asking_servers_when_its_time_is_up(market):
    """Each server costs time, and gunicorn stops the request at 30 seconds."""
    provider = discovery.OSMDiscovery(http=Servers(answering=[]))
    response, url, problems = provider.ask_overpass('[out:json];', lambda: 1.0)

    assert response is None
    assert url == ''
    assert 'ran out of time' in problems[0][1]


def test_every_overpass_server_is_tested_by_the_connection_check(admin, monkeypatch):
    settings.set('osm_overpass_url',
                 'https://first.example/api/interpreter,https://second.example/api/interpreter')
    asked = []

    def report(url, **kwargs):
        asked.append(url)
        return {'url': url, 'host': '', 'ok': True, 'status': 200, 'error': '',
                'detail': '', 'ms': 1, 'addresses': [], 'extra_addresses': 0,
                'verdict': 'ok', 'summary': 'fine', 'tcp_ok': True}

    monkeypatch.setattr(fetch, 'connection_report', report)
    admin.post('/owner/acquisition/connection', data={'csrf_token': 'test-csrf-token'})

    assert 'https://first.example/api/interpreter' in asked
    assert 'https://second.example/api/interpreter' in asked
