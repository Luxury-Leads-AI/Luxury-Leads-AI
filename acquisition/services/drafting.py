"""Phase 5: writing the email, so Moaz can send it himself.

The engine does not send. It writes a subject and a body, records which
facts it used, and waits. Moaz copies each one into his own email and
sends it as himself - which, for the first twenty, is better than
automation anyway: you learn what works from replies to something a
person actually wrote.

THE DECISIONS THIS IMPLEMENTS (Moaz, 1 Nov 2026)

  The ask is a REPLY. Not a call, not a pilot - one question they can
  answer in a line. A booking link in a first cold email from a personal
  address reads like a campaign.

  ONE observation, stated plainly. The research found four or five true
  things about each agency; the email uses the single strongest one.
  Four facts about someone's business in a cold email unsettles people
  rather than impressing them, and the point of the observation is to
  prove a person looked, not to prove a database exists.

  A template he owns, polished by AI. He writes the voice once; the
  facts fill the slots; one cheap pass stops a batch sent the same
  morning reading like a mail merge.

THE RULE EVERYTHING ELSE HANGS OFF

Only `verified` facts may be quoted - things seen in the page's own
markup. A model's inference is not something to tell a stranger about
their own business. `pick_observation` will not look at anything else,
and `unsupported_claims` checks the finished text for numbers and
promises nobody can stand behind.
"""
import json
import re

# The observation, in order of how much it earns the next sentence. The
# first one whose fact is present and verified is the one used.
#
# Each entry: (fact field, a test on its value, the sentence).
# The sentence is deliberately plain. "I noticed you're running Intercom"
# is a person looking at a website; "Our analysis of your digital
# presence indicates" is a robot that bought a list.
OBSERVATIONS = (
    ('chat_kind',
     lambda value, facts: value == 'click_to_chat',
     "I see you've got a {chat_name} button on {domain} - which works "
     "well right up until someone messages at eleven at night."),

    ('chat_kind',
     lambda value, facts: value == 'hosted',
     "I noticed you're running {chat_name} on {domain}, so you've "
     "clearly decided live chat is worth having."),

    ('enquiry_routes',
     lambda value, facts: 'form' in value.lower() and '@' not in value
                          and 'phone' not in value.lower(),
     "Looking at {domain}, a contact form seems to be the only way for "
     "a buyer to reach you."),

    ('booking_link',
     lambda value, facts: bool(value.strip()),
     "I see you let people book viewings online from {domain} - so "
     "you're already set up for the part most agencies aren't."),

    ('languages',
     lambda value, facts: len([p for p in value.split(',') if p.strip()]) > 1,
     "I noticed {domain} is in {language_count} languages, so you're "
     "clearly getting enquiries from more than one country."),

    ('enquiry_routes',
     lambda value, facts: bool(value.strip()),
     "I had a look at {domain} and at how enquiries reach you."),
)

# A draft must not contain a number nobody can stand behind. These are
# the shapes a model reaches for when it is trying to be persuasive and
# has run out of facts.
UNSUPPORTED_PATTERNS = (
    (r'\b\d{1,3}\s?%', 'a percentage'),
    (r'\b\d+(\.\d+)?\s?x\b', 'a multiplier like "3x"'),
    (r'\b(hundreds|thousands|millions) of\b', 'a vague large number'),
    (r'\b(guarantee|guaranteed|proven to)\b', 'a promise'),
    (r'\b(all|most|every) (agencies|agents|clients)\b', 'a claim about everyone'),
)

# The line that makes this a note from a person rather than a campaign,
# and gives them a one-word way out. It is not optional: a test fails if
# a template loses it.
OPT_OUT_LINE = "If you'd rather I didn't write again, just say and I won't."

DEFAULT_SUBJECT = "{agency_short} - after-hours enquiries"

# Each paragraph is ONE line. Hard-wrapping at 70 characters looks tidy
# in a code editor and looks broken in Gmail, which re-wraps to the
# reader's window and leaves your line breaks in the middle of
# sentences. This is pasted into a real email client, so let it wrap.
DEFAULT_BODY = """Hi{owner_greeting},

{observation}

I build an AI assistant for estate agencies that answers website visitors around the clock - it asks the questions you'd ask, works out budget, area and timing, and books the viewing while they're still interested. The lead lands in your inbox with all of it written down.

Is after-hours enquiries something you'd want handled, or is it already covered?

{sign_off}

{opt_out}"""


