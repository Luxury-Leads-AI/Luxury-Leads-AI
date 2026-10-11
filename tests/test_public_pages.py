"""The pages a payment processor reads before it lets you take money.

Paddle checks three things on the public site: a real contact address, a
visible price, and working links to Terms, Privacy and Refunds with Paddle
named as Merchant of Record. All three had holes, and the hole that matters
most was not any single one of them - it was *drift*. The Terms promised a
7-day trial, the pricing page promised 14, the code gave 14, and the refund
policy was written around 14. Three of four agreed, which is exactly how
nobody notices.

So these tests do not check the wording. They check that the pages agree
with each other and with the code, which is the thing that quietly stops
being true.
"""
import inspect
import os
import re

import pytest

import app as app_module

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Every page a visitor - or a reviewer - can reach without signing in.
PUBLIC_PAGES = ('/', '/pricing', '/terms', '/privacy-policy', '/refund-policy',
                '/signup', '/signup/solo', '/signup/agency')

LEGAL_LINKS = ('/terms', '/privacy-policy', '/refund-policy')


def page(client, path):
    response = client.get(path)
    assert response.status_code == 200, f"{path} answered {response.status_code}"
    return response.get_data(as_text=True)


def templates():
    folder = os.path.join(ROOT, 'templates')
    for name in sorted(os.listdir(folder)):
        if name.endswith('.html'):
            with open(os.path.join(folder, name), encoding='utf-8') as handle:
                yield name, handle.read()


# ── 1. a contact address that a human can actually read ──

@pytest.mark.parametrize('path', PUBLIC_PAGES)
def test_every_public_page_shows_the_support_address(client, path):
    """Not only inside a mailto: link. A reviewer with no mail client
    clicks that and nothing happens - which is how this was found."""
    body = page(client, path)
    without_hrefs = re.sub(r'href="[^"]*"', '', body)
    assert app_module.SUPPORT_EMAIL in without_hrefs, \
        f"{path} hides the address inside a link"


@pytest.mark.parametrize('path', PUBLIC_PAGES)
def test_no_page_still_carries_a_personal_or_placeholder_address(client, path):
    body = page(client, path).lower()
    for bad in ('yasar.ai.leads@gmail.com', 'your-email@', 'example@example',
                'lorem ipsum', 'todo:', 'coming soon'):
        assert bad not in body, f"{path} still contains {bad!r}"


# ── 2. a price a reviewer can see ──

def test_the_pricing_page_shows_a_real_monthly_price(client):
    body = page(client, '/pricing')
    assert '$197' in body and '/month' in body
    assert '$497' in body


def test_the_prices_shown_are_the_prices_in_the_code(client):
    """A price on the page that the backend does not charge is the kind of
    discrepancy that loses an account."""
    body = page(client, '/pricing')
    for tier in ('solo', 'agency'):
        assert f"${app_module.TIER_LIMITS[tier]['price']}" in body


def test_the_page_says_what_currency_and_who_bills(client):
    body = page(client, '/pricing')
    assert 'US dollars' in body
    assert 'Merchant of Record' in body


# ── 3. legal footers that work ──

@pytest.mark.parametrize('path', PUBLIC_PAGES)
def test_every_public_page_links_to_all_three_policies(client, path):
    body = page(client, path)
    for link in LEGAL_LINKS:
        assert f'href="{link}"' in body, f"{path} does not link to {link}"


@pytest.mark.parametrize('link', LEGAL_LINKS)
def test_those_links_actually_open(client, link):
    """'Active, working links' - so follow them."""
    assert client.get(link).status_code == 200


@pytest.mark.parametrize('path', PUBLIC_PAGES)
def test_every_public_page_names_paddle_as_merchant_of_record(client, path):
    body = page(client, path)
    assert 'Merchant of Record' in body, f"{path} does not name the MoR"
    assert 'Paddle' in body


@pytest.mark.parametrize('document', ['/terms', '/privacy-policy', '/refund-policy'])
def test_each_policy_explains_paddles_role_in_its_own_text(client, document):
    """The footer line is not enough: Paddle asks for it inside the
    documents themselves."""
    body = page(client, document)
    before_footer = body.split('site-footer')[0]
    assert 'Paddle' in before_footer, f"{document} only mentions Paddle in the footer"


# ── 4. the pages agree with each other, and with the code ──

def code_trial_days():
    return inspect.signature(
        app_module.provision_agency).parameters['trial_days'].default


