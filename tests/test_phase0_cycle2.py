"""Phase 0, cycle 2: lock the Super Admin panel, and pull out the two
functions the acquisition engine will call.

Until now one password in a Render setting, compared with `==`, was the only
thing between a stranger and every agency's data. This cycle:

  1. The password is checked against a hash, in constant time. The plain
     setting still works as a fallback so a deploy can't lock Moaz out.
  2. A second step: a 6-digit code from an authenticator app, set up from a
     page in the panel, with 8 one-time backup codes for a lost phone.
  3. An audit log: sign-ins, failures, two-factor changes, agencies created
     and deleted - each with a time and an address.
  4. provision_agency() - the single door into the SaaS that the engine will
     use to turn a pilot into a live agency - and is_entitled(), the single
     answer to "may this agency use the product?".
  5. A smoke test: prove the chat works for a new agency without paying for
     an OpenAI call.
"""
import os
import re
import subprocess
import sys
from datetime import datetime

import pyotp
import pytest
from werkzeug.security import check_password_hash, generate_password_hash

import app as app_module

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
db = app_module.db

PASSWORD = 'a-good-long-test-password'
HASH = generate_password_hash(PASSWORD)


def read(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as fh:
        return fh.read()


@pytest.fixture(autouse=True)
def _clean_admin_tables():
    """Each test starts with two-factor off and an empty audit log."""
    for model in (app_module.AdminBackupCode, app_module.AdminAudit,
                  app_module.AdminSecurity):
        model.query.delete()
    db.session.commit()
    yield
    db.session.rollback()


@pytest.fixture()
def hashed_password(monkeypatch):
    monkeypatch.setattr(app_module, 'SUPER_ADMIN_PASSWORD_HASH', HASH)
    monkeypatch.setattr(app_module, 'SUPER_ADMIN_PASSWORD', '')
    return PASSWORD


def turn_2fa_on():
    """The end state of the setup page, without going through it."""
    row = app_module.admin_security_row(create=True)
    row.totp_secret = pyotp.random_base32()
    row.confirmed_at = datetime.utcnow()
    row.last_step = None
    db.session.commit()
    return row.totp_secret


def current_code(secret):
    return pyotp.TOTP(secret).now()


def ip(addr='203.0.113.200'):
    return {'X-Forwarded-For': addr}


def actions():
    return [row.action for row in app_module.AdminAudit.query.order_by(
        app_module.AdminAudit.id).all()]


# ─────────────────────────────────────────────────────────────
# 1. The password
# ─────────────────────────────────────────────────────────────

def test_the_password_is_checked_against_the_hash(hashed_password):
    assert app_module.check_super_admin_password(PASSWORD)
    assert not app_module.check_super_admin_password('wrong')
    assert not app_module.check_super_admin_password('')
    assert not app_module.check_super_admin_password(None)


def test_the_plain_setting_still_works_until_the_hash_is_set(monkeypatch):
    """Deploying this must never lock him out of his own panel."""
    monkeypatch.setattr(app_module, 'SUPER_ADMIN_PASSWORD_HASH', '')
    monkeypatch.setattr(app_module, 'SUPER_ADMIN_PASSWORD', 'old-plain-password')
    assert app_module.check_super_admin_password('old-plain-password')
    assert not app_module.check_super_admin_password('nope')


def test_the_hash_wins_when_both_are_set(monkeypatch):
    monkeypatch.setattr(app_module, 'SUPER_ADMIN_PASSWORD_HASH', HASH)
    monkeypatch.setattr(app_module, 'SUPER_ADMIN_PASSWORD', 'the-old-one')
    assert app_module.check_super_admin_password(PASSWORD)
    assert not app_module.check_super_admin_password('the-old-one')


def test_nothing_configured_means_nobody_gets_in(monkeypatch):
    monkeypatch.setattr(app_module, 'SUPER_ADMIN_PASSWORD_HASH', '')
    monkeypatch.setattr(app_module, 'SUPER_ADMIN_PASSWORD', '')
    assert not app_module.check_super_admin_password('')
    assert not app_module.check_super_admin_password('anything')


def test_a_broken_hash_refuses_everyone_instead_of_crashing(monkeypatch):
    monkeypatch.setattr(app_module, 'SUPER_ADMIN_PASSWORD_HASH', 'not-a-hash')
    monkeypatch.setattr(app_module, 'SUPER_ADMIN_PASSWORD', '')
    assert not app_module.check_super_admin_password('anything')


def test_the_old_equals_comparison_is_gone():
    """`==` on a secret answers a little faster the further it gets, which
    is enough to guess it character by character."""
    src = read('app.py')
    assert 'password == SUPER_ADMIN_PASSWORD' not in src
    assert 'secrets.compare_digest' in src


def test_the_hash_tool_prints_a_hash_that_actually_matches():
    result = subprocess.run(
        [sys.executable, os.path.join('tools', 'make_admin_hash.py')],
        input='a-password-long-enough\na-password-long-enough\n',
        cwd=ROOT, capture_output=True, encoding='utf-8', errors='replace',
        timeout=120)
    assert result.returncode == 0, result.stderr[-1000:]
    printed = [line.strip() for line in result.stdout.splitlines()
               if line.strip() and ':' in line and '$' in line]
    assert printed, result.stdout
    assert check_password_hash(printed[-1], 'a-password-long-enough')
    assert 'a-password-long-enough' not in result.stdout  # never echoed back


@pytest.mark.parametrize('typed, reason', [
    ('short\nshort\n', 'too short'),
    ('a-password-long-enough\na-different-one\n', 'they did not match'),
])
def test_the_hash_tool_refuses_a_bad_password(typed, reason):
    result = subprocess.run(
        [sys.executable, os.path.join('tools', 'make_admin_hash.py')],
        input=typed, cwd=ROOT, capture_output=True, encoding='utf-8',
        errors='replace', timeout=120)
    assert result.returncode != 0, reason


# ─────────────────────────────────────────────────────────────
# 2. Two-factor
# ─────────────────────────────────────────────────────────────

def test_two_factor_starts_off_and_the_password_alone_gets_in(client, hashed_password):
    assert app_module.admin_2fa_is_on() is False
    r = client.post('/super-admin-login', data={'password': PASSWORD}, headers=ip())
    assert r.headers['Location'] == '/owner'
    with client.session_transaction() as sess:
        assert sess.get('super_admin') is True


def test_the_setup_page_shows_a_qr_code_and_the_key(client, hashed_password):
    with client.session_transaction() as sess:
        sess['super_admin'] = True
    html = client.get('/super-admin-2fa/setup').get_data(as_text=True)
    assert '<svg' in html                      # the QR, drawn by us, not a web service
    row = app_module.admin_security_row()
    assert row.totp_secret, 'no key was made'
    assert row.totp_secret in html             # for typing in by hand
    assert row.confirmed_at is None            # not on until a code comes back
    assert app_module.admin_2fa_is_on() is False


def test_a_wrong_code_does_not_turn_it_on(client, hashed_password):
    with client.session_transaction() as sess:
        sess['super_admin'] = True
    client.get('/super-admin-2fa/setup')
    r = client.post('/super-admin-2fa/setup', data={'action': 'confirm', 'code': '000000'})
    assert 'error=' in r.headers['Location']
    assert app_module.admin_2fa_is_on() is False


def test_confirming_turns_it_on_and_hands_over_eight_backup_codes(client, hashed_password):
    with client.session_transaction() as sess:
        sess['super_admin'] = True
    client.get('/super-admin-2fa/setup')
    secret = app_module.admin_security_row().totp_secret

    html = client.post('/super-admin-2fa/setup',
                       data={'action': 'confirm', 'code': current_code(secret)}
                       ).get_data(as_text=True)
    assert app_module.admin_2fa_is_on() is True
    codes = re.findall(r'<span>([0-9a-f]{4}-[0-9a-f]{4})</span>', html)
    assert len(codes) == 8
    assert app_module.unused_backup_code_count() == 8
    assert 'twofa_enabled' in actions()


def test_backup_codes_are_stored_as_hashes_only(client, hashed_password):
    plain = app_module.generate_backup_codes()
    stored = [row.code_hash for row in app_module.AdminBackupCode.query.all()]
    assert len(plain) == 8
    for code in plain:
        assert code not in stored
        assert any(check_password_hash(h, code) for h in stored)


def test_the_password_alone_is_not_a_session_once_two_factor_is_on(client, hashed_password):
    turn_2fa_on()
    r = client.post('/super-admin-login', data={'password': PASSWORD}, headers=ip())
    assert r.headers['Location'] == '/super-admin-2fa'
    with client.session_transaction() as sess:
        assert sess.get('super_admin') is None
        assert sess.get('super_admin_pending') is True
    # and the panel is still shut
    assert client.get('/owner').headers['Location'].startswith('/super-admin-login')


def test_the_right_code_finishes_the_login(client, hashed_password):
    secret = turn_2fa_on()
    client.post('/super-admin-login', data={'password': PASSWORD}, headers=ip())
    r = client.post('/super-admin-2fa', data={'code': current_code(secret)}, headers=ip())
    assert r.headers['Location'] == '/owner'
    with client.session_transaction() as sess:
        assert sess.get('super_admin') is True
        assert sess.get('super_admin_pending') is None
    assert client.get('/owner').status_code == 200


def test_a_wrong_code_leaves_the_door_shut(client, hashed_password):
    turn_2fa_on()
    client.post('/super-admin-login', data={'password': PASSWORD}, headers=ip())
    r = client.post('/super-admin-2fa', data={'code': '123456'}, headers=ip())
    assert 'error=' in r.headers['Location']
    with client.session_transaction() as sess:
        assert sess.get('super_admin') is None
    assert 'twofa_failed' in actions()


def test_the_code_cannot_be_used_twice(client, hashed_password):
    """Someone who reads the code over your shoulder has 30 seconds to use
    it - unless the one you just used is already spent."""
    secret = turn_2fa_on()
    code = current_code(secret)
    client.post('/super-admin-login', data={'password': PASSWORD}, headers=ip())
    assert client.post('/super-admin-2fa', data={'code': code},
                       headers=ip()).headers['Location'] == '/owner'
    client.get('/super-admin-logout')

    client.post('/super-admin-login', data={'password': PASSWORD}, headers=ip())
    replay = client.post('/super-admin-2fa', data={'code': code}, headers=ip())
    assert 'error=' in replay.headers['Location']
    with client.session_transaction() as sess:
        assert sess.get('super_admin') is None


def test_a_backup_code_works_once_and_only_once(client, hashed_password):
    turn_2fa_on()
    codes = app_module.generate_backup_codes()
    spare = app_module.format_backup_code(codes[0])   # as it is shown on screen

    client.post('/super-admin-login', data={'password': PASSWORD}, headers=ip())
    r = client.post('/super-admin-2fa', data={'code': spare}, headers=ip())
    assert r.headers['Location'].startswith('/super-admin-2fa/setup')
    with client.session_transaction() as sess:
        assert sess.get('super_admin') is True
    assert app_module.unused_backup_code_count() == 7
    assert 'twofa_backup_used' in actions()

    client.get('/super-admin-logout')
    client.post('/super-admin-login', data={'password': PASSWORD}, headers=ip())
    again = client.post('/super-admin-2fa', data={'code': spare}, headers=ip())
    assert 'error=' in again.headers['Location']


def test_the_second_step_cannot_be_walked_into_without_the_password(client):
    r = client.get('/super-admin-2fa')
    assert r.headers['Location'].startswith('/super-admin-login')
    posted = client.post('/super-admin-2fa', data={'code': '123456'}, headers=ip())
    assert posted.headers['Location'].startswith('/super-admin-login')


def test_guessing_codes_gets_rate_limited(client, hashed_password):
    turn_2fa_on()
    client.post('/super-admin-login', data={'password': PASSWORD}, headers=ip('203.0.113.210'))
    for _ in range(5):
        client.post('/super-admin-2fa', data={'code': '000000'}, headers=ip('203.0.113.210'))
    blocked = client.post('/super-admin-2fa', data={'code': '000000'}, headers=ip('203.0.113.210'))
    assert blocked.headers['Location'].startswith('/super-admin-2fa?error=Too+many+attempts')


def test_turning_it_off_needs_a_current_code(client, hashed_password):
    secret = turn_2fa_on()
    app_module.generate_backup_codes()
    with client.session_transaction() as sess:
        sess['super_admin'] = True

    refused = client.post('/super-admin-2fa/setup',
                          data={'action': 'disable', 'code': '000000'})
    assert 'error=' in refused.headers['Location']
    assert app_module.admin_2fa_is_on() is True

    done = client.post('/super-admin-2fa/setup',
                       data={'action': 'disable', 'code': current_code(secret)})
    assert done.headers['Location'].endswith('disabled=1')
    assert app_module.admin_2fa_is_on() is False
    assert app_module.unused_backup_code_count() == 0
    assert 'twofa_disabled' in actions()


def test_new_backup_codes_replace_the_old_ones(client, hashed_password):
    turn_2fa_on()
    old = app_module.generate_backup_codes()
    with client.session_transaction() as sess:
        sess['super_admin'] = True

    html = client.post('/super-admin-2fa/setup',
                       data={'action': 'new_codes'}).get_data(as_text=True)
    new = re.findall(r'<span>([0-9a-f]{4}-[0-9a-f]{4})</span>', html)
    assert len(new) == 8
    assert set(new).isdisjoint({app_module.format_backup_code(c) for c in old})
    assert app_module.unused_backup_code_count() == 8
    assert app_module.use_backup_code(old[0]) is False


def test_the_setup_page_is_only_for_someone_already_logged_in(client):
    assert client.get('/super-admin-2fa/setup').headers['Location'].startswith('/super-admin-login')
    assert client.post('/super-admin-2fa/setup',
                       data={'action': 'confirm', 'code': '000000'}
                       ).headers['Location'].startswith('/super-admin-login')


# ─────────────────────────────────────────────────────────────
# 3. The audit log
# ─────────────────────────────────────────────────────────────

def test_a_failed_sign_in_is_recorded_with_the_address(client, hashed_password):
    client.post('/super-admin-login', data={'password': 'wrong'},
                headers=ip('198.51.100.33'))
    entry = app_module.AdminAudit.query.order_by(app_module.AdminAudit.id.desc()).first()
    assert entry.action == 'login_failed'
    assert entry.ip == '198.51.100.33'
    assert entry.created_at is not None


def test_a_successful_sign_in_and_sign_out_are_recorded(client, hashed_password):
    client.post('/super-admin-login', data={'password': PASSWORD}, headers=ip())
    client.get('/super-admin-logout')
    assert actions() == ['login', 'logout']


def test_the_two_step_login_records_both_halves(client, hashed_password):
    secret = turn_2fa_on()
    client.post('/super-admin-login', data={'password': PASSWORD}, headers=ip())
    client.post('/super-admin-2fa', data={'code': current_code(secret)}, headers=ip())
    assert actions() == ['password_ok_2fa_required', 'login']


def test_agencies_created_and_deleted_from_the_panel_are_recorded(client, hashed_password):
    with client.session_transaction() as sess:
        sess['super_admin'] = True
    created = client.post('/create-agency',
                          json={'name': 'Audited Realty', 'email': 'audit@example.test'},
                          headers=ip())
    agency_id = created.get_json()['agency_id']
    client.delete(f'/delete-agency/{agency_id}', headers=ip())
    assert actions() == ['agency_created', 'agency_deleted']
    detail = app_module.AdminAudit.query.order_by(
        app_module.AdminAudit.id.desc()).first().detail
    assert 'Audited Realty' in detail


def test_a_public_signup_is_not_an_admin_action(client):
    """The audit log is about the panel. Every visitor signing up would
    drown it."""
    r = client.post('/create-agency',
                    json={'name': 'Self Serve Realty', 'email': 'self@example.test'},
                    headers=ip('203.0.113.99'))
    agency = db.session.get(app_module.Agency, r.get_json()['agency_id'])
    db.session.delete(agency)
    db.session.commit()
    assert actions() == []


def test_the_audit_page_is_behind_the_login_and_lists_what_happened(client, hashed_password):
    assert client.get('/super-admin-audit').headers['Location'].startswith('/super-admin-login')
    client.post('/super-admin-login', data={'password': 'wrong'}, headers=ip('198.51.100.44'))
    with client.session_transaction() as sess:
        sess['super_admin'] = True
    html = client.get('/super-admin-audit').get_data(as_text=True)
    assert 'Wrong super admin password' in html
    assert '198.51.100.44' in html


def test_an_audit_failure_never_breaks_the_thing_it_records(monkeypatch, client, hashed_password):
    def explode(*args, **kwargs):
        raise RuntimeError('database gone')
    monkeypatch.setattr(app_module.db.session, 'commit', explode)
    assert app_module.record_admin_action('login') is None


# ─────────────────────────────────────────────────────────────
# 4. provision_agency() and is_entitled()
# ─────────────────────────────────────────────────────────────

def test_provision_agency_creates_a_usable_agency():
    agency, temp_password = app_module.provision_agency(
        name='Pilot Realty', email='pilot@example.test', tier='agency')
    try:
        assert agency.id and agency.tier == 'agency'
        assert agency.subscription_status == 'trialing'
        assert agency.trial_ends_at > datetime.utcnow()
        assert temp_password and agency.check_password(temp_password)
        assert agency.password_hash != temp_password      # stored hashed
        assert agency.billing_email == 'pilot@example.test'
        assert app_module.is_entitled(agency)
    finally:
        db.session.delete(agency)
        db.session.commit()


def test_provision_agency_keeps_a_password_the_caller_chose():
    agency, temp_password = app_module.provision_agency(
        name='Chosen Realty', email='chosen@example.test', password='their-own-password')
    try:
        assert temp_password is None
        assert agency.check_password('their-own-password')
    finally:
        db.session.delete(agency)
        db.session.commit()


def test_provision_agency_refuses_an_agency_with_no_name_or_email():
    with pytest.raises(ValueError):
        app_module.provision_agency(name='', email='someone@example.test')
    with pytest.raises(ValueError):
        app_module.provision_agency(name='No Email Realty', email='')


def test_an_unknown_tier_falls_back_to_solo():
    agency, _ = app_module.provision_agency(
        name='Odd Tier Realty', email='odd@example.test', tier='platinum')
    try:
        assert agency.tier == 'solo'
    finally:
        db.session.delete(agency)
        db.session.commit()


def test_the_signup_route_answers_exactly_as_it_did_before(client):
    r = client.post('/create-agency',
                    json={'name': 'Shape Realty', 'email': 'shape@example.test'},
                    headers=ip('203.0.113.120'))
    body = r.get_json()
    try:
        assert r.status_code == 200
        assert set(body) == {'agency_id', 'tier', 'trial_ends', 'temp_password', 'message'}
        assert body['tier'] == 'solo'
        assert body['message'] == 'Agency created'
        assert re.fullmatch(r'\d{4}-\d{2}-\d{2}', body['trial_ends'])
        agency = db.session.get(app_module.Agency, body['agency_id'])
        assert agency.check_password(body['temp_password'])
    finally:
        db.session.delete(db.session.get(app_module.Agency, body['agency_id']))
        db.session.commit()


def test_the_route_no_longer_builds_the_agency_itself():
    """One door: if the route grows its own Agency(...) again, the engine
    and the panel will drift apart."""
    src = read('app.py')
    route = src[src.index('def create_agency():'):src.index('def change_owner_password')]
    assert 'provision_agency(' in route
    assert 'Agency(' not in route
    assert src.count('def provision_agency(') == 1


@pytest.mark.parametrize('status, entitled', [
    ('active', True), ('trialing', True), ('past_due', True),
    ('canceled', False), ('paused', False), ('', True), (None, True),
])
def test_is_entitled_answers_for_every_subscription_state(status, entitled):
    class FakeAgency:
        subscription_status = status
    assert app_module.is_entitled(FakeAgency()) is entitled
    assert app_module.has_dashboard_access(FakeAgency()) is entitled


def test_is_entitled_says_no_to_nothing_at_all():
    assert app_module.is_entitled(None) is False


# ─────────────────────────────────────────────────────────────
# 5. The smoke test
# ─────────────────────────────────────────────────────────────

def test_the_smoke_test_answers_without_calling_openai(client, monkeypatch, test_agency):
    monkeypatch.setattr(app_module, 'SMOKE_TEST_TOKEN', 'smoke-token-123')
    monkeypatch.setattr(app_module, 'client', None)   # any OpenAI call would crash

    r = client.post('/chat',
                    json={'message': 'hello', 'agency_id': test_agency.id,
                          'session_id': 'smoke-session'},
                    headers={'X-Smoke-Test': 'smoke-token-123', **ip()})
    body = r.get_json()
    assert r.status_code == 200
    assert body['smoke_test'] is True
    assert body['agency'] == test_agency.name
    assert 'Smoke test OK' in body['reply']


def test_the_smoke_test_leaves_nothing_behind(client, monkeypatch, test_agency):
    monkeypatch.setattr(app_module, 'SMOKE_TEST_TOKEN', 'smoke-token-123')
    monkeypatch.setattr(app_module, 'client', None)
    before = app_module.Lead.query.count()
    client.post('/chat',
                json={'message': 'hello', 'agency_id': test_agency.id,
                      'session_id': 'smoke-leaves-nothing'},
                headers={'X-Smoke-Test': 'smoke-token-123', **ip()})
    assert app_module.Lead.query.count() == before
    assert db.session.get(app_module.ConversationSession,
                          f'{test_agency.id}_smoke-leaves-nothing') is None


def test_an_unknown_agency_fails_the_smoke_test(client, monkeypatch):
    monkeypatch.setattr(app_module, 'SMOKE_TEST_TOKEN', 'smoke-token-123')
    monkeypatch.setattr(app_module, 'client', None)
    r = client.post('/chat', json={'message': 'hello', 'agency_id': 999999999},
                    headers={'X-Smoke-Test': 'smoke-token-123', **ip()})
    assert r.status_code == 400


def test_a_wrong_or_missing_token_is_not_a_smoke_test(client, monkeypatch, test_agency):
    monkeypatch.setattr(app_module, 'SMOKE_TEST_TOKEN', 'smoke-token-123')
    with app_module.app.test_request_context(
            '/chat', headers={'X-Smoke-Test': 'guessed-token'}):
        assert app_module.is_smoke_test_request() is False
    with app_module.app.test_request_context('/chat'):
        assert app_module.is_smoke_test_request() is False


def test_the_smoke_test_is_off_unless_a_token_is_configured(client, monkeypatch):
    monkeypatch.setattr(app_module, 'SMOKE_TEST_TOKEN', '')
    with app_module.app.test_request_context(
            '/chat', headers={'X-Smoke-Test': 'anything'}):
        assert app_module.is_smoke_test_request() is False


# ─────────────────────────────────────────────────────────────
# Wiring that has to be right for any of the above to survive a deploy
# ─────────────────────────────────────────────────────────────

def test_the_new_tables_have_a_startup_migration():
    """Render runs an existing database: a table that only exists in the
    models is a table that does not exist in production."""
    src = read('app.py')
    migrations = src[src.index('# ── SUPER ADMIN SECURITY MIGRATION'):]
    for model in ('AdminSecurity', 'AdminBackupCode', 'AdminAudit'):
        assert model in migrations, f"{model} has no migration behind it"


def test_the_new_pages_are_on_the_theme():
    for name in ('admin_2fa.html', 'admin_2fa_setup.html', 'admin_audit.html'):
        src = read('templates', name)
        assert '/static/theme.css' in src
        assert src.index('/static/theme.css') < src.index('<style')


def test_the_panel_links_to_security_and_the_audit_log():
    src = read('templates', 'owner.html')
    assert '/super-admin-2fa/setup' in src
    assert '/super-admin-audit' in src


def test_the_qr_code_is_drawn_here_not_fetched_from_a_web_service():
    """An outside QR service would be handed the key to the panel."""
    setup_page = read('templates', 'admin_2fa_setup.html')
    assert 'http' not in setup_page.split('<body>')[0].replace(
        'http-equiv', '').replace('https://www.w3.org', '')
    assert 'qr_svg|safe' in setup_page
    svg = app_module.totp_qr_svg('otpauth://totp/test?secret=JBSWY3DPEHPK3PXP')
    assert svg.lstrip().startswith('<?xml') or svg.lstrip().startswith('<svg')
    assert '<svg' in svg
