"""One visitor, two requests - and the way the assistant talks.

15 November 2026. Noor told the assistant she wanted to rent out her villa
AND buy a townhouse. The first half went perfectly: right questions, right
order, lead saved, listing pending, approval email, reply routed to the
agency. The second half fell apart:

  * "Ok now what about the property I want to buy?" was answered with
    "Unfortunately, we don't currently have a property matching your
    requirements" - before she had given any.
  * Having said twice that she wanted to buy, she was asked "are you
    looking to buy or rent?".
  * With her email and WhatsApp number on file she was asked how to reach
    her, said she had already told us, and was asked for the number
    "again, just to confirm".
  * The conversation closed with "your property details have been passed
    to the team" - the owner's closing line, for a buying enquiry.
  * The buying enquiry - a townhouse, 7 to 8 million dollars, within two
    weeks - was never saved. The log said "Seller lead already recorded".

One cause: the conversation was read as a single request, and "rent out"
anywhere in it meant OWNER for good.

Her messages below are verbatim. Her email address is not - a stand-in is
used so a real address does not live in the test suite.
"""
import uuid

import pytest

import app as app_module

from tests.test_route_guards import make_agency


def talk(*texts):
    """Alternating visitor / assistant turns, visitor first."""
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": t}
            for i, t in enumerate(texts)]


NOOR = talk(
    "Hlo",
    "Hello! May I know who I'm speaking with?",
    "I am Noor.",
    "Nice to meet you, Noor! Welcome to Top Class Properties. I'm MOMO and I can help you "
    "buy, sell or rent a property - which brings you in today?",
    "Nice to meet you MOMO. I am here to buy a property and also rent a property.",
    "I can help with both! Let's start with the rental property. What kind of property are "
    "you looking for?",
    "I want to rent out my own property.",
    "Got it! Whereabouts is the property?",
    "Its in Miami city.",
    "Thanks for sharing that, Noor! What kind of property is it - villa, house, apartment?",
    "Its a villa.",
    "Great choice! How many bedrooms does it have?",
    "It has 5 beds.",
    "Thanks for that information! How many bathrooms does the villa have?",
    "6",
    "That's impressive! Anything that makes it stand out - like a pool, garden, solar "
    "features, or parking?",
    "Its a see viewing modern villa on Miami beach. It has pool, 8kv solar system, home "
    "cinema and gym in the basement, rooftop garden with BBQ setup, wide paring area.",
    "That sounds amazing, Noor! A modern villa with all those features will definitely "
    "attract interest. What monthly rent are you asking?",
    "10K $",
    "Thanks for sharing that! How soon are you hoping to have it rented out?",
    "As soon as you find a suitable tenant for me.",
    "No pressure at all; that’s a complete answer! Best way to reach you - WhatsApp, "
    "phone, or email?",
    "Email and whatsapp.",
    "Could you please provide your WhatsApp number?",
    "+65824778886",
    "Thanks for that! Now, could you share your email address?",
    "noor@example.test",
    "Thank you, Noor! Your property details have been passed to the Top Class Properties "
    "team. An agent will review and get in touch about the next steps. I appreciate your "
    "time today!",
    "Ok now what about the property I want to buy?",
    "Unfortunately, we don't currently have a property matching your requirements. I'll "
    "find suitable options and get back to you to plan a viewing.",
    "But I didn't share my requirements yet.",
    "My apologies for the misunderstanding! Please go ahead and share your requirements "
    "for the property you want to buy. What kind of property are you looking for?",
    "I am looking for a town house.",
    "Great choice! Are you looking to buy or rent the townhouse?",
    "As I already told you that I want to buy it.",
    "Thanks for clarifying! Any particular area in mind for the townhouse?",
    "I want it anywhere in USA..",
    "Got it! And what budget are you working with?",
    "its about 7 to 8M $",
    "Thank you for sharing that! How soon are you looking to move?",
    "Within a weak or 2.",
    "No pressure at all, that’s a complete answer! Best way to reach you - WhatsApp, "
    "phone, or email?",
    "I have already shared my email and whatsapp.",
    "You're right, my apologies for that! Just to confirm, could you please provide your "
    "WhatsApp number again?",
    "+65824778886",
)


def after_her_message(number):
    """The conversation as it stood when Noor sent her Nth message."""
    return NOOR[:2 * number - 1]


# ── which request is in hand ─────────────────────────────────────────

def test_the_old_reading_called_the_whole_conversation_an_owners():
    """The bug, stated. Left here so the reason for the rest is on the page."""
    assert app_module.detect_chat_intent(NOOR) == 'rent_out'


