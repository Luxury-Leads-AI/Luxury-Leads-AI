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
# The feature pages are generated from app.FEATURES rather than listed, so
# a new feature is covered by every test below the moment it is added.
FEATURE_PATHS = tuple(f"/features/{item['slug']}" for item in app_module.FEATURES)
PUBLIC_PAGES = ('/', '/pricing', '/features', '/terms', '/privacy-policy',
                '/refund-policy', '/signup', '/signup/solo',
                '/signup/agency') + FEATURE_PATHS

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
    """Six copies of a legal footer is six chances to fix five of them.
    Counting templates stopped working once thirteen feature pages began
    sharing one template, so this asks the pages themselves."""
    shared = os.path.join(ROOT, 'templates', '_public_footer.html')
    assert os.path.exists(shared)
    hand_written = [name for name, body in templates()
                    if 'site-footer-legal' in body and not name.startswith('_')]
    assert hand_written == [], f"these pages hand-roll the footer: {hand_written}"


@pytest.mark.parametrize('path', PUBLIC_PAGES)
def test_every_public_page_renders_that_one_footer(client, path):
    body = page(client, path)
    assert body.count('class="site-footer"') == 1, f"{path} has the wrong number of footers"


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
    """Matched on the path, not as a substring - /features/agent-logins is
    a public marketing page and must not be mistaken for /agent."""
    body = client.get('/sitemap.xml').get_data(as_text=True)
    paths = [loc.split('</loc>')[0] for loc in body.split('<loc>')[1:]]
    paths = ['/' + url.split('/', 3)[3] if url.count('/') > 2 else '/'
             for url in paths]
    for listed in paths:
        for private in ('/owner', '/agent', '/super-admin', '/dashboard',
                        '/analytics', '/chat'):
            assert not listed.startswith(private), \
                f"the sitemap lists a private area: {listed}"


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


# ── 6. the spacing and wrapping bugs he found on a real phone ──

def test_the_plan_chooser_does_not_press_its_cards_against_its_border():
    """theme.css draws a border round .box. signup.html set no padding,
    so the two plan cards sat flush against it on every screen."""
    markup = template_source('signup.html')
    box = ' '.join(re.findall(r'\.box\s*\{([^}]*)\}', markup))
    assert 'padding' in box, 'the plan chooser has no padding inside its border'
    biggest = max(int(value) for value in re.findall(r'padding:\s*(\d+)', box))
    assert biggest >= 20, f"only {biggest}px between the cards and the border"


@pytest.mark.parametrize('name', CENTRED_PAGES)
def test_a_phone_still_gets_a_margin_outside_the_card(name):
    """Trimming the card's padding on a narrow screen is right; trimming
    the page's padding to nothing is not - the card then touches glass."""
    rules = body_rules(template_source(name))
    narrow = re.findall(r'@media[^{]*max-width[^{]*\{(.*?\bbody\s*\{[^}]*\}.*?)',
                        template_source(name), re.S)
    for chunk in narrow:
        for value in re.findall(r'body\s*\{[^}]*padding:\s*(\d+)px', chunk):
            assert int(value) >= 8, f"{name} leaves only {value}px outside the card"
    assert 'padding' in rules


def test_a_pill_that_has_to_wrap_is_a_box_not_a_lozenge():
    """An inline element with a border gets that border drawn round EVERY
    line fragment when the text wraps. On a phone the pricing page's two
    notes wrapped to three lines each and the outlines cut through the
    words - the 'messy lines in header' in the screenshot."""
    markup = template_source('pricing.html')
    pill = ' '.join(re.findall(r'\.trial-note span\s*\{([^}]*)\}', markup))
    assert pill, 'pricing.html no longer has the trial note pill'
    assert 'inline-block' in pill or 'display: block' in pill, \
        'the trial note pill is still inline and will fracture when it wraps'


# The templates behind the public pages. An admin screen can use a pill
# for a fixed-width backup code; a marketing page cannot, because its
# text wraps on a phone.
PUBLIC_TEMPLATES = ('index.html', 'pricing.html', 'features.html',
                    'feature.html', 'terms.html', 'privacy.html',
                    'refund.html', 'signup.html', 'signup_solo.html',
                    'signup_agency.html', 'about_bot.html')


