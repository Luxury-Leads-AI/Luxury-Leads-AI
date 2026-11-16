"""An owner with more than one property to sell or rent out.

Asked for on 16 November 2026: "upgrade the option to sell multiple
properties as well". Until now a conversation held one property to list;
an owner who mentioned a second had it folded into the first, and the
email-based duplicate check would have refused it a lead of its own anyway.

Each property is now a request of its own: asked its own questions (but
not the contact details again), listed as its own pending listing, and
saved as its own seller lead - because each is its own deal, with its own
price, and may go to a different agent.
"""
import uuid

import pytest

import app as app_module

from tests.test_route_guards import make_agency


def talk(*texts):
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": t}
            for i, t in enumerate(texts)]


OMAR = [
    ("hi", "Hello! May I know who I'm speaking with?"),
    ("Omar", "Nice to meet you, Omar! Which brings you in today?"),
    ("I want to sell my villa and my apartment in Dubai", "Let's start with the villa. Whereabouts is it?"),
    ("Palm Jumeirah", "How many bedrooms does it have?"),
    ("5 bedrooms", "How many bathrooms?"),
    ("6", "Anything that makes it stand out?"),
    ("private beach and a pool", "What price are you hoping for?"),
    ("12M AED", "How soon are you hoping to sell?"),
    ("within 3 months", "Best way to reach you - WhatsApp, phone, or email?"),
    ("WhatsApp", "What's your WhatsApp number?"),
    ("+971 50 111 2222", "And your email address, so the team can confirm in writing?"),
    ("omar@example.test", "The villa's details are with the team. Now your apartment - whereabouts is it?"),
    ("Dubai Marina", "How many bedrooms does it have?"),
    ("2 bedrooms", "How many bathrooms?"),
    ("2", "Anything that makes it stand out?"),
    ("sea view", "What price are you hoping for?"),
    ("2.5M AED", "How soon are you hoping to sell?"),
    ("no rush", "Your apartment's details are with the team too. If you have another property, I can take its details."),
    ("Oh and I also have a townhouse I want to rent out", "Whereabouts is the townhouse?"),
    ("Jumeirah Village Circle", "How many bedrooms does it have?"),
    ("3 bedrooms", "How many bathrooms?"),
    ("3", "Anything that makes it stand out?"),
    ("garden", "What monthly rent are you asking?"),
    ("15000 AED", "How soon are you hoping to have it rented out?"),
    ("next month", "Thank you, Omar - all three are with the team now."),
]
HISTORY = talk(*[x for pair in OMAR for x in pair])


def upto(n):
    """The conversation as it stood when Omar sent his Nth message."""
    return HISTORY[:2 * n - 1]


# ── which property is in hand ─────────────────────────────────────────

def test_two_named_up_front_are_two_properties():
    view = app_module.conversation_view(upto(3))
    assert view["key"] == "owner1"
    assert view["announced"] == 2
    assert app_module.requests_waiting(view) == ["sell their second property"]


def test_the_first_is_described_until_it_is_finished():
    for n in range(3, 12):
        view = app_module.conversation_view(upto(n))
        assert view["key"] == "owner1", n
        assert view["moved_on"] is False


def test_the_email_that_finishes_the_first_turns_to_the_second():
    view = app_module.conversation_view(upto(12))
    assert view["key"] == "owner2" and view["moved_on"] is True
    assert view["previous"]["key"] == "owner1"


def test_the_second_property_starts_from_nothing():
    view = app_module.conversation_view(upto(14))
    facts = app_module.property_facts_in(view["messages"])
    assert facts["bedrooms"] == 2
    said = " ".join(m["content"] for m in view["messages"] if m["role"] == "user")
    assert "Palm" not in said and "12M" not in said


def test_the_type_named_up_front_belongs_to_each_property():
    assert app_module.conversation_view(upto(4))["announced_type"] == "villa"
    assert app_module.conversation_view(upto(13))["announced_type"] == "apartment"


def test_one_named_after_the_others_are_done_is_a_third():
    view = app_module.conversation_view(upto(19))
    assert view["key"] == "owner3" and view["intent"] == "rent_out"
    intents = [r["intent"] for r in view["requests"]]
    assert intents == ["sell", "sell", "rent_out"]


def test_one_named_from_the_buying_side_is_a_new_property():
    """Noor's conversation, finished, and then one more line."""
    from tests.test_two_requests import NOOR
    history = NOOR + [{"role": "assistant", "content": "The team will be in touch."},
                      {"role": "user", "content": "I also want to sell my apartment in Dubai"}]
    view = app_module.conversation_view(history)
    assert view["key"] == "owner2" and view["intent"] == "sell"
    assert [r["key"] for r in view["requests"]] == ["seeker1", "owner1", "owner2"]