def test_renting_out_comes_first_and_buying_is_remembered():
    view = app_module.conversation_view(after_her_message(4))
    assert (view['side'], view['intent']) == ('owner', 'rent_out')
    assert view['several'] is True
    assert view['other']['intent'] == 'buy'
    assert view['other']['finished'] is False


def test_the_owner_part_stays_an_owners_until_it_is_finished():
    for number in range(4, 14):
        view = app_module.conversation_view(after_her_message(number))
        assert view['side'] == 'owner', f"message {number} was read as {view['side']}"
        assert view['moved_on'] is False


def test_the_reply_to_her_email_is_the_one_that_turns_to_buying():
    """She should not have had to ask "now what about the property I want
    to buy?". The message that completes the first request is the moment
    to move, and the code makes that call - not the model."""
    view = app_module.conversation_view(after_her_message(14))
    assert (view['side'], view['intent']) == ('seeker', 'buy')
    assert view['moved_on'] is True
    assert view['other']['finished'] is True


def test_everything_after_that_is_a_buyers():
    for number in range(15, 24):
        view = app_module.conversation_view(after_her_message(number))
        assert (view['side'], view['intent']) == ('seeker', 'buy'), number
        assert view['moved_on'] is False


def test_the_two_requests_do_not_share_their_messages():
    view = app_module.conversation_view(after_her_message(21))
    buying = " ".join(m['content'] for m in view['messages'])
    owning = " ".join(m['content'] for m in view['other']['messages'])
    assert 'town house' in buying and '7 to 8M' in buying
    assert 'villa' not in buying.lower().replace('town house', '')
    assert '10K $' in owning and '5 beds' in owning
    assert '7 to 8M' not in owning


def test_each_request_keeps_its_own_money():
    """Her 8M budget overwrote the villa's 10K rent in the old reading."""
    view = app_module.conversation_view(after_her_message(21))
    buying = app_module.lead_data_for(1, NOOR, view['messages'])
    owning = app_module.lead_data_for(1, NOOR, view['other']['messages'])
    assert buying['budget'] == '8 million USD'
    assert owning['budget'] == '10,000 USD'
    # The person is the same person in both.
    assert buying['email'] == owning['email'] == 'noor@example.test'
    assert buying['name'] == owning['name'] == 'Noor'


def test_what_she_said_about_her_villa_is_not_what_she_wants_to_buy():
    view = app_module.conversation_view(after_her_message(17))
    wanted = app_module.property_facts_in(view['messages'])
    owned = app_module.property_facts_in(view['other']['messages'])
    assert wanted == {'property_type': 'townhouse', 'bedrooms': None, 'bathrooms': None}
    assert owned['property_type'] == 'villa' and owned['bedrooms'] == 5


def test_asking_after_the_second_request_goes_to_it():
    """The path her real conversation took: the owner part closed, and she
    raised the other one herself."""
    history = talk(
        "hi", "Hello! May I know who I'm speaking with?",
        "Sam", "Nice to meet you, Sam! Which brings you in today?",
        "I want to sell my house", "Whereabouts is the property?",
        "Austin", "What price are you hoping for?",
        "900k", "How soon are you hoping to sell?",
        "this spring", "Best way to reach you - WhatsApp, phone, or email?",
        "email", "What's your email address?",
        "sam@example.test", "Thank you, Sam! Your property details have been passed on.",
        "Ok now what about the property I want to buy?",
    )
    view = app_module.conversation_view(history)
    assert (view['side'], view['intent']) == ('seeker', 'buy')
    assert view['other']['intent'] == 'sell' and view['other']['finished'] is True
    assert view['moved_on'] is False        # she asked; nothing to announce
    assert [m['content'] for m in view['messages']] == [history[-1]['content']]


def test_an_unfinished_first_request_is_come_back_to():
    """She switches to buying half way through describing her flat. Nothing
    she said about the flat is lost, and the flat is still waiting."""
    history = talk(
        "hi", "Hello! May I know who I'm speaking with?",
        "Sam", "Nice to meet you, Sam! Which brings you in today?",
        "I want to rent out my flat", "Whereabouts is the property?",
        "Leeds, it has 2 bedrooms", "How many bathrooms?",
        "Actually first - I want to buy a house too",
    )
    view = app_module.conversation_view(history)
    assert (view['side'], view['intent']) == ('seeker', 'buy')
    assert view['other']['finished'] is False
    assert app_module.property_facts_in(view['other']['messages'])['bedrooms'] == 2


def test_she_can_ask_to_go_back_to_the_first_one():
    history = talk(
        "I want to sell my flat and I also want to buy a house", "Whereabouts is the flat?",
        "Actually I want to buy first", "What kind of property are you looking for?",
        "a house", "Any particular area in mind?",
        "can we move on to the other one",
    )
    view = app_module.conversation_view(history)
    assert (view['side'], view['intent']) == ('owner', 'sell')
    assert view['other']['intent'] == 'buy'


