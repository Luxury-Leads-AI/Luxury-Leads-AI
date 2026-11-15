"""The test run must not depend on, or act through, the laptop it runs on.

15 November 2026: the suite passed in the cloud and failed 23 tests on
Moaz's laptop. Nothing was wrong with the code. app.py reads the .env file
beside it as it is imported, and his holds the Gmail address he logs in to
Brevo with as SMTP_EMAIL - which becomes the public contact address when
SUPPORT_EMAIL is not set. Every public page then showed a personal address,
and the 23 tests that guard against exactly that said so.

The same file can hold a real BREVO_API_KEY. With it loaded, eight tests
that reach the email sender were posting to the live account.
"""
import os

import dotenv
import pytest

import app as app_module


def test_the_env_file_beside_app_py_is_not_read():
    assert dotenv.load_dotenv() is False


def test_the_public_contact_address_is_the_default_one():
    """Whatever SMTP_EMAIL a laptop carries, the tests see the default."""
    assert app_module.SUPPORT_EMAIL == app_module.DEFAULT_SUPPORT_EMAIL
    assert 'gmail.com' not in app_module.SUPPORT_EMAIL


def test_no_real_email_key_is_loaded():
    """send_email_brevo returns False without a key, so a test that forgets
    to fake the sender does nothing instead of emailing a live account."""
    assert os.getenv("BREVO_API_KEY") is None
    assert app_module.send_email_brevo("nobody@example.test", "x", "y") is False


def test_the_model_is_not_reachable_unless_a_test_fakes_it():
    with pytest.raises(RuntimeError, match="without faking it"):
        app_module.client.chat.completions.create(model="gpt-4o-mini", messages=[])
