"""Regression tests for the multi-city / Spanish-alias fix in
detect_location() (commit 347cfda).

Before the fix: a customer mentioning two cities ("New York or Nashville")
only ever got matched against one of them, and the Spanish "Nueva York"
was not recognized as "New York" at all.
"""
import app as app_module
from conftest import make_listing, user_msg


def test_single_city_still_matches(test_agency):
    make_listing(test_agency.id, "Cozy Downtown Condo", "Nashville, TN")
    history = [user_msg("I'm interested in Nashville.")]

    result = app_module.detect_location(test_agency.id, history)

    assert result == ["nashville"]


def test_two_cities_mentioned_together_both_returned(test_agency):
    """The exact bug from the prior QA round: Carter mentioned 'New York
    or Nashville' but only Nashville was ever offered. Both must now come
    back as OR options, in the order they were mentioned."""
    make_listing(test_agency.id, "Skyline Loft", "New York, NY")
    make_listing(test_agency.id, "Cozy Downtown Condo", "Nashville, TN")
    history = [user_msg("I'm open to New York or Nashville, whichever has something good.")]

    result = app_module.detect_location(test_agency.id, history)

    assert result == ["new york", "nashville"]


def test_order_of_mention_is_preserved(test_agency):
    make_listing(test_agency.id, "Skyline Loft", "New York, NY")
    make_listing(test_agency.id, "Cozy Downtown Condo", "Nashville, TN")
    history = [user_msg("Nashville or New York, either works for me.")]

    result = app_module.detect_location(test_agency.id, history)

    assert result == ["nashville", "new york"]


def test_spanish_nueva_york_maps_to_new_york(test_agency):
    make_listing(test_agency.id, "Skyline Loft", "New York, NY")
    history = [user_msg("Me interesa Nueva York, tienen algo disponible?")]

    result = app_module.detect_location(test_agency.id, history)

    assert result == ["new york"]


def test_no_location_mentioned_returns_empty(test_agency):
    make_listing(test_agency.id, "Skyline Loft", "New York, NY")
    history = [user_msg("What's the price range on your listings?")]

    result = app_module.detect_location(test_agency.id, history)

    assert result == []


def test_only_matches_this_agencys_own_listings(test_agency):
    """detect_location is scoped to the agency's own inventory - a city
    that's real but isn't in *this* agency's listings shouldn't match."""
    make_listing(test_agency.id, "Cozy Downtown Condo", "Nashville, TN")
    history = [user_msg("Do you have anything in Miami?")]

    result = app_module.detect_location(test_agency.id, history)

    assert result == []