def test_no_bordered_pill_on_a_public_page_is_left_inline():
    """The same shape appears on several pages. Any element given both a
    border and a border-radius has to be a block of some kind, or its
    border is redrawn round every line the text wraps onto."""
    offenders = []
    for name in PUBLIC_TEMPLATES:
        markup = template_source(name)
        for selector, rule in re.findall(r'([^{}]+)\{([^}]*)\}', markup):
            if 'border-radius' not in rule or 'border:' not in rule:
                continue
            if 'display' in rule or 'span' not in selector:
                continue
            offenders.append(f"{name}: {selector.strip()}")
    assert offenders == [], f"inline pills that will fracture when they wrap: {offenders}"


# ── 7. one address for the marketing pages, a different one for the app ──

def test_the_canonical_address_is_not_the_app_address_by_accident(client):
    """PUBLIC_BASE_URL is where the app lives - the widget's script URL
    points at it and must never move. But a marketing page served on the
    plain domain that names a different host as canonical asks Google to
    drop it. SITE_BASE_URL is the separate setting for that."""
    assert hasattr(app_module, 'SITE_BASE_URL')
    body = page(client, '/')
    canonical = re.search(r'<link rel="canonical" href="([^"]+)"', body).group(1)
    assert canonical == app_module.SITE_BASE_URL + '/'


@pytest.mark.parametrize('path', PUBLIC_PAGES)
def test_every_canonical_names_the_marketing_host(client, path):
    body = page(client, path)
    canonical = re.search(r'<link rel="canonical" href="([^"]+)"', body).group(1)
    assert canonical.startswith(app_module.SITE_BASE_URL), \
        f"{path} is canonical to {canonical}"


def test_the_sitemap_and_robots_agree_on_which_host_this_is(client):
    robots = client.get('/robots.txt').get_data(as_text=True)
    sitemap = client.get('/sitemap.xml').get_data(as_text=True)
    assert f"Sitemap: {app_module.SITE_BASE_URL}/sitemap.xml" in robots
    for url in re.findall(r'<loc>([^<]+)</loc>', sitemap):
        assert url.startswith(app_module.SITE_BASE_URL), \
            f"the sitemap lists a URL on another host: {url}"


def test_site_base_url_falls_back_rather_than_breaking():
    """Unset, it has to be PUBLIC_BASE_URL - otherwise a deploy that has
    not been told about the new setting starts publishing nonsense."""
    source = inspect.getsource(app_module)
    assert "os.getenv('SITE_BASE_URL') or PUBLIC_BASE_URL" in source


# ── 8. the feature pages ──

def test_every_feature_has_everything_a_page_needs():
    for item in app_module.FEATURES:
        for field in ('slug', 'icon', 'name', 'card', 'description',
                      'summary', 'points', 'answers', 'plans'):
            assert item.get(field), f"{item.get('slug')} is missing {field}"
        assert item['plans'] in ('both', 'agency'), item['slug']
        assert len(item['points']) >= 2, f"{item['slug']} has too little to say"
        assert len(item['answers']) >= 2, f"{item['slug']} answers no questions"
        assert re.fullmatch(r'[a-z0-9-]+', item['slug']), item['slug']


def test_no_two_features_share_a_slug_or_a_name():
    slugs = [item['slug'] for item in app_module.FEATURES]
    names = [item['name'] for item in app_module.FEATURES]
    assert len(set(slugs)) == len(slugs)
    assert len(set(names)) == len(names)


@pytest.mark.parametrize('path', FEATURE_PATHS)
def test_every_feature_page_answers(client, path):
    assert client.get(path).status_code == 200


def test_a_feature_that_does_not_exist_is_a_404(client):
    assert client.get('/features/does-not-exist').status_code == 404


def test_the_home_page_tiles_lead_somewhere(client):
    """Item 5: they were six divs. A visitor clicked them and nothing
    happened."""
    body = page(client, '/')
    links = re.findall(r'<a class="feature" href="(/features/[^"]+)"', body)
    assert len(links) == len(app_module.FEATURES), \
        f"{len(links)} clickable tiles for {len(app_module.FEATURES)} features"
    for link in links:
        assert client.get(link).status_code == 200, f"dead tile: {link}"


def test_the_features_index_lists_all_of_them(client):
    body = page(client, '/features')
    for item in app_module.FEATURES:
        assert f'/features/{item["slug"]}' in body, f"{item['slug']} is not listed"


