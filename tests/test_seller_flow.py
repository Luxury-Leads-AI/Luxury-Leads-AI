"""Tests for observation 1: the seller side of the business.

Before this, every visitor was treated as a buyer. Moaz's own test chat
had the AI ask a man selling a $5M Miami Beach villa what his "budget"
was, then promise to find him properties.

What's covered here:
  - telling a seller apart from a buyer, including the "rent out" trap
    where the word "rent" would otherwise read as a renter
  - the seller never sees buyer inventory, viewing slots or budget talk
  - a qualified seller becomes a lead_type='seller' lead, and their
    property becomes a PENDING listing - invisible to buyers until the
    agency approves it (Moaz's explicit choice over auto-publishing)
  - approve / reject, and the buyer/seller split on the dashboard
"""
import json

import app as app_module

from tests.test_route_guards import make_agency, make_agent, make_lead, login_as_owner


def _user_turns(*texts):
    return [{"role": "user", "content": t} for t in texts]


# ── Intent detection ─────────────────────────────────────────────────

def test_detects_a_seller():
    for phrase in [
        "I want sell my property and looking for a suitable buyer.",
        "I am selling my villa in Miami",
        "I'd like to sell my house",
        "I want to put my apartment on the market",
        "Can you list my property for me?",
    ]:
        assert app_module.detect_chat_intent(_user_turns(phrase)) == 'sell', phrase


def test_detects_a_landlord_without_mistaking_them_for_a_renter():
    """'rent out' contains 'rent' - the trap that would file a landlord as
    someone looking for a place to live."""
    for phrase in [
        "I want to rent out my apartment",
        "Looking to lease out my condo",
        "I'd like to rent my property to a tenant",
        "I am a landlord with a flat available",
    ]:
        assert app_module.detect_chat_intent(_user_turns(phrase)) == 'rent_out', phrase


def test_still_detects_ordinary_buyers_and_renters():
    assert app_module.detect_chat_intent(
        _user_turns("I want to buy a home for my family")) == 'buy'
    assert app_module.detect_chat_intent(
        _user_turns("Looking to rent a 2 bed apartment")) == 'rent'
    assert app_module.detect_chat_intent(_user_turns("Hello there")) is None


def test_is_seller_intent_helper():
    assert app_module.is_seller_intent('sell')
    assert app_module.is_seller_intent('rent_out')
    assert not app_module.is_seller_intent('buy')
    assert not app_module.is_seller_intent(None)


# ── Seller qualification ─────────────────────────────────────────────

def test_seller_lead_needs_contact_details_and_an_asking_price():
    history = _user_turns("I want to sell my villa", "Miami Beach", "5 beds", "5M $")
    assert app_module.is_seller_lead_qualified(
        {"email": "a@b.test", "name": "Alex", "budget": "5 million"}, history, 'sell')
    # No price = nothing an agent can act on
    assert not app_module.is_seller_lead_qualified(
        {"email": "a@b.test", "name": "Alex", "budget": None}, history, 'sell')
    # No contact details
    assert not app_module.is_seller_lead_qualified(
        {"email": None, "name": "Alex", "budget": "5 million"}, history, 'sell')
    # A buyer never qualifies down this path
    assert not app_module.is_seller_lead_qualified(
        {"email": "a@b.test", "name": "Alex", "budget": "5 million"}, history, 'buy')


# ── The prompt a seller actually gets ────────────────────────────────

