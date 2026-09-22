"""Phase 0, cycle 1 of the acquisition plan: make the existing app safe to
grow before the acquisition engine is built on top of it.

Five changes, each pinned here:

  1. PUBLIC_BASE_URL - one setting for every link the app writes. Before,
     the Render address was typed out 16 times in app.py and 5 times in
     templates, so moving to a custom domain meant 22 edits and one missed
     link in an email.
  2. The SECRET_KEY guard - on Render the app refuses to start with the
     fallback key that is written in app.py (anyone who read it could forge
     a super admin session).
  3. Rate limits - /chat (every message is an OpenAI call), /create-agency,
     the three logins and forgot-password each get a per-visitor ceiling,
     and a blocked request gets an answer its caller can show.
  4. requirements.txt is plain UTF-8, so it can be edited safely.
  5. The chat widget tells visitors they are talking to an AI (EU AI Act,
     Article 50, in force since 2 August 2026).
"""
import os
import re
import subprocess
import sys
import tempfile

import pytest

import app as app_module

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD_URL = 'https://luxury-leads-ai.onrender.com'


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as fh:
        return fh.read()


def boot_app(env_overrides):
    """Import app.py in a fresh Python process with the given environment,
    and report what happened. This is the only honest way to test code that
    runs at import time (the SECRET_KEY guard, reading PUBLIC_BASE_URL).

    Every variable app.py reads is set explicitly, so a developer's own .env
    file (which load_dotenv would otherwise read) can't change the result -
    load_dotenv never overrides a variable that is already set, even to ''.

    The child is told to write UTF-8. app.py prints emoji while it starts
    (the migration lines), and on Windows a Python whose output goes into a
    pipe instead of a console writes in cp1252, which has no emoji - so the
    child crashed on its first print. That only ever affected this helper:
    Render's logs are UTF-8, and a Windows console handles emoji itself.
    """
    fd, db_path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    env = dict(os.environ)
    env.update({
        'OPENAI_API_KEY': 'test-dummy-key-not-real',
        'DATABASE_URL': f'sqlite:///{db_path}',
        'SUPER_ADMIN_PASSWORD': 'test-super-admin-pw',
        'SECRET_KEY': 'a-perfectly-good-test-secret-key-0123456789',
        'RENDER': '',
        'PUBLIC_BASE_URL': '',
        'PYTHONIOENCODING': 'utf-8',
        'PYTHONUTF8': '1',
    })
    env.update(env_overrides)
    code = ("import app; "
            "print('BASE=' + app.PUBLIC_BASE_URL); "
            "print('OWNER=' + app.LOGIN_URLS['owner']); "
            "print('AGENT=' + app.LOGIN_URLS['agent'])")
    try:
        return subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env,
                              capture_output=True, encoding='utf-8',
                              errors='replace', timeout=120)
    finally:
        try:
            os.remove(db_path)
        except OSError:
            pass


# ─────────────────────────────────────────────────────────────
# 1. PUBLIC_BASE_URL
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('raw, expected', [
    (None, OLD_URL),
    ('', OLD_URL),
    ('   ', OLD_URL),
    ('https://app.example.com', 'https://app.example.com'),
    ('https://app.example.com/', 'https://app.example.com'),
    ('app.example.com', 'https://app.example.com'),
    ('  https://app.example.com//  ', 'https://app.example.com'),
    ('http://localhost:10000', 'http://localhost:10000'),
])
def test_normalize_base_url(raw, expected):
    assert app_module.normalize_base_url(raw) == expected


def test_the_render_address_is_written_down_exactly_once_in_app_py():
    """Everything else reads PUBLIC_BASE_URL. A second copy of the address
    is a link that won't move when the domain does."""
    src = read('app.py')
    assert src.count(OLD_URL) == 1
    assert f"DEFAULT_PUBLIC_BASE_URL = '{OLD_URL}'" in src


def test_no_template_hard_codes_the_render_address():
    folder = os.path.join(ROOT, 'templates')
    offenders = [name for name in sorted(os.listdir(folder))
                 if name.endswith('.html') and OLD_URL in read('templates', name)]
    assert offenders == []


