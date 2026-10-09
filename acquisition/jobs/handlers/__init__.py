"""The handlers that exist so far.

Phase 1 shipped the two that prove the machinery: a health check and a
prospect tidy-up. Phase 2 adds the two that fill the list without you
typing: find candidates for a city, and confirm each one is a real agency
with a real website before it becomes a prospect.

Research, scoring, drafting and reply classification arrive in phases 3
to 6, each as one more function here.
"""
import time
from datetime import datetime, timedelta

from ... import compliance, models, settings
from ...providers import discovery
from ...services import ai, fetch, prospects
from ...services import research as reading
from .. import registry, runner


@registry.handler('ping')
def ping(job, payload):
    """Does the queue work? Costs nothing, touches nothing."""
    return {'pong': True, 'at': datetime.utcnow().isoformat(timespec='seconds'),
            'echo': payload.get('echo')}


@registry.handler('normalize_prospect')
def normalize_prospect(job, payload):
    """Tidy one prospect: fill in a missing name and website from its
    domain, and make sure it has a stage. Cheap, and it gives the Phase 1
    screens something real to run."""
    prospect_id = job.prospect_id or payload.get('prospect_id')
    prospect = runner.session().get(models.Prospect, prospect_id)
    if prospect is None:
        return {'skipped': 'prospect is gone'}

    changed = []
    if not prospect.website and prospect.canonical_domain:
        prospect.website = f"https://{prospect.canonical_domain}"
        changed.append('website')
    if not prospect.name and prospect.canonical_domain:
        stem = prospect.canonical_domain.split('.')[0].replace('-', ' ')
        prospect.name = stem.title()
        changed.append('name')
    if not prospect.stage:
        prospect.stage = 'new'
        changed.append('stage')
    if changed:
        runner.session().commit()
    return {'prospect_id': prospect.id, 'changed': changed}


# ─────────────────────────────────────────────────────
# PHASE 2: finding agencies without typing them
# ─────────────────────────────────────────────────────

MAX_SEARCHES_NOTE = ("the monthly web-search cap in Settings is used up - "
                     "raise it there if you want more this month")


def searches_this_month():
    """Billable searches made since the 1st, for the monthly cap."""
    from ...services import ai
    rows = (models.Cost.query
            .filter(models.Cost.created_at >= ai.month_start(),
                    models.Cost.purpose == 'discovery_search')
            .with_entities(models.Cost.units).all())
    return sum(int(row[0] or 0) for row in rows)


@registry.handler('discover')
def discover(job, payload):
    """Find candidate agencies for one market, from one source.

    What it does NOT do: save anything it has not looked at. Every candidate
    becomes a 'confirm_candidate' job, because a search - an AI one above
    all - can produce a convincing agency that does not exist. A name only
    becomes a prospect once its website answers.
    """
    market_id = payload.get('market_id')
    market = runner.session().get(models.Market, market_id)
    if market is None:
        return {'skipped': 'that market is gone'}

    source = payload.get('source') or 'osm'
    limit = max(1, min(int(payload.get('limit') or 20), 60))

    provider = discovery.get(source)
    if provider is None:
        return {'error': f"there is no discovery source called '{source}'"}

    if provider.costs_money:
        cap = settings.get_int('web_search_cap_month', 50)
        if cap and searches_this_month() >= cap:
            return {'error': MAX_SEARCHES_NOTE, 'searched': 0, 'found': 0}

    found = provider.search(market, limit=limit, job_id=job.id)
    if found.error:
        if getattr(found, 'retryable', False):
            # Busy, not closed. Raising puts the job back in the queue with a
            # growing wait instead of writing the city off; three tries, then
            # it stops and says so.
            raise RuntimeError(f"{found.error} (it will try again by itself)")
        return {'error': found.error, 'source': source, 'market': market.name}

    queued, already, unusable = 0, 0, 0
    for candidate in found.candidates:
        domain = prospects.canonical_domain(candidate.website)
        if not domain or prospects.is_shared_host(domain):
            unusable += 1
            continue
        if prospects.find_by_domain(domain) is not None:
            already += 1
            continue
        if compliance.is_suppressed(domain=domain):
            unusable += 1
            continue
        runner.enqueue('confirm_candidate',
                       payload={'market_id': market.id, 'source': source,
                                'candidate': candidate.as_dict(),
                                # only set when the whole run is pointed at a
                                # local copy of these services, for testing
                                'allow_private': bool(payload.get('allow_private'))},
                       idempotency_key=f"confirm:{domain}")
        queued += 1

    return {'source': source, 'market': market.name,
            'found': len(found.candidates), 'queued': queued,
            'already_had': already, 'unusable': unusable,
            'searched': found.searched, 'usd': round(found.usd, 4),
            'note': found.note}


