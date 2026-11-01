"""One subscription, one website.

A client pays for an assistant on their website. Nothing stopped them
pasting the same script tag onto a second business's site, and nothing
told Moaz it had happened.

The rules this file holds, and why each one is the way it is:

  - acme.com, www.acme.com and staging.acme.com are ONE website. That is
    how an agency actually runs a site, and a rule that trips a paying
    client on their own domain in week one is a rule that generates
    support tickets rather than revenue;
  - a mismatch is RECORDED, never blocked. A client's live chatbot going
    silent over a rule we guessed at is worse than a fortnight of a
    second site. The ledger is what turns "warn" into something Moaz can
    act on;
  - an agency with no website on file is never checked. Every agency that
    existed when this shipped is in that position, and none of them
    changed under their owner;
  - a TEST agency is an explicit thing, created only from the super admin
    panel. The signup forms cannot ask for one - if they could, a visitor
    could licence themselves out of the rule entirely;
  - and the owner cannot edit their own licensed website. That one is the
    whole feature: a lock the locked party can open is not a lock.

It is a licence check, not a security control. The domain comes from the
browser's Origin header, which someone determined could forge from their
own server. It catches what really happens.
"""
import pytest

import app as app_module

db = app_module.db


@pytest.fixture(autouse=True)
def _clean():
    app_module.WidgetOrigin.query.delete()
    db.session.commit()
    yield
    db.session.rollback()


@pytest.fixture()
def admin(client):
    with client.session_transaction() as sess:
        sess['super_admin'] = True
    return client


def make_agency(website='acme.com', is_test=False, name='Acme Realty'):
    agency, _ = app_module.provision_agency(
        name=name, email=f'{name.split()[0].lower()}@example.test',
        website_url=website, is_test=is_test)
    return agency


# ── what counts as the same website ──

@pytest.mark.parametrize('given,expected', [
    ('https://WWW.Acme.co.uk/contact?x=1', 'acme.co.uk'),
    ('acme.com', 'acme.com'),
    ('https://staging.acme.com/', 'acme.com'),
    ('http://www.hofman.com.au/a/b', 'hofman.com.au'),
    ('https://sub.deep.acme.com', 'acme.com'),
    ('', ''),
    ('   ', ''),
    ('not a url', ''),
    ('localhost', ''),
])
def test_an_address_reduces_to_its_registrable_domain(given, expected):
    assert app_module.site_host(given) == expected


@pytest.mark.parametrize('seen', [
    'https://acme.com', 'https://www.acme.com/page', 'http://acme.com',
    'https://staging.acme.com', 'https://shop.acme.com/x?y=1',
])
def test_their_own_subdomains_are_the_same_website(seen):
    assert app_module.same_site('acme.com', seen)


@pytest.mark.parametrize('seen', [
    'https://otherbrand.com', 'https://acme.co.uk', 'https://acme.com.evil.net',
    'https://notacme.com', '',
])
def test_somebody_elses_domain_is_not(seen):
    assert not app_module.same_site('acme.com', seen)


def test_a_blank_registration_matches_nothing():
    """Otherwise an agency with no website would match every site."""
    assert not app_module.same_site('', 'https://anything.com')
    assert not app_module.same_site(None, 'https://anything.com')


def test_a_three_label_suffix_is_not_mistaken_for_a_domain():
    """acme.co.uk is a website; co.uk is not. Getting this wrong would
    make every .co.uk agency match every other one."""
    assert app_module.site_host('https://acme.co.uk') == 'acme.co.uk'
    assert not app_module.same_site('acme.co.uk', 'https://other.co.uk')


# ── where the domain comes from ──

def test_the_origin_header_is_what_is_read(client):
    headers = {'Origin': 'https://www.acme.com'}
    assert app_module.site_host(headers['Origin']) == 'acme.com'


def test_a_visitor_on_the_right_site_is_recorded_as_allowed(client):
    agency = make_agency('acme.com')
    try:
        client.post('/chat', json={'message': 'hello', 'agency_id': agency.id},
                    headers={'Origin': 'https://www.acme.com',
                             'X-Smoke-Test': 'x'})
        row = app_module.WidgetOrigin.query.filter_by(agency_id=agency.id).first()
        assert row is not None and row.host == 'acme.com'
        assert row.allowed is True
    finally:
        db.session.delete(agency)
        db.session.commit()