def test_seller_conversation_hides_buyer_inventory_and_the_calendar(client, monkeypatch):
    """The seller-mode branch must not hand the model listings to sell them
    or viewing slots to book."""
    agency = make_agency()
    listing = app_module.Listing(
        agency_id=agency.id, title="Some Buyer Listing", location="Miami, FL",
        price=500000, price_numeric=500000, bedrooms=3, bathrooms=2.0,
        status='available', listing_purpose='sale')
    app_module.db.session.add(listing)
    app_module.db.session.commit()

    captured = {}

    class _FakeChoice:
        def __init__(self): self.message = type("m", (), {"content": "Sure, whereabouts is it?"})()

    class _FakeResponse:
        def __init__(self): self.choices = [_FakeChoice()]

    def fake_create(**kwargs):
        captured['system'] = kwargs['messages'][0]['content']
        return _FakeResponse()

    monkeypatch.setattr(app_module.client.chat.completions, "create", fake_create)

    try:
        res = client.post("/chat", json={
            "agency_id": agency.id,
            "message": "Hi, I want to sell my villa in Miami Beach",
            "session_id": "seller-test-session-1",
        })
        assert res.status_code == 200
        system = captured['system']
        assert "PROPERTY OWNER WHO WANTS TO SELL" in system
        assert "Some Buyer Listing" not in system      # no inventory pitched at them
        assert "VIEWING AVAILABILITY" not in system    # no calendar
        assert 'Ask what their "budget" is' in system  # explicitly forbidden
    finally:
        app_module.db.session.delete(listing)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_buyer_conversation_is_unchanged(client, monkeypatch):
    agency = make_agency()
    listing = app_module.Listing(
        agency_id=agency.id, title="Buyer Villa", location="Miami, FL",
        price=500000, price_numeric=500000, bedrooms=3, bathrooms=2.0,
        status='available', listing_purpose='sale')
    app_module.db.session.add(listing)
    app_module.db.session.commit()

    captured = {}

    class _FakeChoice:
        def __init__(self): self.message = type("m", (), {"content": "What are you after?"})()

    class _FakeResponse:
        def __init__(self): self.choices = [_FakeChoice()]

    def fake_create(**kwargs):
        captured['system'] = kwargs['messages'][0]['content']
        return _FakeResponse()

    monkeypatch.setattr(app_module.client.chat.completions, "create", fake_create)

    try:
        client.post("/chat", json={
            "agency_id": agency.id,
            "message": "I want to buy a house in Miami",
            "session_id": "buyer-test-session-1",
        })
        system = captured['system']
        assert "PROPERTY OWNER" not in system
        assert "Buyer Villa" in system
        assert "VIEWING AVAILABILITY" in system
    finally:
        app_module.db.session.delete(listing)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


# ── Listing creation from a seller ───────────────────────────────────

def _fake_extraction(monkeypatch, payload):
    monkeypatch.setattr(app_module, "extract_seller_property",
                        lambda history, intent: payload)


def test_seller_property_becomes_a_pending_listing_not_a_live_one(client, monkeypatch):
    """Moaz chose owner-approval over auto-publishing: nothing a seller
    types is offered to a real buyer until an agent has seen it."""
    agency = make_agency()
    _fake_extraction(monkeypatch, {
        "title": "Miami Beach Luxury Villa", "location": "Miami Beach, FL",
        "property_type": "villa", "bedrooms": 5, "bathrooms": 7.0,
        "features": "Solar system, swimming pool, private gym, home cinema",
        "price_raw": "5M $", "description": "Five-bed luxury villa in Miami Beach.",
    })
    monkeypatch.setattr(app_module, "generate_lead_summary",
                        lambda h, n: "Seller with a Miami Beach villa at $5M.")
    monkeypatch.setattr(app_module, "send_email_brevo", lambda *a, **k: True)

    lead = app_module.Lead(agency_id=agency.id, name="Alex Murphy",
                            email="alex@seller.test", budget="5 million",
                            lead_type='seller', notes='[]')
    app_module.db.session.add(lead)
    app_module.db.session.commit()
    listing = None
    try:
        listing = app_module.create_listing_from_seller(
            agency, lead, _user_turns("I want to sell my villa"), 'sell')
        assert listing is not None
        assert listing.status == 'pending'
        assert listing.source == 'seller_chat'
        assert listing.seller_lead_id == lead.id
        assert listing.bedrooms == 5
        assert listing.listing_purpose == 'sale'
        assert "cinema" in listing.features

        # And critically: the AI must not offer it to anyone yet.
        ctx = app_module.get_listings_context(agency.id, _user_turns("show me a villa"))
        assert "Miami Beach Luxury Villa" not in ctx
    finally:
        if listing:
            app_module.db.session.delete(listing)
        app_module.db.session.delete(lead)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_a_landlords_property_is_created_as_a_rental(monkeypatch):
    agency = make_agency()
    _fake_extraction(monkeypatch, {
        "title": "Downtown Two-Bed", "location": "Austin, TX",
        "property_type": "apartment", "bedrooms": 2, "bathrooms": 1.0,
        "features": "Balcony", "price_raw": "3500/month", "description": "Two-bed flat.",
    })
    lead = app_module.Lead(agency_id=agency.id, name="Landlord",
                            email="ll@seller.test", budget="3500", lead_type='seller', notes='[]')
    app_module.db.session.add(lead)
    app_module.db.session.commit()
    listing = None
    try:
        listing = app_module.create_listing_from_seller(
            agency, lead, _user_turns("I want to rent out my flat"), 'rent_out')
        assert listing.listing_purpose == 'rent'
        assert listing.status == 'pending'
    finally:
        if listing:
            app_module.db.session.delete(listing)
        app_module.db.session.delete(lead)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


# ── Approval ─────────────────────────────────────────────────────────

def _pending_listing(agency_id, seller_lead_id=None):
    row = app_module.Listing(
        agency_id=agency_id, title="Pending Villa", location="Miami, FL",
        price=1000000, price_numeric=1000000, bedrooms=4, bathrooms=3.0,
        status='pending', source='seller_chat', listing_purpose='sale',
        seller_lead_id=seller_lead_id)
    app_module.db.session.add(row)
    app_module.db.session.commit()
    return row