@registry.handler('confirm_candidate')
def confirm_candidate(job, payload):
    """Open one candidate's website. If it answers, it becomes a prospect.

    This is the step that keeps invented agencies out of the list. It also
    gives the prospect a decent name, from the page title, when the source
    had none.
    """
    candidate = payload.get('candidate') or {}
    website = candidate.get('website') or ''
    domain = prospects.canonical_domain(website)
    if not domain:
        return {'skipped': 'no usable website address'}
    if prospects.find_by_domain(domain) is not None:
        return {'skipped': 'already had it', 'domain': domain}

    page = fetch.site_answers(domain, allow_private=payload.get('allow_private', False))
    needs_human = not page.ok
    reason = ''
    if page.blocked_by_robots:
        reason = "the site asks robots not to read it - check it by hand"
    elif page.looks_like_bot_wall:
        reason = "the site puts a human check in front of visitors"
    elif not page.ok:
        reason = page.error or f"the site answered {page.status}"

    if not page.ok and not (page.blocked_by_robots or page.looks_like_bot_wall):
        # Nothing answered at all: most likely the search invented it.
        return {'discarded': domain, 'why': reason}

    name = (candidate.get('name') or '').strip() or fetch.page_title(page.html)
    prospect, created, problem = prospects.add(
        website=f"https://{domain}",
        market_id=payload.get('market_id'),
        name=name or None,
        phone=candidate.get('phone') or None,
        address=candidate.get('address') or None,
        source=payload.get('source') or 'discovery',
        source_ref=candidate.get('source_ref') or None)
    if problem:
        return {'discarded': domain, 'why': problem}
    if not created:
        return {'skipped': 'already had it', 'domain': domain}

    if needs_human:
        prospect.needs_human = True
        prospect.needs_human_reason = reason
    runner.session().commit()

    return {'prospect_id': prospect.id, 'domain': domain, 'name': prospect.name,
            'needs_human': needs_human, 'why': reason,
            'status': page.status, 'took_ms': page.elapsed_ms}


# ─────────────────────────────────────────────────────
# PHASE 3: reading the website
# ─────────────────────────────────────────────────────
#
# One job per prospect, because a job must finish inside gunicorn's 30
# seconds. It opens the home page, follows at most a few pages that are
# likely to say something useful, learns what it can from the markup for
# free, and then - once - asks a model to read the text for the judgement
# calls. Every fact it writes carries the page it came from.

RESEARCH_BUDGET_SECONDS = 22

AI_SYSTEM = (
    "You are reading an estate agency's own website to judge whether an "
    "AI chat assistant would suit them. Answer only from the text given. "
    "Where the text does not say, answer \"unclear\" or leave the list "
    "empty. Never guess, never flatter, never invent a detail."
)

AI_KEYS = ('luxury', 'price_band', 'property_types', 'languages', 'offices',
           'one_line')

# field -> how the Prospect screen should read. Kept here so the screen and
# the extractor cannot drift apart.
FACT_LABELS = {
    'chat_widget': 'Chat widget',
    'enquiry_routes': 'How enquiries reach them',
    'phone': 'Phone',
    'whatsapp': 'WhatsApp',
    'booking_link': 'Booking link',
    'languages': 'Languages',
    'pages_read': 'Pages read',
    'luxury_positioning': 'Luxury positioning',
    'price_band': 'Price band',
    'property_types': 'Property types',
    'offices': 'Offices',
    'what_they_say': 'What they say about themselves',
}


def record_fact(prospect, field, value, confidence, source_url, extractor):
    """One row per field: researching twice updates rather than piles up."""
    models.Fact.query.filter_by(prospect_id=prospect.id, field=field).delete()
    if value in (None, '', [], {}):
        return None
    if isinstance(value, (list, tuple)):
        value = ', '.join(str(part) for part in value if str(part).strip())
        if not value:
            return None
    row = models.Fact(prospect_id=prospect.id, field=field,
                      value=str(value)[:2000], confidence=confidence,
                      source_url=(source_url or '')[:500], extractor=extractor)
    runner.session().add(row)
    return row