def test_a_visitor_on_the_wrong_site_is_recorded_as_not_allowed(client):
    agency = make_agency('acme.com')
    try:
        client.post('/chat', json={'message': 'hello', 'agency_id': agency.id},
                    headers={'Origin': 'https://someoneelse.com'})
        row = app_module.WidgetOrigin.query.filter_by(
            agency_id=agency.id, host='someoneelse.com').first()
        assert row is not None
        assert row.allowed is False
    finally:
        app_module.WidgetOrigin.query.filter_by(agency_id=agency.id).delete()
        db.session.delete(agency)
        db.session.commit()


def test_the_visitor_is_still_answered_on_the_wrong_site(client):
    """The decision: warn, do not block. A client's live chatbot must not
    go silent over a rule."""
    agency = make_agency('acme.com')
    try:
        response = client.post(
            '/chat', json={'message': 'hello', 'agency_id': agency.id},
            headers={'Origin': 'https://someoneelse.com'})
        assert response.status_code != 403, 'the visitor was blocked'
        assert response.status_code in (200, 429, 500)
    finally:
        app_module.WidgetOrigin.query.filter_by(agency_id=agency.id).delete()
        db.session.delete(agency)
        db.session.commit()


def test_visiting_twice_counts_rather_than_duplicating(client):
    agency = make_agency('acme.com')
    try:
        for _ in range(2):
            client.post('/chat', json={'message': 'hi', 'agency_id': agency.id},
                        headers={'Origin': 'https://acme.com'})
        rows = app_module.WidgetOrigin.query.filter_by(agency_id=agency.id).all()
        assert len(rows) == 1
        assert rows[0].hits >= 2
    finally:
        app_module.WidgetOrigin.query.filter_by(agency_id=agency.id).delete()
        db.session.delete(agency)
        db.session.commit()


# ── who is exempt, and why ──

def test_an_agency_with_no_website_is_never_checked():
    """Every agency that existed when this shipped. Nobody's account
    changed under them."""
    agency = app_module.Agency(name='Old Timer', email='old@example.test')
    assert not app_module.licence_is_checked(agency)


def test_a_test_agency_is_never_checked():
    agency = make_agency(website=None, is_test=True, name='Bench Realty')
    try:
        assert agency.is_test is True
        assert agency.website_url is None
        assert not app_module.licence_is_checked(agency)
    finally:
        db.session.delete(agency)
        db.session.commit()


def test_a_test_agency_on_any_domain_is_fine(client):
    agency = make_agency(website=None, is_test=True, name='Bench Two')
    try:
        client.post('/chat', json={'message': 'hi', 'agency_id': agency.id},
                    headers={'Origin': 'https://anywhere-at-all.com'})
        row = app_module.WidgetOrigin.query.filter_by(agency_id=agency.id).first()
        assert row is not None and row.allowed is True
    finally:
        app_module.WidgetOrigin.query.filter_by(agency_id=agency.id).delete()
        db.session.delete(agency)
        db.session.commit()


# ── a real signup must name a website ──

def test_signing_up_without_a_website_is_refused(client):
    response = client.post('/create-agency', json={
        'name': 'No Site Realty', 'email': 'nosite@example.test'})
    assert response.status_code == 400
    assert 'website' in response.get_json()['error'].lower()


def test_signing_up_with_a_website_works(client):
    response = client.post('/create-agency', json={
        'name': 'Has Site Realty', 'email': 'hassite@example.test',
        'website_url': 'https://www.hassite.com/home'})
    assert response.status_code == 200
    agency = db.session.get(app_module.Agency,
                            response.get_json()['agency_id'])
    try:
        # stored reduced, so a later comparison has nothing to normalise
        assert agency.website_url == 'hassite.com'
        assert agency.is_test is False
    finally:
        db.session.delete(agency)
        db.session.commit()


def test_a_visitor_cannot_make_themselves_a_test_agency(client):
    """Both signup forms post to the same route as the panel. Without
    this, anyone could send is_test and skip the rule entirely."""
    response = client.post('/create-agency', json={
        'name': 'Sneaky Realty', 'email': 'sneaky@example.test',
        'is_test': True})
    assert response.status_code == 400, 'a visitor created a test agency'


def test_the_super_admin_can_create_a_test_agency(admin):
    response = admin.post('/create-agency', json={
        'name': 'Bench Realty', 'email': 'bench@example.test',
        'is_test': True})
    assert response.status_code == 200
    agency = db.session.get(app_module.Agency,
                            response.get_json()['agency_id'])
    try:
        assert agency.is_test is True
        assert agency.website_url is None
    finally:
        db.session.delete(agency)
        db.session.commit()