def test_each_property_keeps_its_own_price():
    view = app_module.conversation_view(HISTORY)
    prices = [app_module.budget_from_conversation(r["messages"]) for r in view["requests"]]
    assert prices == ["12 million AED", "2.5 million AED", "15,000 AED"]


def test_one_mentioned_mid_description_waits_its_turn():
    history = talk("I want to sell my house", "Whereabouts is it?",
                   "Austin. Oh and I also have another flat to sell too",
                   "Noted - let's finish the house first. How many bedrooms?")
    view = app_module.conversation_view(history)
    assert view["key"] == "owner1"
    assert view["announced"] == 2
    assert app_module.requests_waiting(view) == ["sell their second property"]


@pytest.mark.parametrize("said", [
    "there's another villa next door that sold last year",
    "the other house on the street is bigger",
])
def test_a_neighbour_is_not_a_second_property(said):
    history = talk("I want to sell my house", "Whereabouts is it?", "Austin", "Anything special?", said)
    view = app_module.conversation_view(history)
    assert view["key"] == "owner1" and view["announced"] <= 1


@pytest.mark.parametrize("said, count", [
    ("I have two properties to sell", 2),
    ("I want to rent out 3 apartments", 3),
    ("Quiero vender dos pisos", 2),
    ("Je veux vendre deux maisons", 2),
    ("Ich möchte zwei Wohnungen verkaufen", 2),
    ("Voglio vendere due appartamenti", 2),
    ("Quero vender duas casas", 2),
    ("Ik wil twee woningen verkopen", 2),
    ("أريد بيع شقتين", 2),
    ("I want to sell my 3 bedroom apartment", 0),
])
def test_how_many_they_said(said, count):
    assert app_module.properties_announced(said.lower()) == count


@pytest.mark.parametrize("said", [
    "I also have another apartment to sell",
    "Tengo otra casa para vender",
    "J'ai une autre maison à vendre",
    "Ich habe noch eine Wohnung zu verkaufen",
    "Ho un'altra casa da vendere",
    "Tenho outra casa para vender",
    "Ik heb nog een woning te koop",
    "لدي شقة أخرى للبيع",
])
def test_another_property_in_every_language(said):
    assert app_module.another_property_in(said.lower(), None, True) is True


# ── what the model is told ────────────────────────────────────────────

def test_the_model_is_told_another_property_is_waiting():
    note = app_module.two_requests_context(app_module.conversation_view(upto(5)), "Dubai Homes")
    assert "Waiting: they ALSO want to sell their second property" in note
    assert "do NOT say goodbye" in note


def test_the_model_is_told_to_move_to_the_next_property():
    note = app_module.two_requests_context(app_module.conversation_view(upto(12)), "Dubai Homes")
    assert "turn to their next property" in note
    assert "Do not ask for their name, their email, a number or a contact preference" in note
    assert "Nothing they told you about the previous property describes this one" in note


# ── through the real route ────────────────────────────────────────────

class ScriptedModel:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def __call__(self, **kwargs):
        self.prompts.append(kwargs["messages"][0]["content"])
        message = type("Message", (), {"content": self.replies.pop(0)})()
        return type("Response", (), {"choices": [type("Choice", (), {"message": message})()]})()


@pytest.fixture()
def omars_chat(client, monkeypatch):
    agency = make_agency()
    agency.name = "Dubai Homes"
    app_module.db.session.commit()
    model = ScriptedModel([reply for _, reply in OMAR])
    monkeypatch.setattr(app_module.client.chat.completions, "create", model)
    places = {"Palm": ("Palm Jumeirah", "villa"), "Marina": ("Dubai Marina", "apartment"),
              "Circle": ("Jumeirah Village Circle", "townhouse")}
    seen = []

    def extract(history, intent):
        said = " ".join(m["content"] for m in history if m["role"] == "user")
        seen.append(said)
        place, kind = next(v for k, v in places.items() if k in said)
        return {"title": f"{kind.title()} in {place}", "location": place, "property_type": kind,
                "bedrooms": None, "bathrooms": None, "features": "", "price_raw": None,
                "description": ""}
    monkeypatch.setattr(app_module, "extract_seller_property", extract)
    monkeypatch.setattr(app_module, "generate_lead_summary", lambda history, name: "summary")
    sent = []
    monkeypatch.setattr(app_module, "send_email_brevo",
                        lambda to, subject, body, reply_to=None: sent.append(subject) or True)
    session_id = f"omar-{uuid.uuid4().hex[:10]}"
    for number, (said, _) in enumerate(OMAR):
        if number % 15 == 0:
            app_module.limiter.reset()      # a real visitor takes longer than a test
        res = client.post("/chat", json={"agency_id": agency.id, "message": said,
                                         "session_id": session_id})
        assert res.status_code == 200, res.get_data(as_text=True)
    leads = app_module.Lead.query.filter_by(agency_id=agency.id).order_by(app_module.Lead.id).all()
    yield {"agency": agency, "model": model, "leads": leads, "seen": seen, "sent": sent}
    app_module.ConversationSession.query.filter(
        app_module.ConversationSession.session_key.like(f"%{session_id}")
    ).delete(synchronize_session=False)
    for model_ in (app_module.Listing, app_module.Lead):
        for row in model_.query.filter_by(agency_id=agency.id).all():
            app_module.db.session.delete(row)
    app_module.db.session.delete(agency)
    app_module.db.session.commit()