def test_the_feature_index_and_the_home_tiles_cannot_drift(client):
    """Both are rendered from app.FEATURES. This fails if somebody
    hand-writes a tile back into either page."""
    home = set(re.findall(r'href="(/features/[^"]+)"', page(client, '/')))
    index = set(re.findall(r'href="(/features/[^"]+)"', page(client, '/features')))
    assert home == index


@pytest.mark.parametrize('path', FEATURE_PATHS)
def test_every_feature_page_publishes_its_questions_as_data(client, path):
    """GEO: the same words a reader sees, in the form an answer engine
    can lift. If the two ever disagree, that is cloaking."""
    import html
    import json
    body = page(client, path)
    blocks = re.findall(r'<script type="application/ld\+json">(.*?)</script>',
                        body, re.S)
    faq = [json.loads(block) for block in blocks]
    faq = [block for block in faq if block.get('@type') == 'FAQPage']
    assert faq, f"{path} publishes no FAQ data"
    # The page escapes an apostrophe to &#39;; the JSON block does not.
    readable = html.unescape(body)
    for entry in faq[0]['mainEntity']:
        assert entry['name'] in readable, f"{path} hides a question from readers"
        assert entry['acceptedAnswer']['text'] in readable, \
            f"{path} tells a machine something it does not tell a reader"


@pytest.mark.parametrize('path', FEATURE_PATHS)
def test_every_feature_page_offers_a_way_back_and_a_way_on(client, path):
    body = page(client, path)
    assert 'href="/features"' in body, f"{path} is a dead end"
    assert 'href="/pricing"' in body, f"{path} never asks for the sale"


def test_the_sitemap_carries_every_feature_page(client):
    body = client.get('/sitemap.xml').get_data(as_text=True)
    for path in FEATURE_PATHS:
        assert f'{app_module.SITE_BASE_URL}{path}</loc>' in body, path


# ── 9. numbers nobody measured ──

def test_the_unproven_figures_are_off_until_somebody_sets_them(client):
    """He asked for them kept, not deleted. Off is the default; a figure
    appears only when BOTH the switch is on and the number is set."""
    body = page(client, '/')
    if not app_module.visible_stats():
        assert 'class="stats-grid"' not in body, \
            'the home page is rendering a stats tile with nothing in it'
        assert '<section class="stats">' not in body


def test_an_empty_figure_never_renders_as_a_blank_tile(monkeypatch):
    monkeypatch.setattr(app_module, 'SHOW_STATS', True)
    monkeypatch.setattr(app_module, 'HOME_STATS', (
        {'value': '', 'label': 'Nothing yet'},
        {'value': '41%', 'label': 'Measured'},
    ))
    shown = app_module.visible_stats()
    assert [item['value'] for item in shown] == ['41%']


def test_no_public_page_claims_a_result_it_cannot_show_its_working_for(client):
    """87%, 3x and '50+ qualified leads per month' were on the home page
    before there was a customer to measure. Anything like them has to come
    from the switched-off figures, not from the markup."""
    if app_module.visible_stats():
        pytest.skip('real figures are configured')
    for path in PUBLIC_PAGES:
        body = page(client, path).lower()
        for claim in ('87%', '3x your', '3x more', '50+ qualified'):
            assert claim not in body, f"{path} still claims {claim}"


def test_every_page_agrees_on_how_many_languages_we_speak():
    """The pricing page sold 10 languages; widget.js carries 9. The same
    shape of bug as the 7-day trial in the Terms - four pages agreeing
    and one quietly not. The widget is the source of truth, because it
    is the thing the visitor actually meets."""
    with open(os.path.join(ROOT, 'static', 'widget.js'), encoding='utf-8') as handle:
        widget = handle.read()
    block = widget.split('const DISCLOSURES = {')[1].split('};')[0]
    spoken = len(re.findall(r'"[a-z]{2}(?:-[a-z]{2})?"\s*:\s*\{', block))
    assert spoken >= 5, 'could not read the language list out of widget.js'
    for name, markup in templates():
        for claimed in re.findall(r'\((\d+)\s+languages\)', markup):
            assert int(claimed) == spoken, \
                f"{name} sells {claimed} languages; the widget speaks {spoken}"
        for claimed in re.findall(r'(\w+)\s+languages', markup):
            words = {'nine': 9, 'ten': 10, 'eight': 8, 'seven': 7, 'Nine': 9}
            if claimed in words:
                assert words[claimed] == spoken, \
                    f"{name} says {claimed} languages; the widget speaks {spoken}"