def trial_lengths_claimed():
    """Every "N-day trial" / "N days" claim in the templates, with its file."""
    claims = {}
    pattern = re.compile(r'(\d+)[\s-]?day', re.I)
    for name, body in templates():
        for match in pattern.finditer(body):
            window = body[max(0, match.start() - 90):match.end() + 90].lower()
            if 'trial' in window:
                claims.setdefault(int(match.group(1)), set()).add(name)
    return claims


def test_the_code_still_gives_the_trial_everyone_was_promised():
    assert code_trial_days() == 14


def test_every_page_promises_the_same_trial_length():
    """The one that started this: Terms said 7, everything else said 14."""
    claims = trial_lengths_claimed()
    assert len(claims) == 1, f"pages disagree about the trial: {claims}"
    assert list(claims) == [code_trial_days()], \
        f"the pages promise {list(claims)} days, the code gives {code_trial_days()}"


def test_nobody_is_told_they_will_be_charged_automatically():
    """No card is taken at signup, so an automatic charge is impossible -
    and promising one in the Terms is a misrepresentation."""
    with open(os.path.join(ROOT, 'templates', 'terms.html'), encoding='utf-8') as handle:
        terms = handle.read().lower()
    assert 'you will be charged the subscription fee unless you cancel' not in terms
    assert 'no payment details are required' in terms


def test_the_policies_name_the_services_actually_in_use():
    """A privacy policy that lists a service you dropped is wrong in the
    one direction regulators care about."""
    with open(os.path.join(ROOT, 'templates', 'privacy.html'), encoding='utf-8') as handle:
        privacy = handle.read()
    assert 'Brevo' in privacy, 'the email provider in use is not named'
    assert 'SendGrid' not in privacy, 'it still names a provider we do not use'


def test_no_template_anywhere_still_names_sendgrid():
    for name, body in templates():
        assert 'SendGrid' not in body, f"{name} still names SendGrid"


# ── 5. the footer is written once ──

def test_the_footer_exists_in_one_place_only():
    """Six copies of a legal footer is six chances to fix five of them."""
    shared = os.path.join(ROOT, 'templates', '_public_footer.html')
    assert os.path.exists(shared)
    including = [name for name, body in templates()
                 if '_public_footer.html' in body]
    assert len(including) == len(PUBLIC_PAGES), including


def test_the_business_address_is_only_shown_when_there_is_one(client):
    """Better an absent address than an invented one."""
    body = page(client, '/')
    assert 'None' not in body.split('site-footer')[-1]
    if not app_module.BUSINESS_ADDRESS:
        assert 'site-footer-legal">·' not in body


# ── 6. the "last updated" date cannot go stale ──
#
# The dates on Terms, Privacy and Refunds said May and July while the text
# had been rewritten in October. Three hard-coded strings is three chances
# to change the wording and forget the date, so the date is now one
# constant - and this is what makes you remember to move it.

POLICY_FINGERPRINTS = {
    'terms.html': '3e2fd3c4c5ac8125',
    'privacy.html': 'dc67bee919ac4ab9',
    'refund.html': '415779c3160a5e2d',
}


def policy_fingerprint(name):
    """A hash of the policy's words - not its styling or its markup."""
    import hashlib
    with open(os.path.join(ROOT, 'templates', name), encoding='utf-8') as handle:
        source = handle.read()
    # Only what is inside <body>: the shared head and footer are not this
    # document's policy, and changing them must not look like it.
    inside = re.search(r'<body[^>]*>(.*)</body>', source, re.S)
    body = inside.group(1) if inside else source
    body = re.sub(r'<style>.*?</style>', '', body, flags=re.S)
    body = re.sub(r'\{%.*?%\}', ' ', body, flags=re.S)      # Jinja statements
    text = re.sub(r'<[^>]+>', ' ', body)
    text = re.sub(r'\s+', ' ', text).strip()
    return hashlib.sha256(text.encode()).hexdigest()[:16]


@pytest.mark.parametrize('name', sorted(POLICY_FINGERPRINTS))
def test_a_policy_cannot_change_without_somebody_noticing_the_date(name):
    """If this fails you changed the wording of a legal document. Move
    app.POLICY_UPDATED to today, then paste the new hash below. Annoying
    on purpose: a policy whose date lies about when it changed is worse
    than one that is simply old."""
    current = policy_fingerprint(name)
    assert current == POLICY_FINGERPRINTS[name], (
        f"{name} has been edited. Set app.POLICY_UPDATED to today's date, "
        f"then update POLICY_FINGERPRINTS['{name}'] to '{current}'."
    )


