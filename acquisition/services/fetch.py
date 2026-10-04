"""The only code in the engine that opens a stranger's website.

Everything else asks this module, because fetching a URL that somebody else
supplied is the riskiest thing the engine does. A page found by a search, or
a link inside a page, can point anywhere - including back at our own server
("http://localhost:10000/delete-agency/3") or at the cloud's metadata
address, which hands out credentials to anything that asks. That attack is
called SSRF, and the defence is not a blocklist of words: it is resolving
the name to an address, refusing every address that is not a public one, and
then connecting to that exact address.

What this module promises:

  - http and https only, no file://, ftp://, gopher:// and friends;
  - the hostname is resolved first, and private, loopback, link-local,
    multicast, reserved and metadata addresses are refused - including
    169.254.169.254;
  - the connection is made to the address that was checked, with the Host
    header set, so a name that answers twice with different addresses
    (DNS rebinding) cannot sneak through;
  - at most 3 redirects, each one checked again from scratch;
  - 10 second timeout, 2 MB cap, HTML only, no cookies, no JavaScript;
  - robots.txt is respected, at most one request per second per site, and
    the User-Agent says who we are and how to ask us to stop;
  - a CAPTCHA or a bot wall is never worked around: the site is simply
    marked for a human to look at.
"""
import socket
import ssl
import time
from dataclasses import dataclass, field
from ipaddress import ip_address
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .. import settings

USER_AGENT = ('LuxuryLeadsAI/1.0 (+https://luxury-leads-ai.onrender.com/about-bot; '
              'research for a business introduction; email to stop)')
TIMEOUT_SECONDS = 10
MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
PER_HOST_DELAY = 1.0
ALLOWED_SCHEMES = ('http', 'https')
HTML_TYPES = ('text/html', 'application/xhtml+xml', 'text/plain')

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


class Blocked(Exception):
    """The address is one we refuse to connect to."""


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


def resolve(host, allow_private=False):
    """Every address this name answers with, if they are all public.

    All of them, because a name that resolves to one public and one private
    address must not be reachable by retrying.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise Blocked(f"that address does not resolve ({e.strerror or e})")
    addresses = sorted({info[4][0] for info in infos})
    if not addresses:
        raise Blocked("that address does not resolve")
    if not allow_private:
        for address in addresses:
            if not is_public_address(address):
                raise Blocked(f"{host} points at a private address ({address})")
    return addresses


def check_url(url, allow_private=False):
    """(address, parsed) for a URL we are willing to open, or Blocked."""
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise Blocked(f"only http and https are allowed, not '{parsed.scheme or url[:12]}'")
    if not parsed.hostname:
        raise Blocked("no site name in that address")
    addresses = resolve(parsed.hostname, allow_private=allow_private)
    return addresses[0], parsed


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
            address, robots_parsed = check_url(root + '/robots.txt',
                                               allow_private=allow_private)
            _wait_turn(parsed.hostname)
            with httpx.Client(timeout=timeout, follow_redirects=False,
                              verify=ssl.create_default_context(),
                              headers={'User-Agent': USER_AGENT,
                                       'Host': parsed.netloc}) as client:
                response = client.get(pinned_url(robots_parsed, address),
                                      extensions={'sni_hostname': parsed.hostname})
            if response.status_code == 200 and len(response.content) < 512 * 1024:
                parser.parse(response.text.splitlines())
        except (Blocked, httpx.HTTPError, UnicodeDecodeError, ValueError):
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


def get(url, allow_private=False, timeout=TIMEOUT_SECONDS, obey_robots=True):
    """Fetch one page. Never raises: the Page says what happened."""
    started = time.monotonic()
    page = Page(url=url, final_url=url)
    current = url
    seen = []

    for hop in range(MAX_REDIRECTS + 1):
        try:
            address, parsed = check_url(current, allow_private=allow_private)
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
            with httpx.Client(timeout=timeout, follow_redirects=False,
                              verify=ssl.create_default_context(),
                              headers={'User-Agent': USER_AGENT,
                                       'Accept': 'text/html,*/*;q=0.1',
                                       'Accept-Language': 'en,fr;q=0.8',
                                       'Host': parsed.netloc}) as client:
                response = client.get(pinned_url(parsed, address),
                                      extensions={'sni_hostname': parsed.hostname})
        except httpx.HTTPError as e:
            page.error = f"{type(e).__name__}: {e}"
            page.ip = address
            page.final_url = current
            page.elapsed_ms = int((time.monotonic() - started) * 1000)
            return page

        page.ip = address
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


def site_answers(domain, allow_private=False):
    """Does this company's website exist at all?

    Discovery by AI search can invent a plausible name and a plausible
    address for an agency that does not exist. One cheap request settles it
    before the domain becomes a row somebody might email.
    """
    for scheme in ('https', 'http'):
        page = get(f"{scheme}://{domain}/", allow_private=allow_private)
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
