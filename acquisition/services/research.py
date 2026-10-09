"""Reading a prospect's own website and writing down only what it says.

This is the part of the engine that decides what a later email is allowed
to mention, so it is built around one rule: **a fact must be able to show
the page it came from.** Everything here returns something traceable, and
`confidence` says how it was learned:

    verified   we saw it in the markup - a mailto: link, a tel: link, a
               chat widget's own script, an hreflang tag
    inferred   a model read the page text and concluded it
    estimated  a guess from weak signals

Rules run first and cost nothing. The model only ever sees text that was
already fetched, gets no tools, and is asked for a fixed set of keys - so
the worst a hostile page can do is produce a bad answer that the schema
check throws away.

What is deliberately NOT collected: addresses belonging to named people.
A company inbox is a business contact; jean.dupont@agency.fr is personal
data with a deletion clock and a duty of care attached. Generic only.
"""
import re
from urllib.parse import urljoin, urlparse

# Pages worth opening after the home page, in the order we want them. The
# words are the ones agencies actually use, in the languages of the markets
# in the registry.
PAGE_HINTS = (
    ('contact', ('contact', 'kontakt', 'contacto', 'contato', 'contatti',
                 'contactez', 'nous-contacter', 'get-in-touch')),
    ('about', ('about', 'a-propos', 'apropos', 'qui-sommes-nous', 'notre-agence',
               'chi-siamo', 'sobre', 'nosotros', 'quienes-somos', 'team',
               'equipe', 'agency', 'agence')),
    ('listings', ('properties', 'property', 'biens', 'nos-biens', 'a-vendre',
                  'for-sale', 'inmuebles', 'immobili', 'propriedades',
                  'listings', 'catalogue', 'annonces')),
    ('services', ('services', 'prestations', 'servicios', 'servizi')),
)

# A chat widget leaves its own script behind. Seeing it is verified fact:
# we are not guessing from a picture of a speech bubble.
WIDGETS = (
    ('Intercom', ('widget.intercom.io', 'intercomsettings', 'intercom-frame')),
    ('Crisp', ('client.crisp.chat', 'crisp_website_id')),
    ('Tawk.to', ('embed.tawk.to', 'tawk_api')),
    ('Drift', ('js.driftt.com', 'drift.load')),
    ('HubSpot', ('js.hs-scripts.com', 'hubspot-messages')),
    ('Tidio', ('code.tidio.co', 'tidiochat')),
    ('LiveChat', ('cdn.livechatinc.com', '__lc.license')),
    ('Zendesk', ('static.zdassets.com', 'zopim')),
    ('Smartsupp', ('smartsuppchat', 'smartsupp.com')),
    ('Chatra', ('call.chatra.io', 'chatraid')),
    ('Olark', ('static.olark.com',)),
    ('JivoChat', ('code.jivosite.com', 'jivo_api')),
    ('Freshchat', ('wchat.freshchat.com', 'fcwidget')),
    ('Zoho SalesIQ', ('salesiq.zoho', '$zoho.salesiq')),
    ('Userlike', ('userlike-cdn', 'userlike.com')),
    ('Facebook Messenger', ('fb-customerchat', 'facebook-jssdk')),
)

# Role addresses: a company inbox, not a person.
GENERIC_LOCAL_PARTS = (
    'info', 'contact', 'contacto', 'contato', 'contatti', 'hello', 'hallo',
    'bonjour', 'hola', 'ciao', 'office', 'bureau', 'enquiries', 'enquiry',
    'inquiries', 'sales', 'team', 'admin', 'mail', 'email', 'reception',
    'welcome', 'support', 'agence', 'agency', 'immobilier', 'properties',
    'rent', 'location', 'ventes', 'ventas', 'vendas',
)

BOOKING_SIGNS = ('calendly.com', 'cal.com/', 'savvycal.com', 'hubspot.com/meetings',
                 'youcanbook.me', 'acuityscheduling.com', 'book-a-viewing',
                 'rendez-vous', 'appointment')

EMAIL_PATTERN = re.compile(
    r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}")
MAILTO_PATTERN = re.compile(r"""mailto:([^"'>?\s]+)""", re.I)
TEL_PATTERN = re.compile(r"""tel:([+0-9().\s-]{6,30})""", re.I)
WHATSAPP_PATTERN = re.compile(
    r"""https?://(?:api\.whatsapp\.com/send|wa\.me|web\.whatsapp\.com)[^"'\s>]*""", re.I)
HREF_PATTERN = re.compile(r"""<a\b[^>]*href=["']([^"'#]+)["']""", re.I)
LANG_PATTERN = re.compile(r"""<html[^>]*\blang=["']([A-Za-z-]{2,10})["']""", re.I)
HREFLANG_PATTERN = re.compile(r"""hreflang=["']([A-Za-z-]{2,10})["']""", re.I)
FORM_PATTERN = re.compile(r"<form\b", re.I)
SCRIPT_STYLE = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.I | re.S)
TAG = re.compile(r"<[^>]+>")
WHITESPACE = re.compile(r"\s+")


