"""One visitor, a viewing booked, then a property to sell.

16 November 2026. Fimi wanted to buy a villa, booked a viewing of one, then
said "I also want sell my property" and described a townhouse in New York.
Separate leads came out of it and emails went - and almost every figure on
them was wrong:

  * The seller lead was saved the moment she said she wanted to sell, from
    the WHOLE conversation: its pending listing was the agency's own Miami
    villa she had just asked to view, and its asking price the villa's
    8 million. Her townhouse at 6 million was never recorded.
  * "Actually I I want buy a villa" did not read as a request to buy
    ("want buy", no "to"), so the conversation was taken to be an owner's
    from start to finish.
  * She was told "You're booked" for Tuesday at 4 PM. No appointment was
    made and no confirmation sent: "I would love to" was not on the list
    of yeses, and nothing else counted.
  * The buyer was never asked how soon she wanted to move - the reply after
    the booking went straight to "Best way to reach you?".
  * "As soon as you find a suitable customer for me" was understood, but
    the seller lead had been saved before she said it, and never updated.

Her messages are verbatim except her email address, and the viewing day,
which moves to the next real Tuesday so the booking stays in the future.
"""
import json
import uuid
from datetime import date, timedelta

import pytest

import app as app_module

from tests.test_route_guards import make_agency


def talk(*texts):
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": t}
            for i, t in enumerate(texts)]


def next_tuesday():
    day = date.today() + timedelta(days=1)
    while day.weekday() != 1:
        day += timedelta(days=1)
    return day


TUESDAY = next_tuesday()
TUESDAY_TEXT = f"Tuesday, {TUESDAY.strftime('%B')} {TUESDAY.day}"
VILLA = ("1. Miami Beachside Modern Luxury Villa [FOR SALE] | Miami beachside | $8,000,000 | "
         "6bed/7bath | double garage, swimming pool, home cinema - This modern luxury villa "
         "has everything you need and is located right on the beachside.")

HER = [
    "Hlo.",
    "I am Fimi.",
    "Actually I I want buy a villa.",
    "I want it at Miami baech.",
    "Ya sure.",
    "I would love to.",
    TUESDAY_TEXT,
    "4 PM",
    "Within a month",
    "Email",
    "fimi@example.test",
    "I also want sell my property.",
    "In NewYork City.",
    "Its a town house.",
    "It has 7 beds.",
    "8",
    "It has a pool, double garage, situated at prime location in NewYork City.",
    "6M $.",
    "I want it to sell as soon as you find a suitable customer for me.",
    "Thank u.",
]

# What she was told, in order - with one difference. After she picked
# 4 PM the model said "You're booked... Best way to reach you?", as it did
# to her. The code now sends that reply back once; the second line here is
# the rewrite it asks for, and "Within a month" is her answer to it.
ITS_REPLIES = [
    "Hello! May I know who I'm speaking with?",
    "Nice to meet you, Fimi! I can help you buy, sell or rent a property - which brings you in today?",
    "Any particular area in mind for your villa?",
    "I have some properties that match your criteria.\n" + VILLA + "\nWould you like to know more?",
    "Would you like to see it in person?",
    "Which day works for you? Here are the available options:\n- " + TUESDAY_TEXT
    + "\n- Wednesday\n- Thursday",
    f"Here are the available time slots for {TUESDAY_TEXT}:\n10:00 AM, 12:00 PM, 2:00 PM, "
    "4:00 PM, 6:00 PM.\nWhat time works best for you?",
    f"You're booked for the Miami Beachside Modern Luxury Villa on {TUESDAY_TEXT} at 4:00 PM.\n"
    "Best way to reach you - WhatsApp, phone, or email?",
    f"You're booked for {TUESDAY_TEXT} at 4:00 PM. How soon are you looking to move?",
    "Best way to reach you - WhatsApp, phone, or email?",
    "Could you please provide your email address?",
    f"Thank you, Fimi. You're all set for your viewing on {TUESDAY_TEXT} at 4:00 PM.",
    "Whereabouts is the property you're looking to sell?",
    "What type of property is it?",
    "How many bedrooms does it have?",
    "How many bathrooms does it have?",
    "Anything that makes it stand out - like a pool, garden, solar, or parking?",
    "What price are you hoping for?",
    "How soon are you hoping to sell?",
    "Your property details have been passed to the Top Class Properties team. An agent will "
    "review and get in touch about next steps. Thank you!",
    "You're welcome, Fimi! If you need anything else, feel free to reach out.",
]


