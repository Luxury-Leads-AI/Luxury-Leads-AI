"""Phase 4: how good a prospect is, and how sure we are.

Three numbers come out of here, and they are deliberately separate:

  fit           0-100. How much this agency would get out of the product.
  reachability  can we actually write to them, and how.
  confidence    how much the fit rests on things we SAW rather than things
                a model concluded.

Keeping them apart matters. A perfect-fit agency with no inbox is not a
60; it is a 90 we cannot reach, and those two want different actions
from a human. Collapsing them into one number hides that.

Nothing here touches the database or the network. It takes the facts
Phase 3 recorded and returns numbers plus the reasoning, line by line,
so a score can always be argued with. `breakdown` is not decoration -
it is the only reason to trust the number.

THE RULES, AND WHY

An agency already running a chat widget scores HIGHER, not lower. They
have already decided live chat is worth paying for, which is the hardest
part of the sale, and the pitch becomes a comparison rather than an
education. (Moaz's call, 15 Oct 2026.)

Luxury positioning and a high price band add points but exclude nobody:
the list is ranked, not filtered, so there is something to learn from
whether luxury actually converts better.

A fact the model inferred counts less than one we read in the markup.
Without that, an enthusiastic model could talk a prospect's score up on
its own say-so, which is the failure that makes scoring worthless.
"""
import json

# How much a fact counts by where it came from. `verified` means we saw
# it in the page; `inferred` means the model concluded it; `estimated`
# means it guessed. The ladder is what stops the AI inflating a score.
CONFIDENCE_WEIGHT = {'verified': 1.0, 'inferred': 0.6, 'estimated': 0.4}

# Points per signal, before the confidence weighting. Editable in
# Settings; a change makes a new ScoreModel version so an old score
# still says what it meant when it was given.
DEFAULT_WEIGHTS = {
    'chat_widget': 22,          # they already bought the idea
    'enquiry_routes': 14,       # they want to be contacted, and we know how
    'luxury_positioning': 16,
    'price_band': 10,
    'languages': 10,            # the assistant speaks nine
    'offices': 8,               # more offices, more agents, Agency tier
    'booking_link': 8,          # they already care about booked viewings
    'whatsapp': 6,              # the assistant captures WhatsApp numbers
    'property_types': 6,        # a real catalogue to talk about
}

# Where the bands fall. A band is what a person reads; the number is
# what sorts the list.
DEFAULT_THRESHOLDS = {'strong': 65, 'worth_a_look': 40}

BAND_LABELS = {
    'strong': 'Strong fit',
    'worth_a_look': 'Worth a look',
    'weak': 'Weak fit',
}

# Widgets we recognise. Used for the reasoning line, not the points -
# every widget scores the same, because guessing which products are paid
# would be guessing.
KNOWN_WIDGETS = (
    'tawk', 'intercom', 'crisp', 'drift', 'zendesk', 'livechat',
    'hubspot', 'tidio', 'freshchat', 'olark', 'smartsupp', 'jivochat',
    'chatra', 'purechat', 'zoho', 'manychat',
)

# Words in a price band or positioning fact that mean the top of the
# market. Matched on the lowercased value.
LUXURY_WORDS = ('luxury', 'prime', 'premium', 'exclusive', 'high-end',
                'high end', 'bespoke', 'prestige', 'finest', 'elite')


def _facts_by_field(facts):
    """Phase 3 keeps one row per field, so this is a plain lookup."""
    return {fact.field: fact for fact in facts}


def _weighted(points, confidence):
    return points * CONFIDENCE_WEIGHT.get(confidence, 0.4)


def _price_band_points(value, full):
    """A band naming the top of the market earns all of it; a band that
    is merely stated earns half, because knowing they publish prices is
    worth something on its own."""
    lowered = (value or '').lower()
    if any(word in lowered for word in LUXURY_WORDS):
        return full, 'names the top of the market'
    for marker in ('m+', 'million', '£1', '$1', '€1'):
        if marker in lowered:
            return full, 'prices in the millions'
    return full * 0.5, 'publishes a price range'


def _count_in(value):
    """How many comma-separated things a fact lists."""
    if not value:
        return 0
    return len([part for part in str(value).split(',') if part.strip()])


