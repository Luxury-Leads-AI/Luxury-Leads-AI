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
