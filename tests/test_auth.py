"""Tests for the security fixes in this PR:

- Super Admin panel (/owner, /agencies, /delete-agency/<id>) now requires a
  SUPER_ADMIN_PASSWORD-gated login, instead of being wide open to anyone
  who finds the URL.
- Agency owner login now checks a real per-agency hashed password
  (agency.check_password) instead of accepting the literal string
  "admin123" for every agency.
- New agencies get a random generated password instead of the shared
  hardcoded "admin123" default.
- /admin (the agency's own leads dashboard) now requires that agency's
  owner session, not just a truthy agency_id query param.
- Agent management routes (/agents/<id>, /add-agent, /toggle-agent,
  /delete-agent, /agent-dashboard/<id>) now require the right session too.
"""
import app as app_module

SUPER_ADMIN_PASSWORD = "test-super-admin-pw"


# ── Super Admin panel ──────────────────────────────────────────────

def test_owner_panel_redirects_when_not_logged_in(client):
    res = client.get("/owner")
    assert res.status_code == 302
    assert "/super-admin-login" in res.headers["Location"]


def test_agencies_api_rejects_unauthenticated_request(client):
    res = client.get("/agencies")
    assert res.status_code == 401


def test_delete_agency_api_rejects_unauthenticated_request(client, test_agency):
    res = client.delete(f"/delete-agency/{test_agency.id}")
    assert res.status_code == 401
    # and it must not have actually deleted the agency
    assert app_module.db.session.get(app_module.Agency, test_agency.id) is not None


def test_super_admin_login_with_wrong_password_is_rejected(client):
    res = client.post("/super-admin-login", data={"password": "not-the-password"})
    assert res.status_code == 302
    assert "error" in res.headers["Location"]
    with client.session_transaction() as sess:
        assert not sess.get("super_admin")


def test_super_admin_login_with_correct_password_grants_access(client):
    res = client.post("/super-admin-login", data={"password": SUPER_ADMIN_PASSWORD})
    assert res.status_code == 302
    assert res.headers["Location"].endswith("/owner")

    # session now carries super_admin, so previously-blocked routes work
    res = client.get("/owner")
    assert res.status_code == 200

    res = client.get("/agencies")
    assert res.status_code == 200


def test_super_admin_logout_revokes_access(client):
    client.post("/super-admin-login", data={"password": SUPER_ADMIN_PASSWORD})
    assert client.get("/agencies").status_code == 200

    client.get("/super-admin-logout")
    assert client.get("/agencies").status_code == 401


# ── Agency owner login / passwords ─────────────────────────────────

def test_owner_login_rejects_old_hardcoded_admin123(client, test_agency):
    """The literal string "admin123" must no longer be a magic bypass for
    every agency - only an agency's own real password should work."""
    test_agency.set_password("something-else-entirely")
    app_module.db.session.commit()

    res = client.post("/owner-login", data={
        "agency_id": str(test_agency.id),
        "password": "admin123",
    })
    assert res.status_code == 302
    assert "error" in res.headers["Location"]
    with client.session_transaction() as sess:
        assert sess.get("agency_id") != str(test_agency.id)


def test_owner_login_accepts_agencys_real_password(client, test_agency):
    test_agency.set_password("correct-horse-battery-staple")
    app_module.db.session.commit()

    res = client.post("/owner-login", data={
        "agency_id": str(test_agency.id),
        "password": "correct-horse-battery-staple",
    })
    assert res.status_code == 302
    assert res.headers["Location"].startswith(f"/admin?agency_id={test_agency.id}")
    with client.session_transaction() as sess:
        assert sess.get("agency_id") == str(test_agency.id)


def test_create_agency_no_longer_uses_shared_default_password(client):
    res = client.post("/create-agency", json={
        "name": "New Test Agency",
        "email": "newtestagency@example.test",
    })
    assert res.status_code == 200
    data = res.get_json()
    agency_id = data["agency_id"]
    temp_password = data["temp_password"]

    try:
        assert temp_password and temp_password != "admin123"
        agency = app_module.db.session.get(app_module.Agency, agency_id)
        assert agency.check_password(temp_password) is True
        assert agency.check_password("admin123") is False
    finally:
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_two_new_agencies_get_different_passwords(client):
    res1 = client.post("/create-agency", json={"name": "A1", "email": "a1@example.test"})
    res2 = client.post("/create-agency", json={"name": "A2", "email": "a2@example.test"})
    d1, d2 = res1.get_json(), res2.get_json()
    try:
        assert d1["temp_password"] != d2["temp_password"]
    finally:
        for d in (d1, d2):
            a = app_module.db.session.get(app_module.Agency, d["agency_id"])
            if a:
                app_module.db.session.delete(a)
        app_module.db.session.commit()