def same_site(url, domain):
    """Is this link on the prospect's own site? Anything else is somebody
    else's page and not evidence about this agency."""
    host = (urlparse(url).hostname or '').lower()
    if host.startswith('www.'):
        host = host[4:]
    return host == domain or host.endswith('.' + domain)


def page_links(html, base_url, domain, limit=4):
    """The next few pages worth opening, best first.

    Ordered by what they are likely to tell us rather than by where they sit
    on the page: contact before about before listings. One per kind, because
    four contact pages teach us nothing new.
    """
    found, seen = [], set()
    candidates = []
    for href in HREF_PATTERN.findall(html or ''):
        url = urljoin(base_url, href.strip())
        if not url.startswith(('http://', 'https://')) or not same_site(url, domain):
            continue
        url = url.split('#')[0].rstrip('/')
        if url in seen or url.rstrip('/') == base_url.rstrip('/'):
            continue
        seen.add(url)
        candidates.append(url)

    for _kind, words in PAGE_HINTS:
        for url in candidates:
            path = urlparse(url).path.lower()
            if any(word in path for word in words):
                found.append(url)
                break                       # one page per kind
        if len(found) >= limit:
            break
    return found[:limit]


def emails_in(html, domain):
    """Addresses published on the page that belong to this agency.

    Filtered to the agency's own domain on purpose: a page footer often
    carries the web designer's address, and writing to them would be both
    useless and rude.
    """
    found = set()
    for raw in MAILTO_PATTERN.findall(html or '') + EMAIL_PATTERN.findall(html or ''):
        address = raw.strip().strip('.,;:').lower()
        if '@' not in address:
            continue
        host = address.rsplit('@', 1)[1]
        if host == domain or host.endswith('.' + domain):
            found.add(address)
    return sorted(found)


def is_generic(address):
    """A role inbox, not a person."""
    local = (address or '').split('@')[0].lower()
    local = re.split(r'[._-]', local)[0] if local else ''
    return local in GENERIC_LOCAL_PARTS


def phones_in(html):
    """Only tel: links. A number loose in the text is as likely to be a
    price or a licence number."""
    numbers = []
    for raw in TEL_PATTERN.findall(html or ''):
        cleaned = WHITESPACE.sub(' ', raw).strip()
        digits = re.sub(r'\D', '', cleaned)
        if 7 <= len(digits) <= 15 and cleaned not in numbers:
            numbers.append(cleaned)
    return numbers[:5]


def whatsapp_in(html):
    links = []
    for url in WHATSAPP_PATTERN.findall(html or ''):
        if url not in links:
            links.append(url)
    return links[:3]


def widget_in(html):
    """Which chat widget, if any, this page loads."""
    lowered = (html or '').lower()
    for name, signs in WIDGETS:
        if any(sign in lowered for sign in signs):
            return name
    return ''


def has_contact_form(html):
    return bool(FORM_PATTERN.search(html or ''))


def booking_link(html, base_url, domain):
    for href in HREF_PATTERN.findall(html or ''):
        url = urljoin(base_url, href.strip())
        lowered = url.lower()
        if any(sign in lowered for sign in BOOKING_SIGNS):
            return url
    return ''


def languages_in(html):
    """From the markup, not from guessing at the prose."""
    codes = []
    for match in LANG_PATTERN.findall(html or '') + HREFLANG_PATTERN.findall(html or ''):
        code = match.strip().lower().split('-')[0]
        if len(code) == 2 and code != 'x' and code not in codes:
            codes.append(code)
    return codes[:8]


def visible_text(html, limit=6000):
    """The words a reader would see, for the model to read. Scripts and
    styles go first, so a widget's source cannot end up quoted back as if
    it were the agency's own prose."""
    body = SCRIPT_STYLE.sub(' ', html or '')
    body = TAG.sub(' ', body)
    body = (body.replace('&nbsp;', ' ').replace('&amp;', '&')
            .replace('&quot;', '"').replace('&#39;', "'")
            .replace('&lt;', '<').replace('&gt;', '>'))
    return WHITESPACE.sub(' ', body).strip()[:limit]


def routes_from(signals):
    """How a visitor can actually reach this agency, in plain words."""
    routes = []
    if signals.get('whatsapp'):
        routes.append('WhatsApp')
    if signals.get('phones'):
        routes.append('phone')
    if signals.get('form'):
        routes.append('contact form')
    if signals.get('emails'):
        routes.append('email')
    if signals.get('booking'):
        routes.append('booking link')
    return routes
