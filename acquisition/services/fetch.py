"""The only code in the engine that opens a connection to another machine.

Everything else asks this module, for two reasons.

The first is safety. Fetching a URL that somebody else supplied is the
riskiest thing the engine does. A page found by a search, or a link inside a
page, can point anywhere - including back at our own server
("http://localhost:10000/delete-agency/3") or at the cloud's metadata
address, which hands out credentials to anything that asks. That attack is
called SSRF, and the defence is not a blocklist of words: it is resolving
the name to an address, refusing every address that is not a public one, and
then connecting to that exact address.

The second is reachability. A name usually has several addresses - some
IPv4, some IPv6 - and a server does not necessarily have a route to all of
them. A container with no IPv6 route that tries an IPv6 address gets
"Network is unreachable" instantly. So every address is tried, IPv4 first
because that is what hosting providers always have, and if none of them
answers the error says what each one said rather than only the last.

What this module promises:

  - http and https only, no file://, ftp://, gopher:// and friends;
  - the hostname is resolved first, and private, loopback, link-local,
    multicast, reserved and metadata addresses are refused - including
    169.254.169.254;
  - every address the name has is tried, IPv4 before IPv6;
  - the connection is made to the address that was checked, with the Host
    header set, so a name that answers twice with different addresses
    (DNS rebinding) cannot sneak through;
  - at most 3 redirects, each one checked again from scratch;
  - 10 second timeout, 2 MB cap, HTML only, no cookies, no JavaScript;
  - robots.txt is respected on strangers' websites, at most one request per
    second per site, and the User-Agent says who we are and how to ask us
    to stop;
  - a CAPTCHA or a bot wall is never worked around: the site is simply
    marked for a human to look at.

Services we chose ourselves - OpenStreetMap, OpenAI - go through request()
instead of get(). Same address handling, no robots check, because a
documented API we are a client of is not a stranger's website.
"""
import os
import socket
import ssl
import time
from dataclasses import dataclass, field
from ipaddress import ip_address
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .. import settings

def _public_base_url():
    """Where this engine lives, from the environment.

    Read here rather than taken from app.py: the package never imports the
    app. It is the same PUBLIC_BASE_URL the SaaS reads, so moving to a real
    domain moves the bot's calling card with it.
    """
    value = (os.getenv('PUBLIC_BASE_URL') or '').strip().rstrip('/')
    if value and not value.startswith(('http://', 'https://')):
        value = 'https://' + value
    return value or 'https://luxury-leads-ai.onrender.com'


USER_AGENT = (f'LuxuryLeadsAI/1.0 (+{_public_base_url()}/about-bot; '
              'research for a business introduction; email to stop)')
TIMEOUT_SECONDS = 10
MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
PER_HOST_DELAY = 1.0
# Trying every address is only safe if trying them is bounded. gunicorn
# gives a request 30 seconds; three addresses that each take their full
# timeout would spend more than that on one call, so a request gets a
# number of attempts and a total time, whichever runs out first.
MAX_ADDRESS_ATTEMPTS = 3
REQUEST_BUDGET_SECONDS = 20
ALLOWED_SCHEMES = ('http', 'https')
HTML_TYPES = ('text/html', 'application/xhtml+xml', 'text/plain')
DEFAULT_PORTS = {'http': 80, 'https': 443}

# Swapped in tests for a client that answers without a network.
client_factory = httpx.Client

_last_request_at = {}
_robots_cache = {}


@dataclass
class Page:
    """What came back, or why nothing did."""
    url: str = ''
    final_url: str = ''
    status: int = 0
    html: str = ''
    ok: bool = False
    error: str = ''
    blocked_by_robots: bool = False
    looks_like_bot_wall: bool = False
    ip: str = ''
    elapsed_ms: int = 0
    redirects: list = field(default_factory=list)
    tried: list = field(default_factory=list)   # [(address, what happened)]


class Blocked(Exception):
    """The address is one we refuse to connect to."""


class Unreachable(Exception):
    """Every address this name has was tried and none of them answered.

    The addresses and what each one said are kept, because "Network is
    unreachable" on its own hides whether one family worked and the other
    did not.
    """

    def __init__(self, host, tried):
        self.host = host
        self.tried = list(tried)
        super().__init__(describe_attempts(host, self.tried))