# ── /admin dashboard requires the matching owner session ───────────

def test_admin_dashboard_redirects_without_login(client, test_agency):
    res = client.get(f"/admin?agency_id={test_agency.id}")
    assert res.status_code == 302
    assert "/owner-login" in res.headers["Location"]


def test_admin_dashboard_blocks_a_different_agencys_session(client, test_agency):
    """Logging in as agency A must not grant access to agency B's dashboard
    just by editing the agency_id in the URL."""
    with client.session_transaction() as sess:
        sess["agency_id"] = str(test_agency.id + 999999)  # some other agency
    res = client.get(f"/admin?agency_id={test_agency.id}")
    assert res.status_code == 302
    assert "/owner-login" in res.headers["Location"]


def test_admin_dashboard_allows_the_matching_owner_session(client, test_agency):
    with client.session_transaction() as sess:
        sess["agency_id"] = str(test_agency.id)
    res = client.get(f"/admin?agency_id={test_agency.id}")
    assert res.status_code == 200


def test_owner_logout_clears_session(client, test_agency):
    with client.session_transaction() as sess:
        sess["agency_id"] = str(test_agency.id)
    client.get("/owner-logout")
    res = client.get(f"/admin?agency_id={test_agency.id}")
    assert res.status_code == 302


# ── Change password route ───────────────────────────────────────────

def test_change_owner_password_requires_the_owner_session(client, test_agency):
    res = client.post(
        f"/change-owner-password/{test_agency.id}",
        json={"new_password": "brand-new-password"},
    )
    assert res.status_code == 401


def test_change_owner_password_works_for_logged_in_owner(client, test_agency):
    with client.session_transaction() as sess:
        sess["agency_id"] = str(test_agency.id)
    res = client.post(
        f"/change-owner-password/{test_agency.id}",
        json={"new_password": "brand-new-password"},
    )
    assert res.status_code == 200
    app_module.db.session.refresh(test_agency)
    assert test_agency.check_password("brand-new-password") is True


def test_change_owner_password_rejects_too_short_password(client, test_agency):
    with client.session_transaction() as sess:
        sess["agency_id"] = str(test_agency.id)
    res = client.post(
        f"/change-owner-password/{test_agency.id}",
        json={"new_password": "abc"},
    )
    assert res.status_code == 400


def test_change_owner_password_works_for_super_admin(client, test_agency):
    with client.session_transaction() as sess:
        sess["super_admin"] = True
    res = client.post(
        f"/change-owner-password/{test_agency.id}",
        json={"new_password": "reset-by-super-admin"},
    )
    assert res.status_code == 200


# ── Agent management routes ─────────────────────────────────────────

def test_add_agent_requires_owner_session(client, test_agency):
    res = client.post(f"/add-agent/{test_agency.id}", json={
        "name": "Test Agent", "email": "agent@example.test",
    })
    assert res.status_code == 401


def test_add_agent_generates_random_password_when_none_supplied(client, test_agency):
    with client.session_transaction() as sess:
        sess["agency_id"] = str(test_agency.id)
    res = client.post(f"/add-agent/{test_agency.id}", json={
        "name": "Test Agent", "email": f"agent-{test_agency.id}@example.test",
    })
    assert res.status_code == 200
    data = res.get_json()
    try:
        assert data["default_password"] != "agent123"
        agent = app_module.db.session.get(app_module.Agent, data["agent_id"])
        assert agent.check_password(data["default_password"]) is True
    finally:
        agent = app_module.db.session.get(app_module.Agent, data["agent_id"])
        if agent:
            app_module.db.session.delete(agent)
            app_module.db.session.commit()


def test_agent_dashboard_requires_that_agents_own_session(client, test_agency):
    agent = app_module.Agent(agency_id=test_agency.id, name="A", email=f"a-{test_agency.id}@x.test")
    agent.set_password("whatever")
    app_module.db.session.add(agent)
    app_module.db.session.commit()
    try:
        res = client.get(f"/agent-dashboard/{agent.id}")
        assert res.status_code == 302
        assert "/agent-login" in res.headers["Location"]

        with client.session_transaction() as sess:
            sess["agent_id"] = agent.id
        res = client.get(f"/agent-dashboard/{agent.id}")
        assert res.status_code == 200
    finally:
        app_module.db.session.delete(agent)
        app_module.db.session.commit()