def score_facts(facts, weights=None):
    """The fit half. Returns (points, max_points, breakdown)."""
    weights = dict(DEFAULT_WEIGHTS, **(weights or {}))
    found = _facts_by_field(facts)
    lines = []
    total = 0.0

    for field, full in weights.items():
        fact = found.get(field)
        if not fact or not (fact.value or '').strip():
            lines.append({'signal': field, 'points': 0, 'of': full,
                          'why': 'nothing found', 'source': None,
                          'confidence': None})
            continue

        value = fact.value.strip()
        points, why = full, 'present'

        if field == 'chat_widget':
            named = [name for name in KNOWN_WIDGETS if name in value.lower()]
            why = (f"already runs {value} - they have decided chat is worth "
                   f"paying for" if named else
                   f"already runs a chat widget ({value})")
        elif field == 'enquiry_routes':
            count = _count_in(value)
            points = min(full, full * count / 3.0)
            why = f"{count} way{'s' if count != 1 else ''} to reach them: {value}"
        elif field == 'luxury_positioning':
            lowered = value.lower()
            if any(word in lowered for word in LUXURY_WORDS):
                why = 'positions itself at the top of the market'
            else:
                points, why = full * 0.4, 'positioning stated but not luxury'
        elif field == 'price_band':
            points, why = _price_band_points(value, full)
        elif field == 'languages':
            count = _count_in(value)
            points = min(full, full * count / 3.0)
            why = (f"{count} languages ({value}) - the assistant speaks nine"
                   if count > 1 else f"one language ({value})")
        elif field == 'offices':
            count = _count_in(value)
            points = min(full, full * count / 3.0)
            why = (f"{count} offices - likely a team, so the Agency tier"
                   if count > 1 else 'one office')
        elif field == 'booking_link':
            why = 'already takes bookings online, so viewings matter to them'
        elif field == 'property_types':
            count = _count_in(value)
            points = min(full, full * count / 4.0)
            why = f"{count} property types listed"

        earned = _weighted(points, fact.confidence)
        total += earned
        lines.append({
            'signal': field,
            'points': round(earned, 1),
            'of': full,
            'why': why,
            'source': fact.source_url,
            'confidence': fact.confidence,
        })

    return total, float(sum(weights.values())), lines


def reachability_of(contacts, facts):
    """How we could write to them, if at all. Separate from fit on
    purpose: 'a good fit we cannot reach' is a different job for a human
    than 'easy to reach and not worth it'."""
    usable = [c for c in contacts
              if c.email and c.email_check in (None, '', 'ok')]
    if usable:
        generic = [c for c in usable if c.is_generic]
        if generic:
            return 'inbox', f"{len(generic)} company inbox(es): " + \
                ', '.join(c.email for c in generic[:3])
        return 'inbox', f"{len(usable)} address(es) on file"

    routes = _facts_by_field(facts).get('enquiry_routes')
    value = (routes.value if routes else '') or ''
    if 'form' in value.lower():
        return 'form_only', 'a contact form, but no inbox we can write to'
    if any(c.email for c in contacts):
        return 'bad_address', 'the only address found did not check out'
    return 'none', 'no inbox and no contact form found'


def confidence_of(lines, facts):
    """How much of the score rests on things we actually saw.

    A high fit built entirely out of inferences is a number to distrust,
    and saying so is more useful than quietly shaving points off it."""
    earned = [line for line in lines if line['points'] > 0]
    if not earned:
        return 'none', 'nothing scored'
    verified = [line for line in earned if line['confidence'] == 'verified']
    share = len(verified) / len(earned)
    pages = _facts_by_field(facts).get('pages_read')
    read = _count_in(pages.value) if pages else 0
    if share >= 0.6 and read >= 2:
        return 'high', f"{len(verified)} of {len(earned)} signals seen on the page"
    if share >= 0.3:
        return 'medium', f"{len(verified)} of {len(earned)} signals seen on the page"
    return 'low', 'mostly inferred rather than read'


def band_for(fit, thresholds=None):
    thresholds = dict(DEFAULT_THRESHOLDS, **(thresholds or {}))
    if fit >= thresholds['strong']:
        return 'strong'
    if fit >= thresholds['worth_a_look']:
        return 'worth_a_look'
    return 'weak'


def score_prospect(facts, contacts, weights=None, thresholds=None):
    """Everything above, in one call.

    Returns a dict ready to become a Score row. It writes nothing and
    changes no stage - promoting a prospect is a person's decision.
    """
    points, possible, lines = score_facts(facts, weights)
    fit = int(round(100 * points / possible)) if possible else 0
    reach, reach_why = reachability_of(contacts, facts)
    sure, sure_why = confidence_of(lines, facts)
    band = band_for(fit, thresholds)
    return {
        'fit': fit,
        'band': band,
        'band_label': BAND_LABELS[band],
        'reachability': reach,
        'reachability_why': reach_why,
        'confidence': sure,
        'confidence_why': sure_why,
        'breakdown': lines,
    }


def breakdown_json(result):
    return json.dumps({
        'band': result['band'],
        'reachability_why': result['reachability_why'],
        'confidence_why': result['confidence_why'],
        'lines': result['breakdown'],
    })