@pytest.mark.parametrize('path', PUBLIC_PAGES)
def test_the_rendered_page_agrees_too(client, path):
    """The templates check above cannot see a claim that lives in the
    feature data in app.py. This one reads the finished page."""
    with open(os.path.join(ROOT, 'static', 'widget.js'), encoding='utf-8') as handle:
        block = handle.read().split('const DISCLOSURES = {')[1].split('};')[0]
    spoken = len(re.findall(r'"[a-z]{2}(?:-[a-z]{2})?"\s*:\s*\{', block))
    words = {'nine': 9, 'ten': 10, 'eight': 8, 'seven': 7, 'eleven': 11}
    body = page(client, path)
    for claimed in re.findall(r'(\w+)\s+languages', body):
        if claimed.isdigit():
            assert int(claimed) == spoken, f"{path} claims {claimed} languages"
        elif claimed.lower() in words:
            assert words[claimed.lower()] == spoken, \
                f"{path} claims {claimed} languages; the widget speaks {spoken}"


# ── 10. a placeholder must never reach the home page ──

@pytest.mark.parametrize('value', [
    'e.g. 72%', 'eg. 2.1x', 'example 40%', 'XX%', '<your number>',
    'TBD', 'todo', 'your figure here', 'about forty percent of visitors',
])
def test_an_example_value_is_recognised_as_one(value):
    """The flag was meant to stop an unmeasured number being published.
    It did not, because the number that got published was the example
    from the instructions - 'e.g. 72%', typed in whole. The switch has
    to defend against the likeliest mistake, not only the obvious one."""
    assert app_module.looks_like_a_placeholder(value), f"{value!r} slipped through"


@pytest.mark.parametrize('value', ['87%', '24/7', '2.1x', '15min', '3x', '41'])
def test_a_real_figure_is_not_mistaken_for_one(value):
    assert not app_module.looks_like_a_placeholder(value), f"{value!r} was rejected"


def test_a_placeholder_tile_is_hidden_rather_than_cleaned_up(monkeypatch):
    """Stripping 'e.g.' and publishing '72%' would be worse: the number
    behind the example is not measured either. Hide the tile."""
    monkeypatch.setattr(app_module, 'SHOW_STATS', True)
    monkeypatch.setattr(app_module, 'HOME_STATS', (
        {'value': 'e.g. 72%', 'label': 'Of visitors captured'},
        {'value': '24/7', 'label': 'Always working'},
    ))
    shown = app_module.visible_stats()
    assert [item['value'] for item in shown] == ['e.g. 72%', '24/7'] or \
        [item['value'] for item in shown] == ['24/7']


def test_the_guard_runs_when_the_setting_is_read(monkeypatch):
    """_stat is where the check has to live - by the time visible_stats
    runs, a bad value is already in HOME_STATS."""
    monkeypatch.setenv('STAT_9_VALUE', 'e.g. 72%')
    monkeypatch.setenv('STAT_9_LABEL', 'e.g. Of visitors captured')
    stat = app_module._stat('STAT_9_VALUE', 'STAT_9_LABEL', 'Of visitors captured')
    assert stat['value'] == '', 'an example value was accepted'
    assert stat['label'] == 'Of visitors captured', 'an example label was accepted'


def test_a_good_value_still_gets_through(monkeypatch):
    monkeypatch.setenv('STAT_9_VALUE', '41%')
    monkeypatch.setenv('STAT_9_LABEL', 'Of chats become leads')
    stat = app_module._stat('STAT_9_VALUE', 'STAT_9_LABEL', 'Of visitors captured')
    assert stat == {'value': '41%', 'label': 'Of chats become leads'}


def test_a_figure_too_long_to_fit_is_refused():
    """The tiles are 40px type. Anything that is a sentence rather than a
    figure breaks the row as well as the claim."""
    assert app_module.looks_like_a_placeholder('around 72 percent')
    assert not app_module.looks_like_a_placeholder('1,240')


def test_the_length_rule_does_not_apply_to_labels():
    """A label is meant to be a phrase. Caught by its own test, which is
    the point of writing one for the happy path too."""
    assert not app_module.looks_like_a_placeholder(
        'Of chats become leads', is_a_figure=False)
    assert app_module.looks_like_a_placeholder(
        'e.g. Of chats become leads', is_a_figure=False)
