"""The move to ailuxuryleads.com: one address, set in one place.

Three things used to be hard-coded or absent, and each was a small trap:

  - the support address was typed into three public pages, so changing it
    meant a code change and a deploy;
  - emails carried no Reply-To, so a client pressing Reply was writing to
    whatever the sending service chose - which is a feedback mechanism
    nobody can read;
  - the fetcher's User-Agent named /about-bot, a page that did not exist,
    which makes a bot nobody can hold to account.

The app's own address was already a setting (PUBLIC_BASE_URL, Phase 0), so
moving domains is configuration rather than code. These tests keep it that
way.
"""
import os
import re

import pytest

import app as app_module
from acquisition.services import fetch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as handle:
        return handle.read()


# ── one address, three fallbacks ──

def test_the_support_address_falls_back_to_the_sender():
    """SUPPORT_EMAIL unset in this test run, so it should be the sender -
    or the last-resort default when there is no sender either."""
    assert app_module.SUPPORT_EMAIL
    assert app_module.SUPPORT_EMAIL == (app_module.SMTP_EMAIL
                                        or app_module.DEFAULT_SUPPORT_EMAIL)


def test_replies_go_to_the_support_address_by_default():
    assert app_module.REPLY_TO_EMAIL == app_module.SUPPORT_EMAIL


def test_the_contact_link_is_never_empty():
    """An empty mailto: on the home page is worse than no link at all."""
    assert '@' in app_module.SUPPORT_EMAIL


@pytest.mark.parametrize('page', ['index.html', 'privacy.html', 'terms.html'])
def test_the_public_pages_read_the_setting(page):
    body = read('templates', page)
    assert '{{ support_email }}' in body


def test_no_personal_address_is_left_in_the_product():
    """The owner's own mailbox must not be what clients are told to write
    to - that is what the support address is for."""
    for folder, _dirs, files in os.walk(ROOT):
        if any(part in folder for part in ('venv', '.git', '__pycache__', 'tests')):
            continue
        for name in files:
            if not name.endswith(('.py', '.html', '.js')):
                continue
            body = read(os.path.join(folder, name))
            assert 'yasar.ai.leads@gmail.com' not in body, f"{folder}/{name}"


# ── what goes out on an email ──

class Sent:
    """Catches the call to Brevo instead of making it."""

    def __init__(self):
        self.payload = None

    def __call__(self, url, headers=None, json=None, timeout=None):
        self.payload = json

        class Answer:
            status_code = 201
            text = 'ok'
        return Answer()


@pytest.fixture()
def posted(monkeypatch):
    catcher = Sent()
    monkeypatch.setenv('BREVO_API_KEY', 'test-key-not-real')
    monkeypatch.setattr(app_module.httpx, 'post', catcher)
    return catcher


def test_every_email_says_where_to_reply(posted, monkeypatch):
    monkeypatch.setattr(app_module, 'REPLY_TO_EMAIL', 'support@ailuxuryleads.com')
    assert app_module.send_email_brevo('agency@example.com', 'Hello', 'Body') is True
    assert posted.payload['replyTo'] == {'email': 'support@ailuxuryleads.com'}


def test_the_sender_name_is_a_setting(posted, monkeypatch):
    monkeypatch.setattr(app_module, 'EMAIL_FROM_NAME', 'Luxury Leads AI')
    monkeypatch.setattr(app_module, 'SMTP_EMAIL', 'support@ailuxuryleads.com')
    app_module.send_email_brevo('agency@example.com', 'Hello', 'Body')
    assert posted.payload['sender'] == {'name': 'Luxury Leads AI',
                                        'email': 'support@ailuxuryleads.com'}


def test_an_email_without_a_reply_address_still_sends(posted, monkeypatch):
    """Belt and braces: a blank setting must not break sending."""
    monkeypatch.setattr(app_module, 'REPLY_TO_EMAIL', '')
    assert app_module.send_email_brevo('agency@example.com', 'Hi', 'Body') is True
    assert 'replyTo' not in posted.payload


# ── the page the bot names ──

def test_the_fetcher_points_at_a_page_that_exists(client):
    """The User-Agent says where to complain. That address has to resolve,
    or the promise is theatre."""
    match = re.search(r'\+(\S+?)(?:;|\))', fetch.USER_AGENT)
    assert match, fetch.USER_AGENT
    path = match.group(1).split('/', 3)[-1]
    response = client.get('/' + path.lstrip('/'))
    assert response.status_code == 200


def test_that_page_needs_no_login(client):
    """A site owner who wants us to stop cannot be asked to sign up first."""
    response = client.get('/about-bot')
    assert response.status_code == 200


def test_that_page_says_how_to_stop_the_bot(client):
    body = client.get('/about-bot').get_data(as_text=True)
    assert 'robots.txt' in body
    assert 'User-agent: LuxuryLeadsAI' in body
    assert app_module.SUPPORT_EMAIL in body
    assert 'do-not-contact' in body


def test_the_fetcher_follows_the_configured_address(monkeypatch):
    """Moving to a real domain moves the bot's calling card with it."""
    monkeypatch.setenv('PUBLIC_BASE_URL', 'app.ailuxuryleads.com')
    assert fetch._public_base_url() == 'https://app.ailuxuryleads.com'
    monkeypatch.delenv('PUBLIC_BASE_URL', raising=False)
    assert fetch._public_base_url().startswith('https://')