def test_renting_one_and_letting_another_in_the_same_breath_is_two_requests():
    """Not a correction: both were said at once."""
    view = app_module.conversation_view(
        talk("I want to rent a flat near work and rent out my house"))
    assert view['several'] is True
    assert (view['side'], view['intent']) == ('owner', 'rent_out')
    assert view['other']['intent'] == 'rent'


def test_an_owner_with_no_price_does_not_hold_the_second_request_up():
    """"I don't know, what would you suggest?" answers the money question.
    Without this the first request could never finish, the model was told
    not to close until it had, and the buying half would never start."""
    history = talk(
        "hi", "Hello! May I know who I'm speaking with?",
        "Sam", "Nice to meet you, Sam! Which brings you in today?",
        "I want to sell my house and I also want to buy a flat", "Whereabouts is the property?",
        "Austin", "What price are you hoping for?",
        "No idea yet, what would you suggest?", "An agent will advise. How soon are you hoping to sell?",
        "soon", "Best way to reach you - WhatsApp, phone, or email?",
        "email", "What's your email address?",
        "sam@example.test",
    )
    view = app_module.conversation_view(history)
    assert (view['side'], view['intent']) == ('seeker', 'buy')
    assert view['moved_on'] is True


def test_a_request_with_no_email_is_never_finished():
    """Nothing can be saved for it, so the conversation must not move on
    as though something had been."""
    history = talk(
        "I want to sell my house and I also want to buy a flat", "Whereabouts is the property?",
        "Austin", "What price are you hoping for?",
        "900k", "Best way to reach you - WhatsApp, phone, or email?",
        "whatsapp", "What's your WhatsApp number?",
        "+1 512 555 0147", "And your email address?",
        "I'd rather not give one",
    )
    view = app_module.conversation_view(history)
    assert view['side'] == 'owner'
    assert view['moved_on'] is False


# ── one request is still one request ─────────────────────────────────

@pytest.mark.parametrize('said', [
    "I want to sell my villa in Miami Beach",
    "I'd like to rent out my apartment",
    "I want to buy a house in Miami",
    "looking to rent a 2 bed flat",
    "Hello there",
])
def test_a_single_request_is_read_exactly_as_before(said):
    history = talk("hi", "Hello! May I know who I'm speaking with?", said)
    view = app_module.conversation_view(history)
    assert view['several'] is False
    assert view['intent'] == app_module.detect_chat_intent(history)
    assert view['messages'] == history            # the whole conversation, not a part
    assert view['other'] is None and view['moved_on'] is False


def test_a_correction_is_not_a_second_request():
    """"I want to rent a property" ... "I mean rent OUT my own" is one
    request, stated twice. Treating it as two would have the assistant
    finish the listing and then ask what she is looking to rent."""
    history = talk(
        "I want to rent a property", "What kind of property are you looking for?",
        "I want to rent out my own property",
    )
    view = app_module.conversation_view(history)
    assert view['several'] is False
    assert view['intent'] == 'rent_out'


@pytest.mark.parametrize('said', [
    "I need a tenant who will rent it for at least a year",
    "It has its own pool and its own parking",
    "The last people who wanted to buy it pulled out",
    "the other property next door went for 2M",
    "I'm looking for a tenant for my apartment",
    "it is currently rented out until March",
    "I want to rent it for 2000 a month",
    "I'd like to rent to students if possible",
    "I'm looking for a valuation of my house first",
    "what about the garden, does that add value?",
])
def test_an_owner_describing_their_property_is_still_an_owner(said):
    """Loose words are everywhere once somebody is describing a property.
    Only saying THEY want one changes sides."""
    history = talk("I want to sell my house", "Whereabouts is the property?", said)
    view = app_module.conversation_view(history)
    assert view['several'] is False, said
    assert view['side'] == 'owner'


@pytest.mark.parametrize('said, wanted', [
    ("I am here to buy a property and also rent a property.", ['buy', 'rent']),
    ("Ok now what about the property I want to buy?", ['buy']),
    ("As I already told you that I want to buy it.", ['buy']),
    ("I'd also like to purchase something smaller", ['buy']),
    ("we are hoping to rent a place nearby", ['rent']),
    ("I'm interested in buying too", ['buy']),
    ("Can I also buy through you?", ['buy']),
    ("I'm looking for a three bed house", [None]),
    ("quiero comprar un piso", ['buy']),
    ("je voudrais acheter une maison", ['buy']),
    ("ich möchte ein Haus kaufen", ['buy']),
    ("vorrei comprare una casa", ['buy']),
    ("ik wil een huis kopen", ['buy']),
])
def test_wanting_a_property_is_recognised(said, wanted):
    assert app_module.seeker_statements_in(said.lower()) == wanted