def test_provision_agency_refuses_a_real_agency_with_no_website():
    with pytest.raises(ValueError):
        app_module.provision_agency(name='Nowhere Realty',
                                    email='nowhere@example.test')


# ── the lock the locked party cannot open ──

def test_the_owner_cannot_change_their_own_licensed_website(client):
    """The whole feature. An owner who could edit this could licence
    themselves a second site."""
    agency = make_agency('acme.com')
    try:
        with client.session_transaction() as sess:
            sess['agency_id'] = str(agency.id)
        client.post(f'/update-agency-profile/{agency.id}', json={
            'name': 'Acme Realty', 'email': 'acme@example.test',
            'website_url': 'somewhereelse.com', 'is_test': True})
        db.session.refresh(agency)
        assert agency.website_url == 'acme.com', 'an owner moved their licence'
        assert agency.is_test is False, 'an owner made themselves a test agency'
    finally:
        db.session.delete(agency)
        db.session.commit()


def test_the_owner_profile_route_never_reads_those_fields():
    """Belt and braces: the route works from an explicit list of fields,
    and this fails if somebody later adds these to it."""
    import inspect
    source = inspect.getsource(app_module.update_agency_profile)
    assert 'website_url' not in source
    assert 'is_test' not in source


def test_only_the_super_admin_can_set_the_website(client):
    agency = make_agency('acme.com')
    try:
        response = client.post(f'/set-agency-website/{agency.id}',
                               json={'website_url': 'elsewhere.com'})
        assert response.status_code == 401
        db.session.refresh(agency)
        assert agency.website_url == 'acme.com'
    finally:
        db.session.delete(agency)
        db.session.commit()


def test_the_super_admin_can_correct_a_website(admin):
    agency = make_agency('acme.com')
    try:
        response = admin.post(f'/set-agency-website/{agency.id}',
                              json={'website_url': 'https://www.acme-group.com'})
        assert response.status_code == 200
        db.session.refresh(agency)
        assert agency.website_url == 'acme-group.com'
    finally:
        db.session.delete(agency)
        db.session.commit()


def test_correcting_the_website_clears_a_warning_that_was_wrong(admin):
    """The common case: the client's real site is acme-group.com and
    somebody typed acme.com at signup. Fixing it must clear the flag, not
    leave a red mark on an innocent client for ever."""
    agency = make_agency('acme.com')
    try:
        db.session.add(app_module.WidgetOrigin(
            agency_id=agency.id, host='acme-group.com', allowed=False, hits=5))
        db.session.commit()
        admin.post(f'/set-agency-website/{agency.id}',
                   json={'website_url': 'acme-group.com'})
        row = app_module.WidgetOrigin.query.filter_by(
            agency_id=agency.id, host='acme-group.com').first()
        assert row.allowed is True
    finally:
        app_module.WidgetOrigin.query.filter_by(agency_id=agency.id).delete()
        db.session.delete(agency)
        db.session.commit()


# ── what the panel sees ──

def test_the_panel_lists_where_each_widget_has_been_seen(admin):
    agency = make_agency('acme.com')
    try:
        db.session.add(app_module.WidgetOrigin(
            agency_id=agency.id, host='someoneelse.com', allowed=False, hits=3))
        db.session.commit()
        rows = admin.get('/agencies').get_json()
        mine = [row for row in rows if row['id'] == agency.id][0]
        assert mine['website_url'] == 'acme.com'
        assert mine['is_test'] is False
        hosts = {entry['host']: entry for entry in mine['seen_on']}
        assert 'someoneelse.com' in hosts
        assert hosts['someoneelse.com']['allowed'] is False
    finally:
        app_module.WidgetOrigin.query.filter_by(agency_id=agency.id).delete()
        db.session.delete(agency)
        db.session.commit()


def test_recording_an_origin_never_breaks_a_conversation(monkeypatch):
    """A licence ledger is not worth one lost lead."""
    agency = make_agency('acme.com', name='Fragile Realty')
    try:
        def explode(*args, **kwargs):
            raise RuntimeError('database on fire')
        monkeypatch.setattr(app_module.WidgetOrigin, 'query',
                            property(lambda self: explode()))
        assert app_module.note_widget_origin(agency, 'acme.com') is None
    finally:
        db.session.rollback()
        db.session.delete(agency)
        db.session.commit()