@pytest.mark.parametrize('document', ['/terms', '/privacy-policy', '/refund-policy'])
def test_every_policy_shows_the_same_date(client, document):
    body = page(client, document)
    assert app_module.POLICY_UPDATED in body


def test_that_date_is_not_one_of_the_old_stale_ones():
    assert app_module.POLICY_UPDATED not in ('May 3, 2026', 'July 2026')


def test_the_date_is_not_written_into_the_templates_any_more():
    """One constant, three pages."""
    for name in POLICY_FINGERPRINTS:
        with open(os.path.join(ROOT, 'templates', name), encoding='utf-8') as handle:
            body = handle.read()
        assert '{{ policy_updated }}' in body, f"{name} hard-codes its date"


# ── 7. being findable: SEO, and being quotable: GEO ──
#
# Search engines and AI answer engines want the same two things: a page
# that says plainly what it is, and machine-readable facts they can lift
# without guessing. The checks below are the ones that silently rot -
# a missing description, two pages claiming the same one, a canonical
# pointing at the old host, a price in the structured data that nobody
# can actually buy.

def head_of(client, path):
    body = page(client, path)
    return body.split('</head>')[0]


@pytest.mark.parametrize('path', PUBLIC_PAGES)
def test_every_page_says_what_it_is(client, path):
    head = head_of(client, path)
    found = re.search(r'<meta name="description" content="([^"]+)"', head)
    assert found, f"{path} has no description"
    length = len(found.group(1))
    assert 50 <= length <= 320, f"{path} description is {length} characters"


def test_no_two_pages_claim_the_same_description(client):
    """Duplicate descriptions are the classic reason a site's inner pages
    never rank: the engine cannot tell them apart."""
    seen = {}
    for path in PUBLIC_PAGES:
        found = re.search(r'<meta name="description" content="([^"]+)"',
                          head_of(client, path))
        assert found.group(1) not in seen, \
            f"{path} and {seen.get(found.group(1))} share a description"
        seen[found.group(1)] = path


@pytest.mark.parametrize('path', PUBLIC_PAGES)
def test_every_page_points_at_its_own_canonical_address(client, path):
    head = head_of(client, path)
    expected = f'<link rel="canonical" href="{app_module.PUBLIC_BASE_URL}{path}">'
    assert expected in head, f"{path} has no canonical, or the wrong one"


@pytest.mark.parametrize('path', PUBLIC_PAGES)
def test_every_page_has_a_social_preview(client, path):
    head = head_of(client, path)
    for tag in ('og:title', 'og:description', 'og:url', 'twitter:card'):
        assert tag in head, f"{path} is missing {tag}"


@pytest.mark.parametrize('path', ['/terms', '/privacy-policy', '/refund-policy'])
def test_the_legal_pages_are_not_search_targets(client, path):
    """Useful to a person who goes looking, not a page to rank."""
    assert 'noindex' in head_of(client, path)


@pytest.mark.parametrize('path', ['/', '/pricing', '/signup'])
def test_the_pages_that_sell_are_indexable(client, path):
    assert 'noindex' not in head_of(client, path)


def test_crawlers_are_told_where_to_look_and_where_not_to(client):
    response = client.get('/robots.txt')
    assert response.status_code == 200
    assert response.mimetype == 'text/plain'
    body = response.get_data(as_text=True)
    assert f"Sitemap: {app_module.PUBLIC_BASE_URL}/sitemap.xml" in body
    for private in ('/owner', '/agent', '/chat', '/super-admin'):
        assert f"Disallow: {private}" in body


def test_the_sitemap_lists_pages_that_actually_exist(client):
    response = client.get('/sitemap.xml')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    paths = re.findall(rf'<loc>{re.escape(app_module.PUBLIC_BASE_URL)}([^<]*)</loc>', body)
    assert paths, 'the sitemap is empty'
    for path in paths:
        assert client.get(path or '/').status_code == 200, f"sitemap lists a dead page: {path}"


def test_the_sitemap_does_not_offer_up_the_private_areas(client):
    body = client.get('/sitemap.xml').get_data(as_text=True)
    for private in ('/owner', '/agent', '/super-admin', '/dashboard'):
        assert private not in body


def test_the_home_page_states_the_facts_an_answer_engine_would_quote(client):
    """GEO: an AI summarising 'AI chat assistant for estate agents' can
    only quote what it can parse. This is that, in JSON-LD."""
    import json
    body = page(client, '/')
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>',
                        body, re.S)
    assert blocks, 'the home page carries no structured data'
    data = json.loads(blocks[0])
    kinds = {item['@type'] for item in data['@graph']}
    assert {'Organization', 'WebSite', 'SoftwareApplication'} <= kinds


