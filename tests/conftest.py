"""Shared pytest fixtures for the app.py test suite.

app.py does real work at import time (reads env vars, builds the OpenAI
client, connects the database, and even runs schema migrations) - so every
fixture in here is about giving it a safe, throwaway environment before
that import happens, without ever touching real credentials or the real
database.
"""
import os
import sys
import tempfile
import uuid

# app.py raises immediately if OPENAI_API_KEY is unset - these tests never
# call OpenAI, so a dummy value satisfies the check without needing (or
# risking) a real key.
os.environ.setdefault("OPENAI_API_KEY", "test-dummy-key-not-real")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("SUPER_ADMIN_PASSWORD", "test-super-admin-pw")

# A fresh, empty SQLite file per test run - never the real luxury_leads.db.
_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
import app as app_module  # noqa: E402  (import triggers db.create_all())


@pytest.fixture(scope="session", autouse=True)
def _flask_app_context():
    """All app.py DB calls expect an active Flask app context."""
    with app_module.app.app_context():
        yield


@pytest.fixture()
def client():
    """Flask test client, for exercising real HTTP routes (auth, sessions,
    redirects) rather than calling functions directly."""
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture()
def test_agency():
    """A throwaway Agency row, deleted (with its listings) after the test."""
    agency = app_module.Agency(
        name=f"Test Agency {uuid.uuid4().hex[:8]}",
        email=f"{uuid.uuid4().hex[:8]}@example.test",
        tier="solo",
    )
    app_module.db.session.add(agency)
    app_module.db.session.commit()
    yield agency
    app_module.Listing.query.filter_by(agency_id=agency.id).delete()
    app_module.db.session.delete(agency)
    app_module.db.session.commit()


def make_listing(agency_id, title, location, **kwargs):
    listing = app_module.Listing(
        agency_id=agency_id,
        title=title,
        location=location,
        **kwargs,
    )
    app_module.db.session.add(listing)
    app_module.db.session.commit()
    return listing


def user_msg(text):
    return {"role": "user", "content": text}