@pytest.mark.parametrize('said', [
    "I want to rent out my own property.",
    "I want to rent my apartment",
    "we'd like to lease it out",
    "ich möchte mein Haus verkaufen",       # sell, not buy: "ver-kaufen"
    "ik wil mijn huis verkopen",
    "tenants who want to rent long term are best",
])
def test_letting_a_property_is_not_wanting_one(said):
    assert app_module.seeker_statements_in(said.lower()) == []


# ── what the model is told ───────────────────────────────────────────

def test_the_model_is_told_about_the_request_that_is_waiting():
    view = app_module.conversation_view(after_her_message(6))
    note = app_module.two_requests_context(view, "Top Class Properties")
    assert "THIS VISITOR HAS MORE THAN ONE REQUEST" in note
    assert "rent out a property they own" in note
    assert "ALSO want to buy a property" in note
    assert "do NOT say goodbye" in note
    assert "do not start on it yourself" in note


def test_the_model_is_told_to_turn_to_the_second_request():
    view = app_module.conversation_view(after_her_message(14))
    note = app_module.two_requests_context(view, "Top Class Properties")
    assert "YOU HAVE JUST FINISHED ONE OF THIS VISITOR'S REQUESTS" in note
    assert "passed to the Top Class Properties team" in note
    assert "they also want to buy a property" in note
    assert "Do not ask for their name, their email, a number or a contact preference" in note


def test_the_model_is_told_the_first_request_is_closed():
    view = app_module.conversation_view(after_her_message(18))
    note = app_module.two_requests_context(view, "Top Class Properties")
    assert "Finished, already with the Top Class Properties team" in note
    assert "never carry it over" in note
    assert "do not repeat a closing line" in note


def test_a_visitor_with_one_request_gets_no_such_note():
    view = app_module.conversation_view(talk("I want to buy a house"))
    assert app_module.two_requests_context(view, "Any Agency") == ""


def test_buy_and_rent_in_one_breath_is_not_an_answer():
    """Her third message said both. Telling the model the question was
    ALREADY ANSWERED - with the wrong answer - is worse than asking."""
    view = app_module.conversation_view(after_her_message(3))
    assert app_module.purpose_is_clear(view) is False
    assert app_module.purpose_is_clear(
        app_module.conversation_view(talk("I want to buy a house"))) is True


def test_buy_or_rent_is_not_asked_of_someone_who_has_said():
    asked = app_module.buyer_sequence(False, {'step': 'done'})
    known = app_module.buyer_sequence(True, {'step': 'done'})
    assert 'Buying or renting?' in asked
    assert 'Buying or renting?' not in known


# ── the whole conversation, through the real route ───────────────────