def test_every_placeholder_sits_inside_an_f_string():
    """'{PUBLIC_BASE_URL}' in a plain string would be emailed literally."""
    import ast
    tree = ast.parse(read('app.py'))
    plain = [node.lineno for node in ast.walk(tree)
             if isinstance(node, ast.Constant) and isinstance(node.value, str)
             and '{PUBLIC_BASE_URL}' in node.value]
    assert plain == []


def test_templates_can_read_the_setting():
    assert app_module.app.jinja_env.globals['public_base_url'] == app_module.PUBLIC_BASE_URL


@pytest.mark.parametrize('page', ['/signup/solo', '/signup/agency'])
def test_signup_pages_show_embed_code_on_the_configured_address(client, page):
    html = client.get(page).get_data(as_text=True)
    assert f'{app_module.PUBLIC_BASE_URL}/static/widget.js' in html
    assert '{{' not in html  # the placeholder was rendered, not printed


def test_the_feedback_page_loads_the_widget_from_its_own_server():
    """That page is served by this app, so a relative path is always right,
    whatever the domain is."""
    src = read('templates', 'appointment_feedback.html')
    assert 'src="/static/widget.js"' in src


def test_emails_use_the_setting_at_send_time(monkeypatch):
    sent = {}
    monkeypatch.setattr(app_module, 'PUBLIC_BASE_URL', 'https://app.example.com')
    monkeypatch.setattr(app_module, 'send_email_brevo',
                        lambda to, subject, body: sent.update(body=body) or True)

    class Obj:
        pass
    agency, appt = Obj(), Obj()
    agency.name = 'Test Realty'
    appt.customer_name, appt.customer_email = 'Sam', 'sam@example.test'
    appt.property_interest, appt.appointment_date, appt.appointment_time = 'Villa', '2026-10-01', '2:00 PM'

    app_module.send_appointment_checkin_email(agency, appt, 'tok123')
    assert 'https://app.example.com/appointment-feedback/tok123?choice=buy' in sent['body']
    assert OLD_URL not in sent['body']


def test_setting_the_variable_on_render_moves_every_login_link():
    """The real path: the variable is set in Render, the app restarts."""
    result = boot_app({'PUBLIC_BASE_URL': 'app.example.com/'})
    assert result.returncode == 0, result.stderr[-2000:]
    assert 'BASE=https://app.example.com' in result.stdout
    assert 'OWNER=https://app.example.com/owner-login' in result.stdout
    assert 'AGENT=https://app.example.com/agent-login' in result.stdout


def test_without_the_variable_nothing_changes():
    result = boot_app({})
    assert result.returncode == 0, result.stderr[-2000:]
    assert f'BASE={OLD_URL}' in result.stdout
    assert f'OWNER={OLD_URL}/owner-login' in result.stdout


# ─────────────────────────────────────────────────────────────
# 2. SECRET_KEY guard
# ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize('key', [None, '', '   ', 'change-this-in-production'])
def test_the_fallback_key_is_refused_on_render(key):
    assert app_module.secret_key_problem(key, on_render=True)


@pytest.mark.parametrize('key', [None, '', 'change-this-in-production'])
def test_the_fallback_key_is_fine_on_your_own_computer(key):
    assert app_module.secret_key_problem(key, on_render=False) is None


def test_a_real_key_is_accepted_on_render():
    assert app_module.secret_key_problem('x' * 48, on_render=True) is None


def test_a_short_real_key_still_boots():
    """A short key only earns a warning in the log. Refusing it would take
    the live site down over something that is not an emergency."""
    assert app_module.secret_key_problem('short-but-set', on_render=True) is None
    result = boot_app({'RENDER': 'true', 'SECRET_KEY': 'short-but-set'})
    assert result.returncode == 0, result.stderr[-2000:]
    assert 'shorter than 32' in result.stdout


def test_render_refuses_to_start_without_a_secret_key():
    result = boot_app({'RENDER': 'true', 'SECRET_KEY': ''})
    assert result.returncode != 0
    assert 'SECRET_KEY is not set on Render' in result.stderr


