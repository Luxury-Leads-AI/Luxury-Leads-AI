"""Where candidate agencies come from.

Three sources today, one shape. A provider is handed a market and a number,
and gives back candidates: name, website, phone, address, and where it heard
it. Nothing here decides anything - it collects, and the discover job does
the checking, the de-duplicating and the saving.

    manual   you paste websites (Phase 1; kept here so the list is honest)
    openai   an AI web search, about $0.011 a search
    osm      OpenStreetMap, free

Google Places slots in beside them later without anything else changing.
That is the point of a plug: a new source is a new class in this file.
"""
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .. import settings
from ..services import ai, fetch

OPENAI_RESPONSES_URL = 'https://api.openai.com/v1/responses'
# The public OpenStreetMap services, which are free and volunteer-run. If we
# ever query them heavily, their fair-use policy asks us to run our own
# copy - which is then a setting, not a deploy.
DEFAULT_NOMINATIM_URL = 'https://nominatim.openstreetmap.org/search'
# More than one, in order, because the main public Overpass server refuses
# connections from cloud hosting: it was being abused from there and blocked
# whole AWS and Azure ranges in October 2025. Render is cloud hosting, so it
# gets "Connection refused" from it. The answer is to ask a server that
# accepts us, say who we are, and keep the volume tiny - never to disguise
# where the request comes from.
DEFAULT_OVERPASS_URLS = (
    'https://overpass.private.coffee/api/interpreter',
    'https://overpass-api.de/api/interpreter',
)
DEFAULT_OVERPASS_URL = DEFAULT_OVERPASS_URLS[0]
# Nominatim understands a handful of plain-English "special phrases" for
# kinds of place. This one maps to office=estate_agent, the same objects
# Overpass is asked for.
DEFAULT_POI_QUERY = 'estate agent'
# What separates "that server said no to us" from "that server was busy".
TEMPORARY_SIGNS = ('timeout', 'timed out', 'temporarily', 'too many requests',
                   '429', 'connectionreset', 'remoteprotocolerror')
CONTACT = fetch._public_base_url()


@dataclass
class Candidate:
    name: str = ''
    website: str = ''
    phone: str = ''
    address: str = ''
    source_ref: str = ''

    def as_dict(self):
        return {'name': self.name, 'website': self.website, 'phone': self.phone,
                'address': self.address, 'source_ref': self.source_ref}


@dataclass
class Found:
    """What one run of one source produced."""
    source: str
    candidates: list = field(default_factory=list)
    searched: int = 0          # billable searches made
    usd: float = 0.0
    error: str = ''
    note: str = ''
    # True when the error was "busy", not "no". The job is then thrown back
    # into the queue to be tried again later instead of being written off.
    retryable: bool = False


def user_agent():
    contact = settings.get('operator_email') or CONTACT
    return f"LuxuryLeadsAI/1.0 (+{CONTACT}; contact: {contact})"


OSM_HINT = ("Open Jobs -> Check the connection to see which addresses this "
            "server can reach. You can also point the engine at a different "
            "OpenStreetMap server in Settings.")


CLOUD_BLOCK_HINT = (
    "Connection refused from every address usually means that server turns "
    "away cloud hosting - the main OpenStreetMap server blocked whole AWS and "
    "Azure ranges in October 2025 after being abused from them, and Render is "
    "cloud hosting. Settings -> Overpass servers to try takes a list, or use "
    "AI web search for this city instead.")


# How long to leave Nominatim's search alone after it says "too many
# requests". Its usage policy is one request a second and no bulk use; this
# is us taking that seriously rather than hammering through a 429.
POI_PAUSE_MINUTES = 60


def pause_poi_search(minutes=POI_PAUSE_MINUTES):
    until = datetime.utcnow() + timedelta(minutes=minutes)
    settings.set('osm_poi_pause_until', until.isoformat(timespec='seconds'))
    return until


