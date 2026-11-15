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

# A developer's own .env must not decide what the tests see - or do.
#
# app.py calls load_dotenv() as it is imported, so on a laptop the .env
# sitting beside it was quietly feeding the test run. Two things followed.
# SMTP_EMAIL in that file became the public contact address, so 23 tests
# that check no page shows a personal address failed on the laptop and
# passed everywhere else. Worse, a real BREVO_API_KEY in that file meant
# eight tests that reach the email sender were posting to the live Brevo
# account, to addresses like 35c04dd8@example.test.
#
# So the file is not read at all here, and every setting app.py takes from
# the environment starts from its default. A test that needs one sets it
# itself, with monkeypatch.
import dotenv  # noqa: E402

dotenv.load_dotenv = lambda *args, **kwargs: False

SETTINGS_A_LAPTOP_MIGHT_CARRY = (
    "BREVO_API_KEY", "SMTP_EMAIL", "SUPPORT_EMAIL", "REPLY_TO_EMAIL",
    "EMAIL_FROM_NAME", "PUBLIC_BASE_URL", "SITE_BASE_URL", "RENDER",
    "BUSINESS_NAME", "BUSINESS_ADDRESS", "SHOW_STATS", "SHOW_TIER_3",
    "SMOKE_TEST_TOKEN", "SUPER_ADMIN_PASSWORD_HASH",
    "PADDLE_PRICE_SOLO", "PADDLE_PRICE_AGENCY", "PADDLE_PRICE_CORP",
)
for _name in SETTINGS_A_LAPTOP_MIGHT_CARRY:
    os.environ.pop(_name, None)
for _name in [n for n in os.environ if n.startswith("STAT_")]:
    os.environ.pop(_name, None)

import pytest  # noqa: E402
import app as app_module  # noqa: E402  (import triggers db.create_all())


@pytest.fixture(autouse=True)
def _fresh_rate_limits():
    """Every test starts with empty rate-limit counters. The whole suite runs
    from one address (127.0.0.1) in one process, so without this the login
    tests would eventually lock each other out."""
    app_module.limiter.reset()
    yield


@pytest.fixture(autouse=True)
def _no_real_model(monkeypatch):
    """No test talks to OpenAI by accident.

    Five tests that post to /chat without faking the model were each
    opening a real connection to api.openai.com with the dummy key - three
    tries apiece, because the SDK retries. Harmless to the bill, but it
    made the run slower and dependent on a network. Now the call fails on
    the spot, which is what those tests already expected of it. A test
    that needs an answer supplies one with monkeypatch, as before."""
    def unreachable(**kwargs):
        raise RuntimeError("a test reached the model without faking it")
    monkeypatch.setattr(app_module.client.chat.completions, "create", unreachable)
    yield


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
