"""Adding prospects, and keeping each company to exactly one row.

Everything here exists to answer one question the same way every time: is
this the same agency we already have? The answer is the canonical domain -
lowercase, no scheme, no www, no path - because two rows for one company is
how somebody gets two cold emails from us.
"""
import re
from urllib.parse import urlparse

from .. import models

_db = None

# A hosted page on someone else's platform is not a company website, and
# using one as an identity would merge dozens of agencies into one row.
SHARED_HOSTS = {
    'facebook.com', 'm.facebook.com', 'instagram.com', 'linkedin.com',
    'twitter.com', 'x.com', 'youtube.com', 'wixsite.com', 'sites.google.com',
    'business.site', 'wordpress.com', 'blogspot.com', 'idx.com',
    'rightmove.co.uk', 'zillow.com', 'realtor.com', 'properstar.com',
    'idealista.com', 'seloger.com', 'bayut.com', 'propertyfinder.ae',
}


def init(db):
    global _db
    _db = db


def canonical_domain(value):
    """'https://WWW.Example.com/about?x=1' -> 'example.com'."""
    text = (value or '').strip().lower()
    if not text:
        return ''
    if '@' in text and '://' not in text:
        text = text.rsplit('@', 1)[-1]
    if '://' not in text:
        text = 'https://' + text
    host = (urlparse(text).hostname or '').strip('.')
    if host.startswith('www.'):
        host = host[4:]
    if not re.fullmatch(r'[a-z0-9.-]+\.[a-z]{2,}', host or ''):
        return ''
    return host


def is_shared_host(domain):
    domain = (domain or '').lower()
    return any(domain == host or domain.endswith('.' + host) for host in SHARED_HOSTS)


def find_by_domain(domain):
    domain = canonical_domain(domain)
    if not domain:
        return None
    return models.Prospect.query.filter_by(canonical_domain=domain).first()


def add(website, market_id=None, name=None, phone=None, address=None,
        source='manual', source_ref=None, notes=None):
    """Add one prospect. Returns (prospect, created, problem).

    Not an exception when it is already there: adding the same agency twice
    is an ordinary thing to do by hand, and the screen should say "you
    already have this one" rather than show an error.
    """
    domain = canonical_domain(website)
    if not domain:
        return None, False, "That doesn't look like a website address"
    if is_shared_host(domain):
        return None, False, ("That is a page on someone else's platform "
                             "(Facebook, Instagram, a portal). Use the agency's "
                             "own website.")

    existing = models.Prospect.query.filter_by(canonical_domain=domain).first()
    if existing is not None:
        return existing, False, None

    prospect = models.Prospect(
        canonical_domain=domain,
        website=(website or '').strip() or f"https://{domain}",
        market_id=market_id,
        name=(name or '').strip() or None,
        phone=(phone or '').strip() or None,
        address=(address or '').strip() or None,
        source=source,
        source_ref=source_ref,
        notes=notes,
        stage='new',
    )
    _db.session.add(prospect)
    _db.session.commit()
    return prospect, True, None


def add_many(lines, market_id=None, source='manual'):
    """Paste a list of websites, one per line. Handy for the first 50, and
    the only discovery there is until Phase 2."""
    added, duplicates, problems = [], [], []
    for raw in (lines or []):
        text = (raw or '').strip()
        if not text:
            continue
        website, _, name = text.partition(',')
        prospect, created, problem = add(
            website.strip(), market_id=market_id, name=name.strip() or None,
            source=source)
        if problem:
            problems.append((text, problem))
        elif created:
            added.append(prospect)
        else:
            duplicates.append(prospect)
    return added, duplicates, problems