def poi_search_paused():
    raw = (settings.get('osm_poi_pause_until') or '').strip()
    if not raw:
        return False
    try:
        return datetime.fromisoformat(raw) > datetime.utcnow()
    except ValueError:
        return False


def is_temporary(text):
    """Would asking again in five minutes plausibly work?

    A refusal would not: that server turns this machine away and will do so
    again. A timeout might: the server was busy, or the query was slow.
    """
    lowered = (text or '').lower()
    if 'refused' in lowered or 'unreachable' in lowered:
        return False
    return any(sign in lowered for sign in TEMPORARY_SIGNS)


def short_problem(e):
    """What went wrong, with every address, in one line."""
    if isinstance(e, fetch.Unreachable):
        return '; '.join(f"{address} said {problem}" for address, problem in e.tried)
    if isinstance(e, fetch.Blocked):
        return str(e)
    return f"{type(e).__name__}: {e}"


def connection_problem(e, service, hint=''):
    """A failed connection, in words that say what actually happened.

    httpx reports only the last address it tried, so "Network is
    unreachable" on its own cannot tell you whether one address family
    worked and the other did not. fetch keeps all of them; this puts them
    in the message.
    """
    if isinstance(e, fetch.Unreachable):
        text = f"this server could not reach {service} at {e.host} - {short_problem(e)}"
    elif isinstance(e, fetch.Blocked):
        text = f"this server will not open {service}: {e}"
    else:
        text = f"{type(e).__name__}: {e}"
    return f"{text}. {hint}".strip() if hint else text


