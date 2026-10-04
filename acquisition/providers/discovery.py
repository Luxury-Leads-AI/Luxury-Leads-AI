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
from dataclasses import dataclass, field

import httpx

from .. import settings
from ..services import ai

OPENAI_RESPONSES_URL = 'https://api.openai.com/v1/responses'
# The public OpenStreetMap services, which are free and volunteer-run. If we
# ever query them heavily, their fair-use policy asks us to run our own
# copy - which is then a setting, not a deploy.
DEFAULT_NOMINATIM_URL = 'https://nominatim.openstreetmap.org/search'
DEFAULT_OVERPASS_URL = 'https://overpass-api.de/api/interpreter'
CONTACT = 'https://luxury-leads-ai.onrender.com'


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


def user_agent():
    contact = settings.get('operator_email') or CONTACT
    return f"LuxuryLeadsAI/1.0 (+{CONTACT}; contact: {contact})"


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
    upgrading it would touch the live chatbot. httpx is already a
    dependency, so this costs nothing.

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
        self.http = http or httpx

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

    def _post(self, payload, timeout=90):
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
                return Found(source=self.name, error=f"{type(e).__name__}: {e}")

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
        self.http = http or httpx

    @property
    def nominatim_url(self):
        return settings.get('osm_nominatim_url') or DEFAULT_NOMINATIM_URL

    @property
    def overpass_url(self):
        return settings.get('osm_overpass_url') or DEFAULT_OVERPASS_URL

    def bbox_for(self, market):
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
            self.nominatim_url, timeout=30,
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

    def query_for(self, bbox, limit):
        south, north, west, east = bbox
        box = f"{south},{west},{north},{east}"
        parts = ''.join(f'nwr["{key}"="{value}"]({box});' for key, value in self.AGENT_TAGS)
        return f"[out:json][timeout:40];({parts});out center tags {limit};"

    def search(self, market, limit=40, **kwargs):
        try:
            bbox = self.bbox_for(market)
        except Exception as e:                           # noqa: BLE001
            return Found(source=self.name, error=f"{type(e).__name__}: {e}")

        try:
            response = self.http.post(
                self.overpass_url, timeout=90,
                data={'data': self.query_for(bbox, limit)},
                headers={'User-Agent': user_agent()})
        except Exception as e:                           # noqa: BLE001
            return Found(source=self.name, error=f"{type(e).__name__}: {e}")

        if response.status_code == 429:
            return Found(source=self.name,
                         error="OpenStreetMap is busy (too many requests). Try again later.")
        if response.status_code >= 400:
            return Found(source=self.name,
                         error=f"Overpass answered {response.status_code}")
        try:
            elements = (response.json() or {}).get('elements') or []
        except ValueError:
            return Found(source=self.name, error="Overpass sent something that is not JSON")

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

        note = ''
        if without_site:
            note = (f"{without_site} agencies in OpenStreetMap had no website "
                    f"recorded, so they were skipped")
        return Found(source=self.name, candidates=candidates, note=note)


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