def test_approving_publishes_the_listing_to_buyers(client, monkeypatch):
    sent = []
    monkeypatch.setattr(app_module, "send_email_brevo",
                        lambda to, s, b: sent.append(to) or True)
    agency = make_agency()
    seller = make_lead(agency.id)
    listing = _pending_listing(agency.id, seller.id)
    try:
        login_as_owner(client, agency.id)
        res = client.post(f"/review-seller-listing/{listing.id}",
                          json={"decision": "approve"})
        assert res.status_code == 200
        app_module.db.session.refresh(listing)
        assert listing.status == 'available'

        # Now, and only now, the AI can offer it.
        ctx = app_module.get_listings_context(agency.id, _user_turns("show me a villa"))
        assert "Pending Villa" in ctx
        # The owner who submitted it gets told it's live.
        assert seller.email in sent
    finally:
        app_module.db.session.delete(listing)
        app_module.db.session.delete(seller)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_rejecting_keeps_it_away_from_buyers(client, monkeypatch):
    monkeypatch.setattr(app_module, "send_email_brevo", lambda *a, **k: True)
    agency = make_agency()
    listing = _pending_listing(agency.id)
    try:
        login_as_owner(client, agency.id)
        res = client.post(f"/review-seller-listing/{listing.id}",
                          json={"decision": "reject"})
        assert res.status_code == 200
        app_module.db.session.refresh(listing)
        assert listing.status == 'rejected'
        ctx = app_module.get_listings_context(agency.id, _user_turns("show me a villa"))
        assert "Pending Villa" not in ctx
    finally:
        app_module.db.session.delete(listing)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_review_requires_the_owning_agency(client):
    agency = make_agency()
    other = make_agency()
    listing = _pending_listing(agency.id)
    try:
        login_as_owner(client, other.id)
        res = client.post(f"/review-seller-listing/{listing.id}",
                          json={"decision": "approve"})
        assert res.status_code == 401
        app_module.db.session.refresh(listing)
        assert listing.status == 'pending'
    finally:
        app_module.db.session.delete(listing)
        app_module.db.session.delete(agency)
        app_module.db.session.delete(other)
        app_module.db.session.commit()


def test_review_rejects_a_nonsense_decision(client):
    agency = make_agency()
    listing = _pending_listing(agency.id)
    try:
        login_as_owner(client, agency.id)
        res = client.post(f"/review-seller-listing/{listing.id}",
                          json={"decision": "maybe"})
        assert res.status_code == 400
    finally:
        app_module.db.session.delete(listing)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


# ── Dashboard separation ─────────────────────────────────────────────

def test_dashboard_tells_buyer_and_seller_leads_apart(client):
    agency = make_agency()
    buyer = make_lead(agency.id)
    seller = make_lead(agency.id, lead_type='seller')
    pending = _pending_listing(agency.id, seller.id)
    try:
        login_as_owner(client, agency.id)
        html = client.get(f"/admin?agency_id={agency.id}").get_data(as_text=True)
        assert "🏠 Seller" in html
        assert "🔍 Buyer" in html
        assert 'data-type="seller"' in html
        # And the pending-approval nudge is surfaced where they'll see it.
        assert "waiting for your approval" in html
    finally:
        app_module.db.session.delete(pending)
        app_module.db.session.delete(buyer)
        app_module.db.session.delete(seller)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_listings_page_separates_submissions_from_live_stock(client):
    agency = make_agency()
    seller = make_lead(agency.id, lead_type='seller')
    pending = _pending_listing(agency.id, seller.id)
    live = app_module.Listing(
        agency_id=agency.id, title="Already Live Villa", location="Austin, TX",
        price=700000, price_numeric=700000, status='available', listing_purpose='sale')
    app_module.db.session.add(live)
    app_module.db.session.commit()
    try:
        login_as_owner(client, agency.id)
        html = client.get(f"/listings/{agency.id}").get_data(as_text=True)
        assert "Submitted by property owners" in html
        assert "Pending Villa" in html
        assert "Already Live Villa" in html
        assert seller.email in html          # who submitted it
        assert "Approve &amp; publish" in html
    finally:
        app_module.db.session.delete(pending)
        app_module.db.session.delete(live)
        app_module.db.session.delete(seller)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_leads_default_to_buyer_type():
    """Every lead captured before this feature existed must keep behaving
    like the buyer it was."""
    agency = make_agency()
    lead = make_lead(agency.id)
    try:
        app_module.db.session.refresh(lead)
        assert lead.lead_type == 'buyer'
    finally:
        app_module.db.session.delete(lead)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()