def test_render_refuses_to_start_with_the_fallback_key():
    result = boot_app({'RENDER': 'true', 'SECRET_KEY': 'change-this-in-production'})
    assert result.returncode != 0
    assert 'SECRET_KEY is not set on Render' in result.stderr


def test_render_starts_normally_with_a_real_key():
    result = boot_app({'RENDER': 'true'})
    assert result.returncode == 0, result.stderr[-2000:]


# ─────────────────────────────────────────────────────────────
# 3. Rate limits
# ─────────────────────────────────────────────────────────────

def ip(addr):
    return {'X-Forwarded-For': addr}


def test_the_visitor_address_is_the_first_forwarded_entry():
    """Render puts the real client first; later entries are proxies."""
    with app_module.app.test_request_context(
            '/', headers={'X-Forwarded-For': '203.0.113.7, 10.0.0.1'},
            environ_base={'REMOTE_ADDR': '10.0.0.2'}):
        assert app_module.client_ip() == '203.0.113.7'
    with app_module.app.test_request_context(
            '/', environ_base={'REMOTE_ADDR': '198.51.100.4'}):
        assert app_module.client_ip() == '198.51.100.4'


def test_limits_are_kept_in_this_process_memory():
    assert app_module.limiter._storage_uri == 'memory://'


def test_chat_blocks_the_21st_message_in_a_minute_with_a_reply_the_widget_shows(client):
    # An empty message is answered with a 400 before any OpenAI call, and
    # still counts against the limit - so this test costs nothing.
    for _ in range(20):
        assert client.post('/chat', json={'message': '', 'agency_id': 1},
                           headers=ip('203.0.113.10')).status_code == 400
    blocked = client.post('/chat', json={'message': '', 'agency_id': 1},
                          headers=ip('203.0.113.10'))
    assert blocked.status_code == 429
    body = blocked.get_json()
    assert body['error'] == 'rate_limited'
    assert 'wait a minute' in body['reply']
    # the widget runs on the agency's own site, so the answer must be
    # readable cross-origin like any other /chat response
    assert blocked.headers.get('Access-Control-Allow-Origin') == '*'


def test_chat_limits_are_per_visitor(client):
    for _ in range(21):
        client.post('/chat', json={'message': '', 'agency_id': 1}, headers=ip('203.0.113.11'))
    other = client.post('/chat', json={'message': '', 'agency_id': 1}, headers=ip('203.0.113.12'))
    assert other.status_code == 400  # a different visitor is unaffected


def test_browser_preflight_requests_do_not_count(client):
    for _ in range(40):
        client.options('/chat', headers=ip('203.0.113.13'))
    first_real = client.post('/chat', json={'message': '', 'agency_id': 1},
                             headers=ip('203.0.113.13'))
    assert first_real.status_code == 400


def test_signup_is_limited_to_five_an_hour(client):
    for _ in range(5):
        # missing name/email: rejected with 400 before anything is created
        assert client.post('/create-agency', json={},
                           headers=ip('203.0.113.20')).status_code == 400
    blocked = client.post('/create-agency', json={}, headers=ip('203.0.113.20'))
    assert blocked.status_code == 429
    assert 'Too many sign-up attempts' in blocked.get_json()['error']


def test_the_super_admin_is_never_limited_when_creating_agencies(client):
    with client.session_transaction() as sess:
        sess['super_admin'] = True
    for _ in range(8):
        assert client.post('/create-agency', json={},
                           headers=ip('203.0.113.21')).status_code == 400


@pytest.mark.parametrize('path, form, allowed', [
    ('/owner-login', {'agency_id': '999999', 'password': 'wrong'}, 10),
    ('/agent-login', {'email': 'nobody@example.test', 'password': 'wrong'}, 10),
    ('/super-admin-login', {'password': 'wrong'}, 5),
])
def test_logins_lock_out_after_repeated_attempts(client, path, form, allowed):
    for _ in range(allowed):
        r = client.post(path, data=form, headers=ip('203.0.113.30'))
        assert r.status_code == 302
        assert 'Too+many' not in r.headers['Location']
    blocked = client.post(path, data=form, headers=ip('203.0.113.30'))
    assert blocked.status_code == 302
    assert blocked.headers['Location'].startswith(f'{path}?error=Too+many+attempts')