def describe_attempts(host, tried):
    if not tried:
        return f"could not reach {host}"
    parts = '; '.join(f"{address} said {problem}" for address, problem in tried)
    return f"could not reach {host} - {parts}"


def is_public_address(raw):
    """True only for an ordinary address on the public internet."""
    try:
        address = ip_address(raw)
    except ValueError:
        return False
    if (address.is_private or address.is_loopback or address.is_link_local
            or address.is_multicast or address.is_reserved
            or address.is_unspecified):
        return False
    # The cloud metadata address is link-local and already refused above;
    # named here so the reason is obvious to the next reader.
    if str(address) in ('169.254.169.254', 'fd00:ec2::254'):
        return False
    return True


def order_addresses(addresses):
    """IPv4 first, then IPv6, each sorted so a retry behaves the same way.

    Not a preference about the internet: a preference about hosting. Every
    provider gives a container an IPv4 route; plenty give it an IPv6
    address with no route behind it, and a name whose IPv6 address is tried
    first then fails in milliseconds with "Network is unreachable". Trying
    IPv4 first means that costs nothing, and IPv6 is still tried if IPv4
    does not answer.
    """
    def rank(raw):
        try:
            return (0 if ip_address(raw).version == 4 else 1, raw)
        except ValueError:
            return (2, raw)
    return sorted(addresses, key=rank)