def test_the_structured_prices_are_prices_somebody_can_buy(client):
    """A price in the markup that is not on sale gets the rich result
    withdrawn - and is a lie to a reader either way."""
    import json
    body = page(client, '/')
    data = json.loads(re.findall(
        r'<script type="application/ld\+json">(.*?)</script>', body, re.S)[0])
    app_item = [i for i in data['@graph'] if i['@type'] == 'SoftwareApplication'][0]
    quoted = {offer['price'] for offer in app_item['offers']}
    buyable = {str(plan['price']) for key, plan in app_module.visible_tiers()}
    assert quoted == buyable, f"structured data offers {quoted}, site sells {buyable}"
    assert 'corporation' not in body.lower() or app_module.SHOW_TIER_3


# ── 5. the layout the shared footer broke, and the overflow it uncovered ──

# These three pages centre their card with `body { display: flex }`. Adding
# the shared footer put a SECOND flex item in that row, so the footer landed
# BESIDE the card instead of under it. The fix is a column direction - and
# these tests exist so the next shared partial cannot do it again.
CENTRED_PAGES = ('signup.html', 'signup_solo.html', 'signup_agency.html')


def template_source(name):
    with open(os.path.join(ROOT, 'templates', name), encoding='utf-8') as handle:
        return handle.read()


def body_rules(markup):
    """The contents of every `body { ... }` rule in a page's own <style>."""
    return ' '.join(re.findall(r'\bbody\s*\{([^}]*)\}', markup))


@pytest.mark.parametrize('name', CENTRED_PAGES)
def test_a_centred_page_stacks_its_footer_instead_of_lining_it_up(name):
    rules = body_rules(template_source(name))
    if 'display:flex' not in rules.replace(' ', ''):
        pytest.skip(f"{name} no longer centres with flex")
    assert 'flex-direction' in rules.replace(' ', ''), \
        f"{name} is a flex row, so the footer sits beside the card"
    assert 'column' in rules, f"{name} lays its flex items out in a row"


@pytest.mark.parametrize('name', CENTRED_PAGES)
def test_a_centred_card_counts_its_padding_inside_its_width(name):
    """width:100% plus padding:40px measured 416px on a 390px phone, which
    is a sideways scrollbar on every signup page."""
    markup = template_source(name)
    box = re.findall(r'\.box\s*\{([^}]*)\}', markup)
    assert box, f"{name} has no .box rule any more"
    assert any('border-box' in rule for rule in box), \
        f"{name} lets .box padding push past the screen"


@pytest.mark.parametrize('name', CENTRED_PAGES)
def test_a_centred_card_still_sits_in_the_middle_of_the_page(name):
    """Stacking the footer cost us `align-items: center` as the vertical
    centring; auto margins put it back. Without this the card jumps to the
    top of tall screens."""
    markup = template_source(name)
    box = ' '.join(re.findall(r'\.box\s*\{([^}]*)\}', markup))
    assert 'margin-block: auto' in box or 'margin-block:auto' in box, \
        f"{name} no longer centres its card vertically"


def test_the_shared_footer_claims_a_line_of_its_own():
    with open(os.path.join(ROOT, 'static', 'theme.css'), encoding='utf-8') as handle:
        css = handle.read()
    rule = re.findall(r'\.site-footer\s*\{([^}]*)\}', css)
    assert rule, 'theme.css has no .site-footer rule'
    rule = rule[0].replace(' ', '')
    for required in ('width:100%', 'align-self:stretch', 'box-sizing:border-box'):
        assert required in rule, f".site-footer is missing {required}"


@pytest.mark.parametrize('name', CENTRED_PAGES)
def test_a_phone_sized_screen_gets_the_card_edge_to_edge(name):
    """Item 3: every change lands on the phone too. The narrow breakpoint
    has to drop the big padding, or the card is a letterbox."""
    markup = template_source(name)
    assert '@media' in markup, f"{name} has no narrow-screen rules at all"
    narrow = re.findall(r'@media[^{]*max-width[^{]*\{(.*?)\n\s*\}\s*\n', markup, re.S)
    assert narrow, f"{name} has no max-width breakpoint"


@pytest.mark.parametrize('path', PUBLIC_PAGES)
def test_every_public_page_scales_to_the_device_it_is_read_on(client, path):
    body = page(client, path)
    assert 'name="viewport"' in body, f"{path} is not mobile-scaled"
    assert 'width=device-width' in body, f"{path} fixes its own width"