class ScriptedModel:
    """Stands in for OpenAI: returns the next line of a script and keeps
    every system prompt it was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []
        self.calls = 0

    def __call__(self, **kwargs):
        self.calls += 1
        self.prompts.append(kwargs['messages'][0]['content'])
        content = self.replies.pop(0)
        message = type("Message", (), {"content": content})()
        return type("Response", (), {"choices": [type("Choice", (), {"message": message})()]})()


HER_MESSAGES = [m['content'] for m in NOOR if m['role'] == 'user'][:14] + [
    "I am looking for a town house.",
    "I want it anywhere in USA..",
    "its about 7 to 8M $",
    "Within a weak or 2.",
]

# What a well-behaved model says back - except the last two lines, where it
# does the thing it did to Noor and is made to say it again.
ITS_REPLIES = [
    "Hello! May I know who I'm speaking with?",
    "Nice to meet you, Noor! I can help you buy, sell or rent - which brings you in today?",
    "I can help with both. Let's start with the rental. What kind of property are you looking for?",
    "Whereabouts is the property?",
    "What kind of property is it - villa, house, apartment?",
    "How many bedrooms does it have?",
    "How many bathrooms does it have?",
    "Anything that makes it stand out - pool, garden, solar, parking?",
    "A rooftop garden and a home cinema will appeal to the right tenant. What monthly rent are you asking?",
    "How soon are you hoping to have it rented out?",
    "Best way to reach you - WhatsApp, phone, or email?",
    "What's your WhatsApp number?",
    "And your email address, so the team can confirm in writing?",
    "Your villa's details are with the team and an agent will be in touch. Now, the property "
    "you'd like to buy - what kind of property are you looking for?",
    "Any particular area in mind?",
    "And what budget are you working with?",
    "How soon are you looking to move?",
    "Just to confirm, could you please provide your WhatsApp number again?",
    "The team will be in touch shortly with townhouses that fit.",
]


@pytest.fixture()
def noors_chat(client, monkeypatch):
    """Runs her conversation through /chat and hands back what happened."""
    agency = make_agency()
    agency.name = "Top Class Properties"
    app_module.db.session.commit()

    model = ScriptedModel(ITS_REPLIES)
    sent = []
    monkeypatch.setattr(app_module.client.chat.completions, "create", model)
    monkeypatch.setattr(app_module, "extract_seller_property", lambda history, intent: {
        "title": "Miami Beach Modern Villa", "location": "Miami city",
        "property_type": "villa", "bedrooms": 5, "bathrooms": 6.0,
        "features": "pool, solar, home cinema, gym, rooftop garden",
        "price_raw": "10K $", "description": "Five-bed modern villa on Miami Beach.",
        "seen": " ".join(m['content'] for m in history),
    })
    summaries = []
    monkeypatch.setattr(app_module, "generate_lead_summary",
                        lambda history, name: summaries.append(
                            " ".join(m['content'] for m in history)) or "summary")
    monkeypatch.setattr(app_module, "send_email_brevo",
                        lambda to, subject, body, reply_to=None: sent.append(subject) or True)

    session_id = f"noor-{uuid.uuid4().hex[:10]}"
    seen, prompts_by_turn = [], []
    for said in HER_MESSAGES:
        before = len(model.prompts)
        res = client.post("/chat", json={"agency_id": agency.id, "message": said,
                                         "session_id": session_id})
        assert res.status_code == 200, res.get_data(as_text=True)
        seen.append(res.get_json()["reply"])
        prompts_by_turn.append(model.prompts[before:])

    def leads():
        return app_module.Lead.query.filter_by(agency_id=agency.id).order_by(
            app_module.Lead.id).all()

    yield {"agency": agency, "seen": seen, "prompts": prompts_by_turn,
           "leads": leads, "summaries": summaries, "sent": sent}

    app_module.ConversationSession.query.filter(
        app_module.ConversationSession.session_key.like(f"%{session_id}")
    ).delete(synchronize_session=False)
    for row in app_module.Listing.query.filter_by(agency_id=agency.id).all():
        app_module.db.session.delete(row)
    for row in leads():
        app_module.db.session.delete(row)
    app_module.db.session.delete(agency)
    app_module.db.session.commit()


def prompt_at(chat, her_message_number):
    return chat["prompts"][her_message_number - 1][0]


def test_both_enquiries_are_saved(noors_chat):
    """The one that mattered most: a 7-8M buyer, lost to a log line."""
    leads = noors_chat["leads"]()
    assert [(l.lead_type, l.budget) for l in leads] == [
        ('seller', '10,000 USD'),
        ('buyer', '8 million USD'),
    ]
    assert {l.email for l in leads} == {'noor@example.test'}
    assert {l.name for l in leads} == {'Noor'}
    assert {l.whatsapp_number for l in leads} == {'+65824778886'}


def test_the_villa_is_listed_once_and_from_her_own_words(noors_chat):
    listings = app_module.Listing.query.filter_by(agency_id=noors_chat["agency"].id).all()
    assert [(l.title, l.status, l.listing_purpose) for l in listings] == [
        ('Miami Beach Modern Villa', 'pending', 'rent')]


def test_each_agent_reads_about_their_own_enquiry(noors_chat):
    """The summary on the seller lead is about the villa. The one on the
    buyer lead is about the townhouse - not a blend of the two."""
    about_the_villa, about_the_townhouse = noors_chat["summaries"]
    assert '10K $' in about_the_villa and 'town house' not in about_the_villa
    assert 'town house' in about_the_townhouse and '7 to 8M' in about_the_townhouse
    assert '10K $' not in about_the_townhouse


def test_the_agency_is_told_about_both(noors_chat):
    subjects = " | ".join(noors_chat["sent"])
    assert "New Seller Lead" in subjects
    assert "New" in subjects and subjects.count("Lead") >= 2


def test_she_is_an_owner_while_she_describes_the_villa(noors_chat):
    prompt = prompt_at(noors_chat, 6)
    assert "PROPERTY OWNER WHO WANTS TO RENT OUT" in prompt
    assert "THIS VISITOR HAS MORE THAN ONE REQUEST" in prompt
    # The owner's closing instruction would have ended the conversation.
    assert "STOP asking questions" not in prompt
    assert "do NOT close the conversation" in prompt


def test_the_turn_she_finishes_the_villa_is_the_turn_it_moves_to_buying(noors_chat):
    prompt = prompt_at(noors_chat, 14)
    assert "YOU HAVE JUST FINISHED ONE OF THIS VISITOR'S REQUESTS" in prompt
    assert "PROPERTY OWNER WHO WANTS TO" not in prompt
    assert "INFORMATION TO COLLECT FROM A BUYER OR RENTER" in prompt


def test_the_buying_half_knows_nothing_about_the_villa(noors_chat):
    captured = prompt_at(noors_chat, 16).split("ALREADY CAPTURED FROM THIS CUSTOMER")[1]
    captured = captured.split("GOLDEN RULE")[0]
    assert "Property type: townhouse" in captured
    assert "villa" not in captured.lower()
    assert "Bedrooms: 5" not in captured
    assert "10,000" not in captured
    assert "They are looking to buy" in captured
    assert "Name: Noor" in captured and "noor@example.test" in captured


def test_buy_or_rent_is_not_on_the_list_for_her(noors_chat):
    assert "Buying or renting?" not in prompt_at(noors_chat, 15)


def test_her_contact_details_are_not_on_the_list_again(noors_chat):
    """Asked once, held, and absent from the second request's sequence."""
    for number in (14, 15, 16, 17, 18):
        prompt = prompt_at(noors_chat, number)
        assert "CONTACT DETAILS: complete" in prompt
        sequence = prompt.split("INFORMATION TO COLLECT FROM A BUYER OR RENTER")[1]
        sequence = sequence.split("WHY THE TIMELINE MATTERS")[0].lower()
        for word in ("contact", "email", "whatsapp", "phone"):
            assert word not in sequence, f"message {number}: the list still mentions {word}"