def resolve(host, allow_private=False):
    """Every address this name answers with, in the order to try them.

    All of them, because a name that resolves to one public and one private
    address must not be reachable by retrying.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise Blocked(f"that address does not resolve ({e.strerror or e})")
    addresses = order_addresses({info[4][0] for info in infos})
    if not addresses:
        raise Blocked("that address does not resolve")
    if not allow_private:
        for address in addresses:
            if not is_public_address(address):
                raise Blocked(f"{host} points at a private address ({address})")
    return addresses


def check_url(url, allow_private=False):
    """(addresses, parsed) for a URL we are willing to open, or Blocked."""
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise Blocked(f"only http and https are allowed, not '{parsed.scheme or url[:12]}'")
    if not parsed.hostname:
        raise Blocked("no site name in that address")
    addresses = resolve(parsed.hostname, allow_private=allow_private)
    return addresses, parsed


def pinned_url(parsed, address):
    """The same request, addressed to the IP we just checked.

    The name is resolved, the address is checked, and then we connect to
    THAT address with the Host header set - so a name that answers with a
    public address when checked and a private one a moment later (DNS
    rebinding) cannot get through. For https the real hostname still goes
    into the handshake, so the certificate is checked properly.
    """
    host = f"[{address}]" if ':' in address else address
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return parsed._replace(netloc=netloc).geturl()


def _wait_turn(host):
    """One request per second per site, however busy we are."""
    last = _last_request_at.get(host)
    if last is not None:
        gap = time.monotonic() - last
        if gap < PER_HOST_DELAY:
            time.sleep(PER_HOST_DELAY - gap)
    _last_request_at[host] = time.monotonic()


def send(method, parsed, addresses, *, headers, timeout, params=None,
         data=None, json=None, budget=None):
    """One request, tried at each address until one answers.

    Returns (response, address, tried). Raises Unreachable with every
    address and its problem when none of them does. At most
    MAX_ADDRESS_ATTEMPTS addresses, and never past the budget: an address
    that hangs must not be able to spend the next one's time as well.
    """
    budget = REQUEST_BUDGET_SECONDS if budget is None else budget
    timeout = min(timeout, budget)
    started = time.monotonic()
    tried = []
    for index, address in enumerate(addresses[:MAX_ADDRESS_ATTEMPTS]):
        if index and (time.monotonic() - started) >= budget:
            tried.append((address, 'not tried - this request ran out of time'))
            break
        try:
            with client_factory(timeout=timeout, follow_redirects=False,
                                verify=ssl.create_default_context(),
                                headers=headers) as client:
                response = client.request(
                    method, pinned_url(parsed, address),
                    params=params, data=data, json=json,
                    extensions={'sni_hostname': parsed.hostname})
            return response, address, tried
        except httpx.HTTPError as e:
            tried.append((address, f"{type(e).__name__}: {e}"))
    raise Unreachable(parsed.hostname, tried)


def robots_allows(url, allow_private=False, timeout=5):
    """Does this site's robots.txt let us read this page?

    A site that does not answer, or answers with an error, counts as
    allowed - that is what robots.txt means. A site that says no is not
    argued with.
    """
    parsed = urlparse(url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    parser = _robots_cache.get(root)
    if parser is None:
        parser = RobotFileParser()
        parser.parse([])                         # default: allow
        try:
            addresses, robots_parsed = check_url(root + '/robots.txt',
                                                 allow_private=allow_private)
            _wait_turn(parsed.hostname)
            response, _address, _tried = send(
                'GET', robots_parsed, addresses, timeout=timeout,
                headers={'User-Agent': USER_AGENT, 'Host': parsed.netloc})
            if response.status_code == 200 and len(response.content) < 512 * 1024:
                parser.parse(response.text.splitlines())
        except (Blocked, Unreachable, httpx.HTTPError, UnicodeDecodeError, ValueError):
            pass
        _robots_cache[root] = parser
    try:
        return parser.can_fetch(USER_AGENT, url)
    except Exception:                            # noqa: BLE001 - a broken file
        return True                              # is not a refusal


BOT_WALL_SIGNS = ('captcha', 'are you a robot', 'verify you are human',
                  'cf-browser-verification', 'access denied', 'request blocked',
                  'enable javascript and cookies')


def looks_like_bot_wall(status, html):
    if status in (403, 429):
        return True
    lowered = (html or '')[:4000].lower()
    return any(sign in lowered for sign in BOT_WALL_SIGNS)


def get(url, allow_private=False, timeout=TIMEOUT_SECONDS, obey_robots=True,
        budget=None):
    """Fetch one page from a stranger's website. Never raises: the Page
    says what happened."""
    budget = (TIMEOUT_SECONDS + 5) if budget is None else budget
    started = time.monotonic()
    page = Page(url=url, final_url=url)
    current = url
    seen = []

    for hop in range(MAX_REDIRECTS + 1):
        try:
            addresses, parsed = check_url(current, allow_private=allow_private)
        except Blocked as e:
            page.error = str(e)
            page.elapsed_ms = int((time.monotonic() - started) * 1000)
            return page

        if obey_robots and not robots_allows(current, allow_private=allow_private):
            page.error = "that site's robots.txt asks us not to read this page"
            page.blocked_by_robots = True
            page.final_url = current
            page.elapsed_ms = int((time.monotonic() - started) * 1000)
            return page

        _wait_turn(parsed.hostname)
        try:
            response, address, tried = send(
                'GET', parsed, addresses, timeout=timeout,
                budget=budget - (time.monotonic() - started),
                headers={'User-Agent': USER_AGENT,
                         'Accept': 'text/html,*/*;q=0.1',
                         'Accept-Language': 'en,fr;q=0.8',
                         'Host': parsed.netloc})
        except Unreachable as e:
            page.error = str(e)
            page.tried = e.tried
            page.final_url = current
            page.elapsed_ms = int((time.monotonic() - started) * 1000)
            return page

        page.ip = address
        page.tried = tried
        page.status = response.status_code
        page.final_url = current

        if response.is_redirect:
            location = response.headers.get('location', '')
            if not location:
                break
            if hop >= MAX_REDIRECTS:
                page.error = f"too many redirects (more than {MAX_REDIRECTS})"
                page.elapsed_ms = int((time.monotonic() - started) * 1000)
                return page
            seen.append(current)
            current = urljoin(current, location)
            page.redirects = list(seen)
            continue
        break

    content_type = (response.headers.get('content-type') or '').split(';')[0].strip().lower()
    body = response.content[:MAX_BYTES]
    try:
        html = body.decode(response.encoding or 'utf-8', errors='replace')
    except (LookupError, UnicodeDecodeError):
        html = body.decode('utf-8', errors='replace')

    page.html = html if content_type in HTML_TYPES or not content_type else ''
    page.looks_like_bot_wall = looks_like_bot_wall(page.status, page.html)
    page.ok = (200 <= page.status < 300) and not page.looks_like_bot_wall
    if not page.ok and not page.error:
        if page.looks_like_bot_wall:
            page.error = ("that site asks for a human check - marked for you "
                          "to look at rather than worked around")
        else:
            page.error = f"the site answered {page.status}"
    page.elapsed_ms = int((time.monotonic() - started) * 1000)
    return page


# ─────────────────────────────────────────────────────
# Services we chose ourselves
# ─────────────────────────────────────────────────────

API_REDIRECTS = 2


def request(method, url, *, params=None, data=None, json=None, headers=None,
            timeout=15, allow_private=False, budget=None):
    """One request to a service we picked - OpenStreetMap, OpenAI.

    Same address handling as get(), so a server without an IPv6 route still
    reaches an IPv6-advertising API over IPv4. No robots.txt: this is a
    documented interface we are a client of, not a stranger's website.

    Returns the response. Raises Unreachable, naming every address tried,
    when nothing answers - the point being that the message says whether
    IPv4 worked and IPv6 did not, or neither did.
    """
    budget = REQUEST_BUDGET_SECONDS if budget is None else budget
    started = time.monotonic()
    current = url
    body_headers = {'User-Agent': USER_AGENT}
    body_headers.update(headers or {})

    for hop in range(API_REDIRECTS + 1):
        addresses, parsed = check_url(current, allow_private=allow_private)
        _wait_turn(parsed.hostname)
        sent = dict(body_headers, Host=parsed.netloc)
        response, _address, _tried = send(method, parsed, addresses,
                                          headers=sent, timeout=timeout,
                                          budget=budget - (time.monotonic() - started),
                                          params=params, data=data, json=json)
        if not response.is_redirect or hop >= API_REDIRECTS:
            return response
        location = response.headers.get('location', '')
        if not location:
            return response
        current = urljoin(current, location)
        if response.status_code in (301, 302, 303):
            method, data, json, params = 'GET', None, None, None
    return response


class Http:
    """What the discovery providers hold instead of httpx.

    Same two calls with the same arguments, so a provider can still be
    handed a fake in a test, and so swapping it in changed one line.
    """

    def get(self, url, timeout=15, params=None, headers=None,
            allow_private=False, budget=None):
        return request('GET', url, params=params, headers=headers,
                       timeout=timeout, allow_private=allow_private,
                       budget=budget)

    def post(self, url, timeout=15, data=None, json=None, headers=None,
             allow_private=False, budget=None):
        return request('POST', url, data=data, json=json, headers=headers,
                       timeout=timeout, allow_private=allow_private,
                       budget=budget)


http = Http()


def connection_report(url, timeout=3, max_addresses=4, allow_private=False,
                      deadline=None):
    """Can this server reach that service, and over which addresses?

    Written for the Connection check screen, and deliberately in three
    stages, because the three things that produce the same sentence in a
    job's error are different problems with different fixes:

      the name does not resolve        -> DNS, or a typo in Settings
      no address accepts a connection  -> this server has no route there
      an address accepts, the request
      does not get through             -> something in between is refusing

    Only the first address that answered is used for the real request: the
    point is a quick, bounded answer, not a full survey.
    """
    parsed = urlparse(url)
    report = {'url': url, 'host': parsed.hostname or '', 'addresses': [],
              'ok': False, 'error': '', 'status': 0, 'ms': 0,
              'extra_addresses': 0, 'tcp_ok': False,
              'verdict': 'no_route', 'summary': ''}
    if not report['host']:
        report['verdict'] = 'dns'
        report['error'] = 'that is not a web address'
        report['summary'] = report['error']
        return report

    try:
        addresses = resolve(report['host'], allow_private=allow_private)
    except Blocked as e:
        report['verdict'] = 'dns'
        report['error'] = str(e)
        report['summary'] = f"The name {report['host']} could not be looked up."
        return report

    port = parsed.port or DEFAULT_PORTS.get(parsed.scheme, 443)
    chosen = addresses[:max_addresses]
    report['extra_addresses'] = len(addresses) - len(chosen)

    for address in chosen:
        if deadline is not None and time.monotonic() >= deadline:
            report['extra_addresses'] += 1
            continue
        row = {'address': address, 'family': 'IPv6' if ':' in address else 'IPv4',
               'ok': False, 'detail': '', 'ms': 0}
        started = time.monotonic()
        sock = None
        try:
            family = socket.AF_INET6 if ':' in address else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect((address, port))
            row['ok'] = True
            row['detail'] = 'answered'
            report['tcp_ok'] = True
        except OSError as e:
            row['detail'] = f"{type(e).__name__}: {e}"
        finally:
            if sock is not None:
                sock.close()
        row['ms'] = int((time.monotonic() - started) * 1000)
        report['addresses'].append(row)

    working = [row['address'] for row in report['addresses'] if row['ok']]
    if not working:
        refused = [row for row in report['addresses'] if 'refused' in row['detail'].lower()]
        if refused:
            # A refusal is an answer: something sent back "no" immediately,
            # rather than the packet going nowhere. That is what a service
            # that turns away whole ranges of cloud hosting looks like.
            report['verdict'] = 'refused'
            report['summary'] = (f"{report['host']} refused the connection. The "
                                 f"service is up and is turning this server away, "
                                 f"which is what blocking a hosting provider's "
                                 f"addresses looks like.")
        else:
            report['verdict'] = 'no_route'
            report['summary'] = (f"no address of {report['host']} accepted a "
                                 f"connection from this server")
        report['error'] = report['summary']
        return report

    started = time.monotonic()
    try:
        _wait_turn(parsed.hostname)
        response, _address, _tried = send(
            'GET', parsed, working[:1], timeout=timeout,
            headers={'User-Agent': USER_AGENT, 'Host': parsed.netloc})
        report['ok'] = True
        report['verdict'] = 'ok'
        report['status'] = response.status_code
        report['summary'] = (f"{working[0]} answered {response.status_code}, "
                             f"so the engine can use this service.")
    except (Blocked, Unreachable, httpx.HTTPError) as e:
        report['verdict'] = 'blocked'
        report['error'] = str(e) if isinstance(e, (Blocked, Unreachable)) else \
            f"{type(e).__name__}: {e}"
        report['summary'] = (f"{working[0]} accepts a connection but the request "
                             f"did not get through - something between this "
                             f"server and {report['host']} is refusing it.")
    report['ms'] = int((time.monotonic() - started) * 1000)
    return report


def page_title(html):
    """The <title>, tidied - enough to name a prospect sensibly."""
    import re
    match = re.search(r'<title[^>]*>(.*?)</title>', html or '', re.S | re.I)
    if not match:
        return ''
    text = re.sub(r'\s+', ' ', match.group(1)).strip()
    # "Riviera Estates | Luxury homes in Nice" -> "Riviera Estates": the bit
    # before the separator is the firm's name, the rest is its tagline.
    for separator in (' | ', ' - ', ' – ', ' — ', ' :: ', ' · '):
        if separator in text:
            head = text.split(separator)[0].strip()
            if len(head) >= 3:
                text = head
                break
    return text[:150]


def site_answers(domain, allow_private=False, budget=18):
    """Does this company's website exist at all?

    Discovery by AI search can invent a plausible name and a plausible
    address for an agency that does not exist. One cheap request settles it
    before the domain becomes a row somebody might email.

    Two schemes, several addresses each, all inside one budget: a site that
    hangs is a site we move on from, not one that spends the whole request.
    """
    started = time.monotonic()
    page = Page(url=f"https://{domain}/", error='nothing was tried')
    for scheme in ('https', 'http'):
        left = budget - (time.monotonic() - started)
        if scheme == 'http' and left <= 2:
            break
        page = get(f"{scheme}://{domain}/", allow_private=allow_private,
                   budget=max(3.0, left))
        if page.ok or page.blocked_by_robots or page.looks_like_bot_wall:
            return page
        if page.status and page.status < 500:
            return page
    return page


def reset_caches():
    """Tests, and a long-running worker that should not remember forever."""
    _last_request_at.clear()
    _robots_cache.clear()


def settings_contact_line():
    """The email a site owner can write to, if one is set."""
    return settings.get('operator_email') or ''
