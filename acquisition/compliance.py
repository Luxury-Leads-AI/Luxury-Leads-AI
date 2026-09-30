"""The one gate before anything is sent to anybody.

can_contact() is asked three times for every email - when it is drafted,
when it is approved, and again when Mark as sent is pressed - because the
world can change in between: an opt-out can arrive, a market can be paused,
the kill switch can go on.

It answers with a reason, always, so a blocked prospect explains itself on
screen instead of quietly disappearing from the queue.
"""
from collections import namedtuple

from . import models, settings

Decision = namedtuple('Decision', 'allowed reason')

_db = None


def init(db):
    global _db
    _db = db

ALLOWED = Decision(True, '')


def normalize_email(email):
    return (email or '').strip().lower()


def domain_of(email_or_domain):
    value = normalize_email(email_or_domain)
    if '@' in value:
        value = value.rsplit('@', 1)[-1]
    return value.lstrip('www.') if value.startswith('www.') else value


def is_suppressed(email=None, domain=None):
    """Has this address, or anyone at this domain, asked us to stop?"""
    email = normalize_email(email)
    domain = domain_of(domain or email)
    query = models.Suppression.query
    if email and query.filter_by(email=email).first():
        return True
    if domain and query.filter_by(domain=domain).first():
        return True
    return False


def suppress(email=None, domain=None, reason='unsubscribe'):
    """Add an opt-out. Deliberately not tied to a prospect row: it has to
    outlive whatever the request came from."""
    email = normalize_email(email)
    domain = domain_of(domain) if domain else None
    if not email and not domain:
        raise ValueError("an opt-out needs an email or a domain")
    if is_suppressed(email=email, domain=domain):
        return None
    row = models.Suppression(email=email or None, domain=domain or None, reason=reason)
    _db.session.add(row)
    _db.session.commit()
    return row


def can_contact(prospect, contact=None):
    """May we send this prospect an email right now?"""
    if settings.outreach_stopped():
        return Decision(False, "Outreach is switched off in Settings")

    if prospect is None:
        return Decision(False, "No prospect")

    if prospect.do_not_contact:
        return Decision(False, prospect.do_not_contact_reason or "Marked do not contact")

    if prospect.stage in ('rejected', 'client'):
        return Decision(False, f"Stage is {models.PROSPECT_STAGE_LABELS.get(prospect.stage, prospect.stage)}")

    market = prospect.market
    if market is None:
        return Decision(False, "No market set, so no legal status to check")
    if market.status == 'paused':
        return Decision(False, f"{market.name} is paused")
    if market.legal_status != 'verified':
        label = models.LEGAL_STATUS_LABELS.get(market.legal_status, market.legal_status)
        return Decision(False, f"{market.name} is {label} - record a decision first")
    if market.outreach_method != 'email':
        label = models.OUTREACH_METHOD_LABELS.get(market.outreach_method, market.outreach_method)
        return Decision(False, f"{market.name}: {label}")

    if contact is not None:
        email = normalize_email(contact.email)
        if not email:
            return Decision(False, "That contact has no email address")
        if contact.email_check == 'bad_format':
            return Decision(False, "That address is not a valid email address")
        if contact.email_check == 'no_mx':
            return Decision(False, "That domain has no mail server")
        if is_suppressed(email=email):
            return Decision(False, "That address has opted out")

    if is_suppressed(domain=prospect.canonical_domain):
        return Decision(False, "Someone at that company has opted out")

    return ALLOWED


def contactable_contacts(prospect):
    """The contacts we may actually write to, best first: a named person
    beats info@, and a checked address beats an unchecked one."""
    allowed = []
    for contact in prospect.contacts:
        if can_contact(prospect, contact).allowed:
            allowed.append(contact)
    return sorted(allowed, key=lambda c: (c.is_generic, c.email_check != 'ok'))
