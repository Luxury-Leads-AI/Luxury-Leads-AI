"""The handlers that exist so far.

Phase 1 shipped the two that prove the machinery: a health check and a
prospect tidy-up. Phase 2 adds the two that fill the list without you
typing: find candidates for a city, and confirm each one is a real agency
with a real website before it becomes a prospect.

Research, scoring, drafting and reply classification arrive in phases 3
to 6, each as one more function here.
"""
from datetime import datetime

from ... import compliance, models, settings
from ...providers import discovery
from ...services import fetch, prospects
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