def test_login_pages_themselves_are_never_limited(client):
    for _ in range(30):
        assert client.get('/super-admin-login', headers=ip('203.0.113.31')).status_code == 200


def test_a_locked_out_visitor_does_not_lock_out_the_real_super_admin(client):
    for _ in range(6):
        client.post('/super-admin-login', data={'password': 'wrong'}, headers=ip('203.0.113.32'))
    real = client.post('/super-admin-login', data={'password': 'test-super-admin-pw'},
                       headers=ip('198.51.100.9'))
    assert real.status_code == 302 and real.headers['Location'] == '/owner'


def test_password_reset_requests_are_limited(client):
    for _ in range(5):
        assert client.post('/forgot-password', data={'email': ''},
                           headers=ip('203.0.113.40')).status_code == 200
    blocked = client.post('/forgot-password', data={'email': ''}, headers=ip('203.0.113.40'))
    assert blocked.status_code == 429
    assert 'Too many reset requests' in blocked.get_data(as_text=True)


# ─────────────────────────────────────────────────────────────
# 4. requirements.txt
# ─────────────────────────────────────────────────────────────

def test_requirements_is_plain_utf8():
    raw = open(os.path.join(ROOT, 'requirements.txt'), 'rb').read()
    assert b'\x00' not in raw, "requirements.txt is UTF-16 again"
    assert not raw.startswith(b'\xef\xbb\xbf'), "requirements.txt has a UTF-8 BOM"
    raw.decode('utf-8')


def test_every_requirement_is_pinned():
    lines = [ln.strip() for ln in read('requirements.txt').splitlines() if ln.strip()]
    for line in lines:
        assert re.fullmatch(r'[A-Za-z0-9_.\-]+==[0-9][0-9A-Za-z.\-]*', line), line
    names = {ln.split('==')[0].lower() for ln in lines}
    assert {'flask', 'flask-limiter', 'limits', 'openai'} <= names


def test_openai_stays_on_the_version_the_chatbot_was_built_on():
    assert 'openai==1.54.4' in read('requirements.txt')


# ─────────────────────────────────────────────────────────────
# 5. The widget: AI disclosure and where it finds the server
# ─────────────────────────────────────────────────────────────

def test_the_widget_says_it_is_an_ai_in_the_header_and_the_first_line():
    js = read('static', 'widget.js')
    assert 'id="chat-ai-label"' in js and '${esc(disclosure.label)}' in js
    assert 'id="chat-ai-disclosure"' in js and '${esc(disclosure.note)}' in js
    # the disclosure sits inside the message list, above anything said
    messages = js.index('id="chat-messages"')
    assert messages < js.index('id="chat-ai-disclosure"') < js.index('id="typing-indicator"')


def test_the_disclosure_speaks_the_languages_of_the_planned_markets():
    js = read('static', 'widget.js')
    block = js[js.index('const DISCLOSURES'):js.index('function pickDisclosure')]
    for lang in ('"en"', '"fr"', '"es"', '"pt"', '"de"', '"it"', '"nl"', '"ar"'):
        assert lang in block, f"no disclosure for {lang}"
    assert 'return DISCLOSURES.en;' in js  # anything else falls back to English


def test_agency_names_are_escaped_before_they_reach_the_page():
    js = read('static', 'widget.js')
    assert '${info.assistant}' not in js and '${info.agency}' not in js
    assert '${esc(info.assistant)}' in js and '${esc(info.agency)}' in js


def test_the_widget_finds_its_server_from_its_own_address():
    js = read('static', 'widget.js')
    assert 'new URL(script.src).origin' in js
    assert 'data-base-url' in js
    assert js.count(OLD_URL) == 1  # only as the last-resort default
    assert 'fetch(`${BASE_URL}/chat`' in js