def parse_json_loosely(text):
    """Models like to wrap JSON in prose or code fences. Dig it out."""
    if not text:
        return None
    text = text.strip()
    fence = re.search(r'```(?:json)?\s*(.+?)```', text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find('{'), text.rfind('}')
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            return None
    return None


# ─────────────────────────────────────────────────────────────
# Manual
# ─────────────────────────────────────────────────────────────

class ManualDiscovery:
    """You paste the websites. Free, and still the most accurate source."""
    name = 'manual'
    label = 'Manual (you paste websites)'
    costs_money = False

    def search(self, market, limit=20, **kwargs):
        return Found(source=self.name, note="Paste websites on the Prospects page.")


# ─────────────────────────────────────────────────────────────
# OpenAI web search
# ─────────────────────────────────────────────────────────────

class OpenAIWebSearchDiscovery:
    """Ask a model with web search for agencies in one city.

    Called over plain HTTP rather than through the installed `openai`
    package: the pinned version (1.54.4) predates this endpoint, and
    upgrading it would touch the live chatbot. The request goes through
    fetch, so it gets the same address handling as everything else.

    The model may still invent an agency, so every website it returns is
    checked by the discover job before it becomes a prospect.
    """
    name = 'openai'
    label = 'AI web search (about $0.011 a search)'
    costs_money = True

    def __init__(self, api_key=None, http=None):
        import os
        # None means "take it from the environment"; '' means "there isn't one".
        self.api_key = os.getenv('OPENAI_API_KEY', '') if api_key is None else api_key
        self.http = http or fetch.http

    def prompt(self, market, limit):
        focus = 'luxury or high-end ' if market.luxury_focus else ''
        return (
            f"Find up to {limit} {focus}{market.target_type}s that have their own "
            f"website and an office in {market.city}, {market.country}.\n\n"
            "Rules:\n"
            "- Only firms you can see on the web right now.\n"
            "- The website must be the firm's own domain, not Facebook, "
            "Instagram, LinkedIn or a property portal.\n"
            "- Do not guess an address: leave a field out if you did not see it.\n\n"
            'Answer with JSON only, in this shape:\n'
            '{"agencies": [{"name": "...", "website": "https://...", '
            '"phone": "...", "address": "..."}]}'
        )

    def _post(self, payload, timeout=25):
        return self.http.post(
            OPENAI_RESPONSES_URL, timeout=timeout,
            headers={'Authorization': f'Bearer {self.api_key}',
                     'Content-Type': 'application/json'},
            json=payload)

    def _extract_text(self, data):
        """The answer text, wherever this version of the API puts it."""
        if not isinstance(data, dict):
            return ''
        if isinstance(data.get('output_text'), str):
            return data['output_text']
        chunks = []
        for item in data.get('output') or []:
            if not isinstance(item, dict):
                continue
            for part in item.get('content') or []:
                if isinstance(part, dict) and part.get('type') in ('output_text', 'text'):
                    chunks.append(part.get('text') or '')
        return '\n'.join(chunk for chunk in chunks if chunk)

    def search(self, market, limit=10, **kwargs):
        if not self.api_key:
            return Found(source=self.name, error="No OpenAI key configured")

        per_search = settings.get_float('web_search_usd', 0.01)
        allowed, why = ai.can_spend(per_search + 0.002)
        if not allowed:
            return Found(source=self.name, error=why)

        model = settings.get('web_search_model') or 'gpt-4o-mini'
        wanted = settings.get('web_search_tool_type') or 'auto'
        attempts = ([wanted] if wanted in ('web_search', 'web_search_preview')
                    else ['web_search', 'web_search_preview'])

        last_error = ''
        for tool_type in attempts:
            payload = {'model': model,
                       'tools': [{'type': tool_type}],
                       'input': self.prompt(market, limit)}
            try:
                response = self._post(payload)
            except Exception as e:                      # noqa: BLE001
                return Found(source=self.name,
                             error=connection_problem(e, 'OpenAI'))

            if response.status_code == 400 and tool_type != attempts[-1]:
                last_error = (response.text or '')[:300]
                continue                                 # try the other spelling
            if response.status_code >= 400:
                return Found(source=self.name,
                             error=f"OpenAI answered {response.status_code}: "
                                   f"{(response.text or '')[:300]}")

            try:
                data = response.json()
            except ValueError:
                return Found(source=self.name, error="OpenAI sent something that is not JSON")

            # This spelling worked; remember it so the next run goes straight there.
            if wanted == 'auto':
                settings.set('web_search_tool_type', tool_type)

            usage = data.get('usage') or {}
            tokens_in = int(usage.get('input_tokens') or usage.get('prompt_tokens') or 0)
            tokens_out = int(usage.get('output_tokens') or usage.get('completion_tokens') or 0)
            token_cost = ai.estimate_usd(model, tokens_in, tokens_out)
            ai.record_cost(model, 'discovery_search', input_tokens=tokens_in,
                           output_tokens=tokens_out, units=1,
                           usd=token_cost + per_search,
                           job_id=kwargs.get('job_id'))

            parsed = parse_json_loosely(self._extract_text(data)) or {}
            rows = parsed.get('agencies') or parsed.get('results') or []
            candidates = []
            for row in rows[:limit]:
                if not isinstance(row, dict):
                    continue
                website = (row.get('website') or row.get('url') or '').strip()
                if not website:
                    continue
                candidates.append(Candidate(
                    name=(row.get('name') or '').strip()[:200],
                    website=website[:300],
                    phone=(row.get('phone') or '').strip()[:60],
                    address=(row.get('address') or '').strip()[:300],
                    source_ref=f"openai:{model}:{market.city}"))
            return Found(source=self.name, candidates=candidates, searched=1,
                         usd=token_cost + per_search,
                         note='' if candidates else "The search came back with nothing usable")

        return Found(source=self.name,
                     error=f"OpenAI refused both tool names: {last_error}")


# ─────────────────────────────────────────────────────────────
# OpenStreetMap
# ─────────────────────────────────────────────────────────────

class OSMDiscovery:
    """Estate agents mapped in OpenStreetMap, free.

    Two steps: ask Nominatim where the city is (a box of coordinates), then
    ask Overpass for estate agents inside that box. The box is remembered,
    so a city is looked up once.

    Both services are volunteer-run and ask for the same two things: say who
    you are, and do not hammer them. We do both. The data is ODbL-licensed;
    we keep facts about businesses, which is what it is for.
    """
    name = 'osm'
    label = 'OpenStreetMap (free)'
    costs_money = False

    AGENT_TAGS = (('office', 'estate_agent'), ('shop', 'estate_agent'))

    def __init__(self, http=None):
        self.http = http or fetch.http

    @property
    def nominatim_url(self):
        return settings.get('osm_nominatim_url') or DEFAULT_NOMINATIM_URL

    @property
    def overpass_urls(self):
        """Every Overpass server to try, in the order to try them.

        A list rather than one address, because a public Overpass server
        can refuse this server outright (CLOUD_BLOCK_HINT). The one that
        answered last time goes first, so a working setup does not pay for
        a dead one on every search.
        """
        configured = settings.get('osm_overpass_url') or ''
        urls = [part.strip() for part in configured.split(',') if part.strip()]
        urls = urls or list(DEFAULT_OVERPASS_URLS)
        last_good = (settings.get('osm_overpass_last_good') or '').strip()
        if last_good in urls:
            urls = [last_good] + [url for url in urls if url != last_good]
        return urls

    @property
    def overpass_url(self):
        """The first server to try, for anything that wants a single name."""
        return self.overpass_urls[0]

    def bbox_for(self, market, timeout=10):
        """(south, north, west, east) for a market, asked once and kept."""
        key = f"osm_bbox:{market.id}"
        cached = settings.get(key, '')
        if cached:
            try:
                south, north, west, east = (float(part) for part in cached.split(','))
                return south, north, west, east
            except ValueError:
                pass
        response = self.http.get(
            self.nominatim_url, timeout=timeout, budget=timeout + 2,
            params={'city': market.city, 'country': market.country,
                    'format': 'json', 'limit': 1},
            headers={'User-Agent': user_agent()})
        if response.status_code >= 400:
            raise RuntimeError(f"Nominatim answered {response.status_code}")
        rows = response.json()
        if not rows:
            raise RuntimeError(f"OpenStreetMap does not know a city called "
                               f"{market.city}, {market.country}")
        south, north, west, east = (float(value) for value in rows[0]['boundingbox'])
        settings.set(key, f"{south},{north},{west},{east}")
        return south, north, west, east

    def query_for(self, bbox, limit, seconds=25):
        south, north, west, east = bbox
        box = f"{south},{west},{north},{east}"
        parts = ''.join(f'nwr["{key}"="{value}"]({box});' for key, value in self.AGENT_TAGS)
        return f"[out:json][timeout:{seconds}];({parts});out center tags {limit};"

    def overpass_query(self, bbox, limit, seconds):
        """The query, told to give up when we do.

        The server-side timeout matches ours on purpose: a volunteer server
        should not keep grinding on a query nobody is waiting for any more.
        """
        return self.query_for(bbox, limit, seconds=seconds)

    def ask_overpass(self, bbox, limit, seconds_left):
        """Ask each Overpass server in turn until one answers.

        Returns (response, the url that answered, problems). Each problem is
        (url, what happened, was it temporary). The difference matters: a
        refusal is that server saying no to this machine and will say no
        again in five minutes, while a timeout means it was busy and the
        search is worth repeating.

        Each server gets a short turn rather than the whole budget, because
        a slow first server used to leave no time for the second - which is
        exactly what happened on the first run from Render.
        """
        problems = []
        for url in self.overpass_urls:
            spare = seconds_left()
            if spare < 6:
                problems.append((url, 'not tried - the search ran out of time', True))
                break
            timeout = int(max(5, min(self.PER_SERVER_SECONDS, spare - 6)))
            try:
                response = self.http.post(
                    url, timeout=timeout, budget=timeout,
                    data={'data': self.overpass_query(bbox, limit, timeout)},
                    headers={'User-Agent': user_agent()})
            except (fetch.Unreachable, fetch.Blocked) as e:
                problems.append((url, short_problem(e), is_temporary(short_problem(e))))
                continue
            except Exception as e:                       # noqa: BLE001
                text = f"{type(e).__name__}: {e}"
                problems.append((url, text, is_temporary(text)))
                continue
            if response.status_code == 429:
                problems.append((url, 'answered 429 (too many requests)', True))
                continue
            if response.status_code >= 500:
                problems.append((url, f"answered {response.status_code}", True))
                continue
            return response, url, problems
        return None, '', problems

    def candidates_from(self, elements):
        """Overpass elements -> candidates, and how many had no website."""
        candidates, without_site = [], 0
        for element in elements:
            tags = element.get('tags') or {}
            website = (tags.get('website') or tags.get('contact:website')
                       or tags.get('url') or '').strip()
            if not website:
                without_site += 1
                continue
            address = ' '.join(part for part in (
                tags.get('addr:housenumber'), tags.get('addr:street'),
                tags.get('addr:postcode'), tags.get('addr:city')) if part)
            candidates.append(Candidate(
                name=(tags.get('name') or '').strip()[:200],
                website=website[:300],
                phone=(tags.get('phone') or tags.get('contact:phone') or '').strip()[:60],
                address=address[:300],
                source_ref=f"osm:{element.get('type')}/{element.get('id')}"))
        return candidates, without_site

    def nominatim_candidates(self, bbox, limit, timeout):
        """The same agencies, asked of Nominatim instead of Overpass.

        Nominatim is a place search rather than a database query, so it
        returns fewer and caps at 40. It is here because it is reachable
        from places Overpass is not - the first searches from Render proved
        Nominatim answers and Overpass refuses - and something is better
        than a city that cannot be worked at all. Its usage policy asks for
        one request a second and a real User-Agent: the fetcher does the
        first, user_agent() the second.
        """
        south, north, west, east = bbox
        response = self.http.get(
            self.nominatim_url, timeout=timeout, budget=timeout + 2,
            params={'q': settings.get('osm_poi_query') or DEFAULT_POI_QUERY,
                    'format': 'jsonv2', 'limit': max(1, min(40, limit)),
                    'extratags': 1, 'addressdetails': 1, 'bounded': 1,
                    'viewbox': f"{west},{north},{east},{south}"},
            headers={'User-Agent': user_agent()})
        if response.status_code == 429:
            # It asked us to stop. Stopping means not asking again in thirty
            # seconds - a free service that keeps being pushed stops
            # answering at all, and it would be right to.
            pause_poi_search(POI_PAUSE_MINUTES)
            raise RuntimeError(f"Nominatim answered 429 (too many requests), so "
                               f"its search is left alone for "
                               f"{POI_PAUSE_MINUTES} minutes")
        if response.status_code >= 400:
            raise RuntimeError(f"Nominatim answered {response.status_code}")
        rows = response.json() or []
        candidates, without_site = [], 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            extra = row.get('extratags') or {}
            website = (extra.get('website') or extra.get('contact:website')
                       or extra.get('url') or '').strip()
            if not website:
                without_site += 1
                continue
            name = (row.get('name') or '').strip()
            if not name:
                name = (row.get('display_name') or '').split(',')[0].strip()
            candidates.append(Candidate(
                name=name[:200],
                website=website[:300],
                phone=(extra.get('phone') or extra.get('contact:phone') or '').strip()[:60],
                address=(row.get('display_name') or '')[:300],
                source_ref=f"osm:{row.get('osm_type')}/{row.get('osm_id')}"))
        return candidates, without_site

    SEARCH_BUDGET_SECONDS = 20
    PER_SERVER_SECONDS = 8

    def search(self, market, limit=40, **kwargs):
        started = time.monotonic()

        def seconds_left():
            return self.SEARCH_BUDGET_SECONDS - (time.monotonic() - started)

        try:
            bbox = self.bbox_for(market,
                                 timeout=max(4, min(10, int(seconds_left()))))
        except (fetch.Unreachable, fetch.Blocked) as e:
            # Busy is worth repeating; refused is not.
            return Found(source=self.name,
                         retryable=is_temporary(short_problem(e)),
                         error=connection_problem(e, 'Nominatim (OpenStreetMap)',
                                                  OSM_HINT))
        except Exception as e:                           # noqa: BLE001
            return Found(source=self.name, error=f"{type(e).__name__}: {e}")

        response, url, problems = self.ask_overpass(bbox, limit, seconds_left)

        if response is not None and response.status_code >= 400:
            problems.append((url, f"answered {response.status_code}", False))
            response = None

        if response is not None:
            try:
                elements = (response.json() or {}).get('elements') or []
            except ValueError:
                problems.append((url, 'sent something that is not JSON', True))
                response = None

        if response is None:
            return self.without_overpass(bbox, limit, problems, seconds_left)

        if url != (settings.get('osm_overpass_last_good') or ''):
            settings.set('osm_overpass_last_good', url)

        candidates, without_site = self.candidates_from(elements)
        notes = []
        if without_site:
            notes.append(f"{without_site} agencies in OpenStreetMap had no website "
                         f"recorded, so they were skipped")
        if problems:
            notes.append(f"answered by {url}, after {len(problems)} other server(s) "
                         f"would not")
        return Found(source=self.name, candidates=candidates, note='; '.join(notes))

    def without_overpass(self, bbox, limit, problems, seconds_left):
        """No Overpass server answered. Ask Nominatim, then explain."""
        tried = ' | '.join(f"{where}: {why}" for where, why, _temp in problems)
        temporary = any(temp for _where, _why, temp in problems)

        if poi_search_paused():
            tried += (" | Nominatim search: resting until "
                      f"{settings.get('osm_poi_pause_until')} (it asked us to "
                      f"slow down)")
        elif settings.get_bool('osm_nominatim_fallback') and seconds_left() > 6:
            try:
                candidates, without_site = self.nominatim_candidates(
                    bbox, limit, timeout=max(5, min(10, int(seconds_left()) - 2)))
            except Exception as e:                       # noqa: BLE001
                tried += f" | Nominatim search: {type(e).__name__}: {e}"
            else:
                if candidates:
                    notes = [f"no Overpass server answered ({tried}), so this came "
                             f"from Nominatim's own search instead, which finds "
                             f"fewer"]
                    if without_site:
                        notes.append(f"{without_site} had no website recorded, so "
                                     f"they were skipped")
                    return Found(source=self.name, candidates=candidates,
                                 note='; '.join(notes))
                tried += " | Nominatim search: found nothing with a website"

        if problems and all('429' in why for _where, why, _temp in problems):
            return Found(source=self.name, retryable=True,
                         error=f"every Overpass server is busy (too many "
                               f"requests). Try again later. Tried: {tried}")
        return Found(source=self.name, retryable=temporary,
                     error=f"no Overpass server answered. Tried: {tried}. "
                           f"{CLOUD_BLOCK_HINT}")


PROVIDERS = {
    ManualDiscovery.name: ManualDiscovery,
    OpenAIWebSearchDiscovery.name: OpenAIWebSearchDiscovery,
    OSMDiscovery.name: OSMDiscovery,
}


def get(name):
    """The provider by name, or None. Unknown names are a typo, not a crash."""
    factory = PROVIDERS.get((name or '').strip().lower())
    return factory() if factory else None


def enabled_names():
    """The sources switched on in Settings, in a sensible order."""
    chosen = [name for name in settings.get_list('discovery_enabled')
              if name in PROVIDERS]
    return chosen or ['manual']


def choices():
    """(name, label, costs_money) for every source, for the screen."""
    return [(name, factory.label, factory.costs_money)
            for name, factory in PROVIDERS.items()]
