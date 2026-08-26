"""Regression tests for the appointment date-window fix (commit 347cfda).

Before the fix: a confirmed booking was silently dropped whenever the slot
date was more than 7 days out from "today" - too tight, given real
conversations often take minutes or hours (or longer) to reach a
confirmed booking. The window was widened to 30 days and pulled out into
its own function, is_slot_within_booking_window(), so it can be tested
directly instead of only indirectly through the full /chat endpoint.
"""
from datetime import date, timedelta

import app as app_module

TODAY = date(2026, 8, 23)


def test_today_is_within_window():
    assert app_module.is_slot_within_booking_window(TODAY, TODAY) is True


def test_original_bug_scenario_8_days_out_now_succeeds():
    """This is the exact scenario the prior QA round was asked to retest:
    a booking made later in a longer conversation, 8+ days out. Under the
    old 7-day rule this silently failed; it must now succeed."""
    slot_date = TODAY + timedelta(days=8)
    assert app_module.is_slot_within_booking_window(slot_date, TODAY) is True


def test_exactly_30_days_out_is_the_inclusive_boundary():
    slot_date = TODAY + timedelta(days=30)
    assert app_module.is_slot_within_booking_window(slot_date, TODAY) is True


def test_31_days_out_is_rejected():
    slot_date = TODAY + timedelta(days=31)
    assert app_module.is_slot_within_booking_window(slot_date, TODAY) is False


def test_a_past_date_is_rejected():
    slot_date = TODAY - timedelta(days=1)
    assert app_module.is_slot_within_booking_window(slot_date, TODAY) is False


def test_far_past_date_is_rejected():
    slot_date = TODAY - timedelta(days=365)
    assert app_module.is_slot_within_booking_window(slot_date, TODAY) is False


def test_custom_window_size_is_respected():
    """max_days_ahead is a parameter, not a hardcoded 30 - confirm the
    caller can still override it if a future tier needs a different
    window."""
    slot_date = TODAY + timedelta(days=10)
    assert app_module.is_slot_within_booking_window(slot_date, TODAY, max_days_ahead=5) is False
    assert app_module.is_slot_within_booking_window(slot_date, TODAY, max_days_ahead=10) is True