def record_contact(prospect, address, source_url):
    """A company inbox. Named people are not collected at all, so there is
    no personal data here to leak - but the retention clock is set anyway,
    because an inbox at a company we never contacted is still theirs."""
    existing = models.Contact.query.filter_by(prospect_id=prospect.id,
                                              email=address).first()
    if existing is not None:
        return existing
    days = settings.get_int('retention_days_uncontacted', 180)
    row = models.Contact(prospect_id=prospect.id, email=address[:200],
                         is_generic=True, source_url=(source_url or '')[:500],
                         personal_data_expires_at=datetime.utcnow() + timedelta(days=days))
    runner.session().add(row)
    return row


def research_spent_on(prospect_id):
    """What this one prospect has already cost in AI, for the per-prospect cap."""
    rows = (models.Cost.query
            .filter(models.Cost.prospect_id == prospect_id,
                    models.Cost.purpose.like('research%'))
            .with_entities(models.Cost.usd).all())
    return sum(float(row[0] or 0) for row in rows)


def read_site(prospect, seconds_left, allow_private=False):
    """The home page, then a few pages worth opening. [(url, html)]."""
    domain = prospect.canonical_domain
    home = fetch.site_answers(domain, allow_private=allow_private,
                              budget=max(4.0, min(12.0, seconds_left())))
    if not home.ok or not home.html:
        return [], home

    start = home.final_url or f"https://{domain}/"
    pages = [(start, home.html)]
    wanted = reading.page_links(home.html, start, domain,
                                limit=max(0, settings.get_int('research_pages', 5) - 1))
    for url in wanted:
        if seconds_left() < 4:
            break
        page = fetch.get(url, allow_private=allow_private,
                         budget=max(3.0, min(8.0, seconds_left() - 2)))
        if page.ok and page.html:
            pages.append((page.final_url or url, page.html))
    return pages, home


def facts_from_markup(prospect, pages):
    """Everything learnable for free, and where each thing was seen."""
    domain = prospect.canonical_domain
    signals = {'emails': [], 'phones': [], 'whatsapp': [], 'form': False,
               'booking': '', 'languages': [], 'widget': '', 'sources': {}}

    for url, html in pages:
        for address in reading.emails_in(html, domain):
            if reading.is_generic(address) and address not in signals['emails']:
                signals['emails'].append(address)
                signals['sources'].setdefault(address, url)
        for number in reading.phones_in(html):
            if number not in signals['phones']:
                signals['phones'].append(number)
                signals['sources'].setdefault('phone', url)
        for link in reading.whatsapp_in(html):
            if link not in signals['whatsapp']:
                signals['whatsapp'].append(link)
                signals['sources'].setdefault('whatsapp', url)
        if not signals['form'] and reading.has_contact_form(html):
            signals['form'] = True
            signals['sources'].setdefault('form', url)
        if not signals['booking']:
            link = reading.booking_link(html, url, domain)
            if link:
                signals['booking'] = link
                signals['sources'].setdefault('booking', url)
        if not signals['widget']:
            name = reading.widget_in(html)
            if name:
                signals['widget'] = name
                signals['sources'].setdefault('widget', url)
        for code in reading.languages_in(html):
            if code not in signals['languages']:
                signals['languages'].append(code)
                signals['sources'].setdefault('languages', url)
    return signals