def test_three_properties_three_seller_leads(omars_chat):
    leads = omars_chat["leads"]
    assert [l.lead_type for l in leads] == ["seller", "seller", "seller"]
    assert [l.budget for l in leads] == ["12 million AED", "2.5 million AED", "15,000 AED"]


def test_three_pending_listings_each_from_its_own_description(omars_chat):
    listings = app_module.Listing.query.filter_by(agency_id=omars_chat["agency"].id).order_by(
        app_module.Listing.id).all()
    assert [l.location for l in listings] == ["Palm Jumeirah", "Dubai Marina", "Jumeirah Village Circle"]
    assert [l.listing_purpose for l in listings] == ["sale", "sale", "rent"]
    assert all(l.status == "pending" for l in listings)
    assert len(omars_chat["seen"]) == 3


def test_each_lead_has_its_own_timeline(omars_chat):
    assert [l.timeline for l in omars_chat["leads"]] == ["1_3_months", "browsing", "1_3_months"]


def test_the_agency_hears_about_each_property(omars_chat):
    assert sum("New Seller Lead" in s for s in omars_chat["sent"]) == 3


def test_contact_details_are_asked_for_once(omars_chat):
    later = omars_chat["model"].prompts[12:]
    assert all("CONTACT DETAILS: complete" in p for p in later)
    assert all("THE CONTACT STEP - when you reach it" not in p for p in later)


def test_each_property_is_known_by_the_type_he_gave_it(omars_chat):
    """"My villa and my apartment": the first is the villa, the second the
    apartment. Read as one property, the longer word won and the villa
    was described to the model as an apartment."""
    first, second = omars_chat["model"].prompts[3], omars_chat["model"].prompts[12]
    assert "Property type: villa (ALREADY ANSWERED" in first
    assert "Property type: apartment (ALREADY ANSWERED" in second


def test_the_owner_close_offers_to_take_another(omars_chat):
    lone_owner_prompt = omars_chat["model"].prompts[-1]
    assert "another property to sell or rent out" in lone_owner_prompt


def test_the_second_is_never_a_duplicate_of_the_first():
    agency = make_agency()
    first = app_module.Lead(agency_id=agency.id, name="O", email="o@example.test", lead_type="seller")
    app_module.db.session.add(first)
    app_module.db.session.commit()
    try:
        # Too little known about the second to tell them apart - still not the first.
        assert app_module.seller_lead_for_property(agency.id, "o@example.test", {},
                                                   taken=[first.id]) is None
        # Another day, nothing else on record: the same lead as before.
        assert app_module.seller_lead_for_property(agency.id, "o@example.test", {}) == first
    finally:
        app_module.db.session.delete(first)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_the_same_property_on_another_day_is_one_lead():
    agency = make_agency()
    lead = app_module.Lead(agency_id=agency.id, name="O", email="o@example.test", lead_type="seller")
    app_module.db.session.add(lead)
    app_module.db.session.commit()
    listing = app_module.Listing(agency_id=agency.id, title="x", location="Dubai Marina",
                                 property_type="apartment", status="pending", seller_lead_id=lead.id)
    app_module.db.session.add(listing)
    app_module.db.session.commit()
    try:
        same = {"location": "Dubai marina", "property_type": "Apartment"}
        other = {"location": "Palm Jumeirah", "property_type": "villa"}
        assert app_module.seller_lead_for_property(agency.id, "O@example.test", same) == lead
        assert app_module.seller_lead_for_property(agency.id, "o@example.test", other) is None
    finally:
        app_module.db.session.delete(listing)
        app_module.db.session.delete(lead)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()