def test_a_reply_that_asks_for_her_number_again_is_sent_back(noors_chat):
    """The model does to the script what it did to Noor. She never sees it."""
    assert len(noors_chat["prompts"][17]) == 2          # asked twice on that turn
    assert "WhatsApp number" not in noors_chat["seen"][17]
    assert noors_chat["seen"][17] == "The team will be in touch shortly with townhouses that fit."


def test_nothing_she_saw_asked_for_details_she_had_given(noors_chat):
    for reply in noors_chat["seen"][13:]:
        assert not app_module.asks_for_contact_details(reply), reply


def test_the_path_her_real_conversation_took(client, monkeypatch):
    """Nothing said about buying until the owner part had closed - then
    "now what about the property I want to buy?". That message used to be
    answered as an owner's. It has to start a buyer's conversation, with a
    listing to offer and a lead at the end of it."""
    agency = make_agency()
    listing = app_module.Listing(
        agency_id=agency.id, title="Austin Townhouse", location="Austin, TX",
        price=650000, price_numeric=650000, bedrooms=3, bathrooms=2.0,
        property_type='Townhouse', status='available', listing_purpose='sale')
    app_module.db.session.add(listing)
    app_module.db.session.commit()

    said_and_answered = [
        ("hi", "Hello! May I know who I'm speaking with?"),
        ("Sam", "Nice to meet you, Sam! Which brings you in today?"),
        ("I want to sell my house", "Whereabouts is the property?"),
        ("Dallas", "What price are you hoping for?"),
        ("900k $", "How soon are you hoping to sell?"),
        ("within a month", "Best way to reach you - WhatsApp, phone, or email?"),
        ("email", "What's your email address?"),
        ("sam.two@example.test", "Thank you, Sam. Your property details have been passed on."),
        ("Ok now what about the property I want to buy?", "What kind of property are you looking for?"),
        ("a townhouse", "Any particular area in mind?"),
        ("Austin", "And what budget are you working with?"),
        ("700k $", "How soon are you looking to move?"),
    ]
    model = ScriptedModel([answer for _, answer in said_and_answered])
    monkeypatch.setattr(app_module.client.chat.completions, "create", model)
    monkeypatch.setattr(app_module, "extract_seller_property", lambda history, intent: {
        "title": "Dallas House", "location": "Dallas", "property_type": "house",
        "bedrooms": None, "bathrooms": None, "features": "", "price_raw": "900k $",
        "description": "A house in Dallas."})
    monkeypatch.setattr(app_module, "generate_lead_summary", lambda history, name: "summary")
    monkeypatch.setattr(app_module, "send_email_brevo", lambda *a, **k: True)

    session_id = f"sam-{uuid.uuid4().hex[:10]}"
    try:
        for said, _ in said_and_answered:
            res = client.post("/chat", json={"agency_id": agency.id, "message": said,
                                             "session_id": session_id})
            assert res.status_code == 200, res.get_data(as_text=True)

        # While he was only selling, nothing about this was any different.
        assert "THIS VISITOR HAS MORE THAN ONE REQUEST" not in model.prompts[4]
        assert "PROPERTY OWNER WHO WANTS TO SELL" in model.prompts[4]
        assert "STOP asking questions" in model.prompts[4]

        asked_about_buying = model.prompts[8]
        assert "PROPERTY OWNER WHO WANTS TO" not in asked_about_buying
        assert "Finished, already with the" in asked_about_buying
        assert "Austin Townhouse" in asked_about_buying         # something to offer
        assert "VIEWING AVAILABILITY" in asked_about_buying     # and a calendar
        assert "CONTACT DETAILS: complete" in asked_about_buying
        captured = asked_about_buying.split("ALREADY CAPTURED")[1].split("GOLDEN RULE")[0]
        assert "900" not in captured                            # his asking price is not his budget
        assert "They are looking to buy" in captured

        leads = app_module.Lead.query.filter_by(agency_id=agency.id).order_by(
            app_module.Lead.id).all()
        assert [(l.lead_type, l.budget) for l in leads] == [
            ('seller', '900,000 USD'), ('buyer', '700,000 USD')]
    finally:
        app_module.ConversationSession.query.filter(
            app_module.ConversationSession.session_key.like(f"%{session_id}")
        ).delete(synchronize_session=False)
        for row in app_module.Listing.query.filter_by(agency_id=agency.id).all():
            app_module.db.session.delete(row)
        for row in app_module.Lead.query.filter_by(agency_id=agency.id).all():
            app_module.db.session.delete(row)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