@registry.handler('research')
def research(job, payload):
    """Read one prospect's website and write down what it says."""
    prospect_id = job.prospect_id or payload.get('prospect_id')
    prospect = runner.session().get(models.Prospect, prospect_id)
    if prospect is None:
        return {'skipped': 'prospect is gone'}
    if settings.everything_stopped():
        return {'error': 'everything is stopped in Settings'}

    started = time.monotonic()

    def seconds_left():
        return RESEARCH_BUDGET_SECONDS - (time.monotonic() - started)

    allow_private = bool(payload.get('allow_private'))
    pages, home = read_site(prospect, seconds_left, allow_private=allow_private)
    if not pages:
        reason = (home.error or 'the website did not answer')[:200]
        prospect.needs_human = True
        prospect.needs_human_reason = reason
        runner.session().commit()
        if home.blocked_by_robots:
            return {'error': f"that site's robots.txt asks us not to read it - "
                             f"look at {prospect.canonical_domain} by hand",
                    'prospect_id': prospect.id}
        raise RuntimeError(f"could not read {prospect.canonical_domain}: {reason}")

    signals = facts_from_markup(prospect, pages)
    where = signals['sources']
    home_url = pages[0][0]

    record_fact(prospect, 'chat_widget', signals['widget'] or 'none seen',
                'verified', where.get('widget', home_url), 'rules')
    record_fact(prospect, 'enquiry_routes', reading.routes_from(signals),
                'verified', home_url, 'rules')
    record_fact(prospect, 'phone', signals['phones'], 'verified',
                where.get('phone'), 'rules')
    record_fact(prospect, 'whatsapp', signals['whatsapp'], 'verified',
                where.get('whatsapp'), 'rules')
    record_fact(prospect, 'booking_link', signals['booking'], 'verified',
                where.get('booking'), 'rules')
    record_fact(prospect, 'pages_read', [url for url, _html in pages],
                'verified', home_url, 'rules')
    if signals['languages']:
        record_fact(prospect, 'languages', signals['languages'], 'verified',
                    where.get('languages', home_url), 'rules')

    for address in signals['emails']:
        record_contact(prospect, address, where.get(address, home_url))

    note, model_used = ai_read(prospect, pages, job, signals)

    prospect.stage = 'researched'
    if prospect.needs_human and prospect.needs_human_reason and \
            'did not answer' in (prospect.needs_human_reason or ''):
        prospect.needs_human = False
        prospect.needs_human_reason = None
    runner.session().commit()

    return {'prospect_id': prospect.id, 'domain': prospect.canonical_domain,
            'pages': len(pages), 'emails': len(signals['emails']),
            'widget': signals['widget'] or 'none seen',
            'routes': reading.routes_from(signals),
            'ai': model_used or 'skipped', 'note': note}


def ai_read(prospect, pages, job, signals):
    """One model pass over what was already fetched. (note, model or '')."""
    if not settings.get_bool('research_ai'):
        return 'AI reading is switched off in Settings', ''

    cap = settings.get_float('research_cost_cap_usd', 0.02)
    if cap and research_spent_on(prospect.id) >= cap:
        return f"already spent the ${cap} cap on this agency", ''

    text = '\n\n'.join(
        f"--- {url} ---\n{reading.visible_text(html, 2500)}" for url, html in pages
    )[:9000]
    if len(text) < 200:
        return 'the pages had almost no text to read', ''

    model = settings.get('model_extract') or 'gpt-4o-mini'
    question = (
        f"Agency: {prospect.name or prospect.canonical_domain} "
        f"({prospect.canonical_domain})\n\n{text}\n\n"
        'Answer with JSON exactly like: {"luxury": "yes|no|unclear", '
        '"price_band": "short phrase or empty", "property_types": ["..."], '
        '"languages": ["fr","en"], "offices": 0, '
        '"one_line": "at most 25 words on what makes this agency distinctive, '
        'in their own terms"}'
    )
    data, error = ai.ask_json(AI_SYSTEM, question, purpose='research_read',
                              model=model, max_tokens=400, estimated_usd=0.004,
                              job_id=job.id, prospect_id=prospect.id,
                              schema_keys=AI_KEYS)
    if error:
        return f"the AI read did not happen: {error}", ''

    home_url = pages[0][0]
    record_fact(prospect, 'luxury_positioning', (data.get('luxury') or '').strip(),
                'inferred', home_url, model)
    record_fact(prospect, 'price_band', (data.get('price_band') or '').strip(),
                'inferred', home_url, model)
    record_fact(prospect, 'property_types', data.get('property_types') or [],
                'inferred', home_url, model)
    record_fact(prospect, 'what_they_say', (data.get('one_line') or '').strip(),
                'inferred', home_url, model)
    offices = data.get('offices')
    if isinstance(offices, int) and offices > 0:
        record_fact(prospect, 'offices', offices, 'inferred', home_url, model)
    if not signals['languages'] and data.get('languages'):
        record_fact(prospect, 'languages', data.get('languages'), 'inferred',
                    home_url, model)
    return '', model