class ScriptedModel:
    """Stands in for OpenAI. Returns the next line of the script, and keeps
    every set of messages it was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs['messages'])
        message = type("Message", (), {"content": self.replies.pop(0)})()
        return type("Response", (), {"choices": [type("Choice", (), {"message": message})()]})()


@pytest.fixture()
def fimis_chat(client, monkeypatch):
    agency = make_agency()
    agency.name = "Top Class Properties"
    villa = app_module.Listing(
        agency_id=agency.id, title="Miami Beachside Modern Luxury Villa",
        location="Miami beachside", price=8000000, price_numeric=8000000,
        price_raw="$8,000,000", bedrooms=6, bathrooms=7.0, property_type="villa",
        listing_purpose="sale", status="available")
    app_module.db.session.add(villa)
    app_module.db.session.commit()

    model = ScriptedModel(ITS_REPLIES)
    monkeypatch.setattr(app_module.client.chat.completions, "create", model)
    extractor_saw = []

    def extract(history, intent):
        said = " | ".join(m['content'] for m in history if m['role'] == 'user')
        extractor_saw.append(said)
        return {"title": "New York Townhouse", "location": "New York City",
                "property_type": "townhouse", "bedrooms": 7, "bathrooms": 8.0,
                "features": "pool, double garage", "price_raw": "6M $", "description": ""}
    monkeypatch.setattr(app_module, "extract_seller_property", extract)
    monkeypatch.setattr(app_module, "generate_lead_summary", lambda history, name: "summary")
    sent = []
    monkeypatch.setattr(app_module, "send_email_brevo",
                        lambda to, subject, body, reply_to=None: sent.append(subject) or True)

    session_id = f"fimi-{uuid.uuid4().hex[:10]}"
    shown = []
    for said in HER:
        res = client.post("/chat", json={"agency_id": agency.id, "message": said,
                                         "session_id": session_id})
        assert res.status_code == 200, res.get_data(as_text=True)
        shown.append(res.get_json()["reply"])

    def leads(kind):
        return app_module.Lead.query.filter_by(agency_id=agency.id, lead_type=kind).all()

    yield {"agency": agency, "model": model, "shown": shown, "sent": sent,
           "extractor_saw": extractor_saw, "leads": leads}

    app_module.ConversationSession.query.filter(
        app_module.ConversationSession.session_key.like(f"%{session_id}")
    ).delete(synchronize_session=False)
    for model_ in (app_module.Appointment, app_module.Listing, app_module.Lead):
        for row in model_.query.filter_by(agency_id=agency.id).all():
            app_module.db.session.delete(row)
    app_module.db.session.delete(agency)
    app_module.db.session.commit()


# ── through the real route ────────────────────────────────────────────

def test_one_buyer_lead_and_one_seller_lead(fimis_chat):
    assert len(fimis_chat["leads"]("buyer")) == 1
    assert len(fimis_chat["leads"]("seller")) == 1


def test_the_seller_lead_is_her_townhouse_not_the_villa_she_viewed(fimis_chat):
    seen = fimis_chat["extractor_saw"]
    assert seen, "the property was never extracted"
    for said in seen:
        assert "NewYork" in said
        assert "Miami" not in said and "villa" not in said.lower()


def test_her_asking_price_is_what_she_asked(fimis_chat):
    seller = fimis_chat["leads"]("seller")[0]
    assert seller.budget == "6 million USD"
    listing = app_module.Listing.query.filter_by(seller_lead_id=seller.id).one()
    assert listing.price_raw == "6M $" and listing.status == "pending"


def test_her_selling_timeline_reaches_the_lead_after_it_was_saved(fimis_chat):
    seller = fimis_chat["leads"]("seller")[0]
    assert seller.timeline == "immediate"
    assert "suitable customer" in seller.timeline_raw


def test_the_viewing_she_was_told_about_is_booked(fimis_chat):
    appts = app_module.Appointment.query.filter_by(agency_id=fimis_chat["agency"].id).all()
    assert len(appts) == 1
    assert appts[0].appointment_date_iso == TUESDAY.isoformat()
    assert appts[0].appointment_time == "4:00 PM"
    assert any("Appointment Confirmed" in s for s in fimis_chat["sent"])


def test_the_viewing_belongs_to_her_buyer_lead(fimis_chat):
    buyer = fimis_chat["leads"]("buyer")[0]
    appt = app_module.Appointment.query.filter_by(agency_id=fimis_chat["agency"].id).one()
    assert appt.lead_id == buyer.id
    assert "Booked a viewing" in buyer.quality_reasons


def test_the_buyer_was_asked_how_soon(fimis_chat):
    buyer = fimis_chat["leads"]("buyer")[0]
    assert buyer.timeline == "immediate"
    assert "How soon are you looking to move?" in fimis_chat["shown"][7]


def test_the_reply_that_skipped_the_timeline_was_sent_back_once(fimis_chat):
    retries = [c for c in fimis_chat["model"].calls
               if c[-1]["role"] == "system" and "how soon" in c[-1]["content"].lower()]
    assert len(retries) == 1
    assert "Best way to reach you" in retries[0][-2]["content"]


def test_her_answers_about_selling_stay_with_selling(fimis_chat):
    buyer = fimis_chat["leads"]("buyer")[0]
    assert buyer.budget.startswith("8 million USD")
    assert "suitable customer" not in (buyer.timeline_raw or "")


# ── the pieces ────────────────────────────────────────────────────────

@pytest.mark.parametrize("said", [
    "Actually I I want buy a villa.",
    "i wanna buy a house",
    "I want to buy a villa",
    "We are looking to buy",
])
def test_wanting_to_buy_without_the_grammar(said):
    assert app_module.seeker_statements_in(said.lower()) == ['buy']


def test_her_conversation_is_two_requests():
    history = talk(*[x for pair in zip(HER, ITS_REPLIES[:8] + ITS_REPLIES[9:]) for x in pair])
    view = app_module.conversation_view(history)
    assert view["several"] is True
    keys = {r["key"]: r for r in view["requests"]}
    assert keys["seeker1"]["intent"] == "buy"
    assert keys["owner1"]["intent"] == "sell"
    owning = " ".join(m["content"] for m in keys["owner1"]["messages"] if m["role"] == "user")
    assert "suitable customer" in owning and "Miami" not in owning


def test_an_owner_price_is_never_inferred_from_listings():
    history = talk("I want to sell my house", "Whereabouts is it?", "Miami", "Price?")
    data = app_module.lead_data_for(1, history, history, side='owner')
    assert not data['budget'] and data['budget_inferred'] is False


def test_an_inferred_price_does_not_qualify_a_seller():
    data = {'email': 'a@example.test', 'name': 'A', 'budget': '8 million USD (based on '
            'properties discussed)', 'budget_inferred': True}
    history = talk("hi", "?", "I want to sell my flat", "?", "Miami", "?", "flat", "?")
    assert app_module.is_seller_lead_qualified(data, history, 'sell') is False


@pytest.mark.parametrize("answer", [
    "I want it to sell as soon as you find a suitable customer for me.",
    "as soon as you find a suitable buyer",
    "as soon as you find me a tenant",
])
def test_leaving_it_to_the_agency_means_now(answer):
    bucket, _ = app_module.extract_timeline(talk("x", "How soon are you hoping to sell?", answer)[1:])
    assert bucket == "immediate"


@pytest.mark.parametrize("yes", ["I would love to.", "I'd love to", "Ya sure", "Yup", "Please"])
def test_these_answer_a_viewing_offer(yes):
    history = talk("hi", "Would you like to see it in person?", yes)
    assert app_module.extract_appointment_data(999999, history)["requested"] is True


def test_picking_an_offered_day_is_a_request_in_any_words():
    history = talk(
        "hi", f"Which day works for you?\n- {TUESDAY_TEXT}\n- Wednesday\n- Thursday",
        TUESDAY_TEXT, "Times: 10:00 AM, 12:00 PM, 2:00 PM, 4:00 PM. Which works?",
        "4 PM")
    data = app_module.extract_appointment_data(999999, history)
    assert data["requested"] is True
    assert data["slots"] and data["slots"][0]["time"] == "4:00 PM"


def test_a_day_mentioned_without_an_offer_is_not_a_booking():
    history = talk("hi", "How soon are you looking to move?", "by tuesday ideally")
    assert app_module.extract_appointment_data(999999, history)["requested"] is False


def view_for(*texts):
    return app_module.conversation_view(talk(*texts))


def test_a_reply_that_jumps_to_contact_details_is_caught():
    history = talk("I want to buy a villa", "Area?", "Miami", "Here it is. See it?",
                   "yes", "Which day?", "Tuesday")
    view = app_module.conversation_view(history)
    plan = app_module.contact_plan(history, {})
    reply = "You're booked for Tuesday at 4 PM. Best way to reach you - WhatsApp, phone, or email?"
    assert app_module.skips_the_timeline(reply, view, plan) is True


def test_once_the_timeline_is_asked_nothing_is_caught():
    history = talk("I want to buy a villa", "Area?", "Miami", "How soon are you looking to move?",
                   "next month")
    view = app_module.conversation_view(history)
    plan = app_module.contact_plan(history, {})
    reply = "Best way to reach you - WhatsApp, phone, or email?"
    assert app_module.skips_the_timeline(reply, view, plan) is False


def test_the_opening_questions_are_not_caught():
    history = talk("I want to buy a villa")
    view = app_module.conversation_view(history)
    plan = app_module.contact_plan(history, {})
    assert app_module.skips_the_timeline("What's your email?", view, plan) is False


def test_a_rewrite_that_still_skips_it_is_not_used(monkeypatch):
    model = ScriptedModel(["Great. What's your email?"])
    monkeypatch.setattr(app_module.client.chat.completions, "create", model)
    first = "You're booked. Best way to reach you?"
    assert app_module.reply_with_timeline_question([], first, "How soon?") == first


def test_the_session_remembers_which_lead_each_request_became():
    key = f"test_{uuid.uuid4().hex[:8]}"
    app_module.remember_capture(key, "owner2", 41)
    app_module.remember_capture(key, "seeker1", 40)
    assert app_module.captured_leads(key) == {"owner2": 41, "seeker1": 40}
    app_module.db.session.delete(app_module.db.session.get(app_module.ConversationSession, key))
    app_module.db.session.commit()