# ── asking again ─────────────────────────────────────────────────────

@pytest.mark.parametrize('reply', [
    "Just to confirm, could you please provide your WhatsApp number again?",
    "Best way to reach you - WhatsApp, phone, or email?",
    "Could you share your email address?",
    "What's your phone number?",
    "How can I reach you?",
])
def test_a_question_about_contact_details_is_recognised(reply):
    assert app_module.asks_for_contact_details(reply) is True


@pytest.mark.parametrize('reply', [
    "And what's your budget?",
    "An agent will email you shortly. Is there anything else I can help with?",
    "I'll send the details to your WhatsApp number.",
    "How soon are you looking to move?",
    "The team will contact you about next steps.",
])
def test_everything_else_is_left_alone(reply):
    assert app_module.asks_for_contact_details(reply) is False


def test_a_failed_second_attempt_keeps_the_first_reply(monkeypatch):
    def broken(**kwargs):
        raise RuntimeError("model unavailable")
    monkeypatch.setattr(app_module.client.chat.completions, "create", broken)
    first = "Could you share your email address?"
    assert app_module.reply_without_contact_question([], first) == first


# ── the way it talks ─────────────────────────────────────────────────

@pytest.mark.parametrize('she_said, it_said, she_sees', [
    ("I want to rent out my own property.", "Got it! Whereabouts is the property?",
     "Whereabouts is the property?"),
    ("Its in Miami city.",
     "Thanks for sharing that, Noor! What kind of property is it - villa, house, apartment?",
     "What kind of property is it - villa, house, apartment?"),
    ("Its a villa.", "Great choice! How many bedrooms does it have?",
     "How many bedrooms does it have?"),
    ("It has 5 beds.", "Thanks for that information! How many bathrooms does the villa have?",
     "How many bathrooms does the villa have?"),
    ("6", "That's impressive! Anything that makes it stand out - like a pool or a garden?",
     "Anything that makes it stand out - like a pool or a garden?"),
    ("10K $", "Thanks for sharing that! How soon are you hoping to have it rented out?",
     "How soon are you hoping to have it rented out?"),
    ("+65824778886", "Thanks for that! Now, could you share your email address?",
     "Now, could you share your email address?"),
    ("I want it anywhere in USA..", "Got it! And what budget are you working with?",
     "And what budget are you working with?"),
    ("its about 7 to 8M $", "Thank you for sharing that! How soon are you looking to move?",
     "How soon are you looking to move?"),
])
def test_the_empty_opener_is_taken_off(she_said, it_said, she_sees):
    """Every one of these is a line Noor was sent."""
    history = [{"role": "user", "content": she_said}]
    assert app_module.tidy_reply(it_said, history) == she_sees


def test_the_prompts_own_words_do_not_reach_the_visitor():
    """ "No pressure at all; that's a complete answer!" was the instructions
    leaking - and "no pressure" to someone who wants a tenant as soon as
    possible has the meaning backwards as well."""
    history = [{"role": "user", "content": "As soon as you find a suitable tenant for me."}]
    said = ("No pressure at all; that’s a complete answer! Best way to reach you - "
            "WhatsApp, phone, or email?")
    assert app_module.tidy_reply(said, history) == \
        "Best way to reach you - WhatsApp, phone, or email?"