def verified(facts):
    """Only what we saw in the page. A model's conclusion is not
    something to tell a stranger about their own business."""
    return {f.field: f for f in facts if f.confidence == 'verified' and f.value}


def _language_count(facts):
    row = facts.get('languages')
    if not row:
        return 0
    return len([part for part in row.value.split(',') if part.strip()])


def _chat_name(facts):
    row = facts.get('chat_widget')
    if not row:
        return 'chat'
    return row.value.replace(' click-to-chat', '').strip() or 'chat'


def pick_observation(facts, domain):
    """The single strongest true thing to open with.

    Returns (sentence, [fact ids used]) or ('', []) when nothing was
    verified - in which case there is no honest opening and the draft
    says so rather than inventing one.
    """
    seen = verified(facts)
    for field, applies, sentence in OBSERVATIONS:
        row = seen.get(field)
        if not row:
            continue
        try:
            if not applies((row.value or '').strip(), seen):
                continue
        except Exception:             # noqa: BLE001 - a bad fact is not a crash
            continue
        used = [row.id] if getattr(row, 'id', None) else []
        if field == 'chat_kind' and seen.get('chat_widget'):
            widget = seen['chat_widget']
            if getattr(widget, 'id', None):
                used.append(widget.id)
        filled = sentence.format(
            domain=domain,
            chat_name=_chat_name(seen),
            language_count=_language_count(seen))
        return filled, used
    return '', []


def short_name(name, domain):
    """'The Hofman Group Ltd' -> 'The Hofman Group'. A subject line with
    a legal suffix in it looks like a letter from a bank."""
    text = (name or '').strip()
    if not text:
        return (domain or '').split('.')[0].title()
    for suffix in (' ltd', ' limited', ' llc', ' inc', ' gmbh', ' bv',
                   ' sarl', ' pty', ' plc', ' s.l.', ' srl'):
        if text.lower().endswith(suffix):
            text = text[:-len(suffix)]
            break
    return text.strip(' .,')


def fill(template_subject, template_body, prospect, observation, sign_off):
    """Put the facts into the template. No model involved."""
    domain = prospect.canonical_domain or ''
    fields = {
        'agency': (prospect.name or domain),
        'agency_short': short_name(prospect.name, domain),
        'domain': domain,
        'observation': observation,
        'owner_greeting': '',
        'sign_off': sign_off or '',
        'opt_out': OPT_OUT_LINE,
    }
    subject = (template_subject or DEFAULT_SUBJECT).format(**fields)
    body = (template_body or DEFAULT_BODY).format(**fields)
    return subject.strip(), body.strip()


def unsupported_claims(text):
    """Numbers and promises nobody can stand behind.

    Runs on the finished draft - including after the AI pass, which is
    the point. A model asked to make an email read better will reach for
    a statistic if nothing stops it.
    """
    found = []
    for pattern, label in UNSUPPORTED_PATTERNS:
        if re.search(pattern, text, re.I):
            found.append(label)
    return found


def new_facts_in(polished, original):
    """Did the polish pass invent something?

    Any number in the polished text that was not in the draft it was
    given. Crude on purpose: a rewrite that adds a figure is exactly the
    failure worth catching, and anything subtler is for a person to spot
    when they read it before sending.
    """
    before = set(re.findall(r'\d[\d,.]*', original))
    after = set(re.findall(r'\d[\d,.]*', polished))
    return sorted(after - before)


def check(subject, body, original_body=None):
    """Everything that must be true before a person is asked to send it."""
    problems = []
    for label in unsupported_claims(subject + '\n' + body):
        problems.append(f"contains {label}")
    if OPT_OUT_LINE not in body:
        problems.append("the opt-out line is missing")
    if '{' in body or '}' in body:
        problems.append("a slot was left unfilled")
    if original_body:
        invented = new_facts_in(body, original_body)
        if invented:
            problems.append("the polish added numbers that were not in the "
                            "draft: " + ', '.join(invented))
    return problems


def check_json(problems, observation_used, fact_ids):
    return json.dumps({
        'problems': problems,
        'observation': observation_used,
        'fact_ids': fact_ids,
    })