def test_a_real_reaction_to_a_real_description_is_kept():
    history = [{"role": "user", "content":
                "It has pool, 8kv solar system, home cinema and gym in the basement, "
                "rooftop garden with BBQ setup, wide parking area."}]
    said = "That sounds amazing, Noor! What monthly rent are you asking?"
    assert app_module.tidy_reply(said, history) == said


def test_but_not_two_in_a_row():
    history = [
        {"role": "user", "content": "It is a big modern place right on the beach with a lot of light"},
        {"role": "assistant", "content": "That sounds lovely! How many bedrooms does it have?"},
        {"role": "user", "content": "Five bedrooms, all of them with a balcony and a sea view"},
    ]
    assert app_module.tidy_reply("That sounds wonderful! How many bathrooms?", history) == \
        "How many bathrooms?"


def test_a_goodbye_keeps_its_thank_you():
    """No question follows, so it is not filler in front of one."""
    history = [{"role": "user", "content": "noor@example.test"}]
    said = "Thank you, Noor! Your property details have been passed to the team."
    assert app_module.tidy_reply(said, history) == said


def test_warmth_is_left_alone_when_somebody_hesitates():
    history = [{"role": "user", "content": "I'm not sure, it feels expensive"}]
    said = "No pressure at all. What feels comfortable?"
    assert app_module.tidy_reply(said, history) == said


@pytest.mark.parametrize('said', [
    "Nice to meet you, Noor! Which brings you in today?",
    "My apologies for the misunderstanding! What kind of property are you looking for?",
    "Great news - we have three that fit. Would you like to know more?",
    "¡Perfecto! ¿Cuántas habitaciones tiene?",
    "Whereabouts is the property?",
])
def test_ordinary_replies_pass_through_untouched(said):
    assert app_module.tidy_reply(said, [{"role": "user", "content": "ok"}]) == said


def test_the_instructions_no_longer_ask_for_an_acknowledgement_every_time(noors_chat):
    for prompt in (prompt_at(noors_chat, 6), prompt_at(noors_chat, 16)):
        assert "Acknowledge what they said before responding" not in prompt
        assert "Go straight to your next question" in prompt
        assert "Never compliment a plain fact" in prompt
        # The phrases the model was repeating back to visitors.
        assert "complete answer" not in prompt
        assert "acceptable answer" not in prompt
        # And the old order, which two lines still described.
        assert "email, then contact preference" not in prompt


def test_nothing_is_called_unmatched_before_she_has_asked_for_anything(noors_chat):
    assert "Never tell them nothing matches before they have said what they are looking for" \
        in prompt_at(noors_chat, 15)


# ── when she wants it ────────────────────────────────────────────────

def test_as_soon_as_you_find_a_tenant_is_a_timeline():
    """Her answer was scored "Timeline: not given"."""
    history = talk("x", "How soon are you hoping to have it rented out?",
                   "As soon as you find a suitable tenant for me.")
    assert app_module.extract_timeline(history)[0] == 'immediate'


def test_a_weak_or_two_is_a_week_or_two_when_it_answers_how_soon():
    history = talk("x", "How soon are you looking to move?", "Within a weak or 2.")
    assert app_module.extract_timeline(history)[0] == 'immediate'


def test_a_weak_market_is_not_a_timeline():
    assert app_module.extract_timeline(talk("it has been a weak market lately")) == (None, None)


# ── two leads, one person ────────────────────────────────────────────

def test_a_lead_is_looked_up_on_its_own_side():
    agency = make_agency()
    seller = app_module.Lead(agency_id=agency.id, name="Noor", email="two@example.test",
                             lead_type='seller', notes='[]')
    app_module.db.session.add(seller)
    app_module.db.session.commit()
    try:
        assert app_module.lead_on_file(agency.id, "two@example.test", 'seller').id == seller.id
        assert app_module.lead_on_file(agency.id, "two@example.test", 'buyer') is None
        assert app_module.lead_on_file(agency.id, None, 'buyer') is None
    finally:
        app_module.db.session.delete(seller)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()


def test_a_lead_saved_before_types_existed_counts_as_a_buyer():
    agency = make_agency()
    old = app_module.Lead(agency_id=agency.id, name="Old", email="old@example.test", notes='[]')
    app_module.db.session.add(old)
    app_module.db.session.commit()
    old.lead_type = None
    app_module.db.session.commit()
    try:
        assert app_module.lead_on_file(agency.id, "old@example.test", 'buyer').id == old.id
    finally:
        app_module.db.session.delete(old)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()
