"""The contact step: preference first, then the detail that preference needs.

From a real seller transcript (Nov 2026). Four things went wrong in one
conversation:

  1. The owner's first answer was "I want to rent out my apartment" and the
     AI still asked "what kind of property is it - a villa, a house, or an
     apartment?". Nothing fed the property facts back into the prompt's
     ALREADY CAPTURED list, so his own words never counted as an answer.
  2. The chat closed with no phone or WhatsApp number in it at all.
  3. "What's the best way to reach you?" was asked AFTER the email address
     had already been collected - a question whose answer arrived too late
     to change anything, and which nobody acted on when it did.
  4. The approval email told the seller to "just reply to this email", and
     the reply went to the platform's own support inbox rather than to the
     agency that has his listing.
"""
import app as app_module


def turn(role, content):
    return {"role": role, "content": content}


PREF_QUESTION = "Best way to reach you - WhatsApp, phone, or email?"


# ── 1. their own words count as an answer ────────────────────────────

def test_property_type_read_from_the_owners_opening_line():
    history = [
        turn("user", "hi"),
        turn("assistant", "Hello! May I know who I'm speaking with?"),
        turn("user", "Imran"),
        turn("assistant", "Nice to meet you, Imran! Which brings you in today?"),
        turn("user", "I want to rent out my apartment"),
    ]
    facts = app_module.property_facts_in(history)
    assert facts["property_type"] == "apartment"


def test_the_assistants_own_question_is_never_read_as_an_answer():
    """The question itself names all three types. Reading assistant turns
    would capture one of them and skip the question that was just asked."""
    history = [
        turn("assistant", "What kind of property is it - a villa, a house, or an apartment?"),
    ]
    assert app_module.property_facts_in(history)["property_type"] is None


def test_bedrooms_and_bathrooms_in_their_own_words():
    history = [turn("user", "it's a 3-bedroom place with 2.5 bathrooms")]
    facts = app_module.property_facts_in(history)
    assert facts["bedrooms"] == 3
    assert facts["bathrooms"] == 2.5


def test_written_out_numbers_count_too():
    facts = app_module.property_facts_in([turn("user", "four bedrooms, two baths")])
    assert facts["bedrooms"] == 4
    assert facts["bathrooms"] == 2


def test_real_estate_is_not_a_property_type():
    """'estate' is a real type, but 'real estate' is the industry."""
    assert app_module.property_facts_in(
        [turn("user", "I need help with real estate in Miami")])["property_type"] is None
    assert app_module.property_facts_in(
        [turn("user", "we have an estate in Surrey")])["property_type"] == "estate"


def test_the_most_specific_type_word_wins():
    assert app_module.property_facts_in(
        [turn("user", "it's a town house")])["property_type"] == "townhouse"


def test_a_flat_is_an_apartment():
    assert app_module.property_facts_in(
        [turn("user", "renting out my flat")])["property_type"] == "apartment"


def test_nothing_is_invented_when_nothing_was_said():
    facts = app_module.property_facts_in([turn("user", "just looking around for now")])
    assert facts == {"property_type": None, "bedrooms": None, "bathrooms": None}


# ── 2 + 3. the order of the contact step ─────────────────────────────

def test_the_preference_is_asked_before_any_contact_detail():
    plan = app_module.contact_plan([turn("user", "sounds good")], known={})
    assert plan["step"] == "ask_preference"


def test_whatsapp_chosen_means_the_number_is_asked_next():
    history = [
        turn("assistant", PREF_QUESTION),
        turn("user", "WhatsApp please"),
    ]
    plan = app_module.contact_plan(history, known={})
    assert plan["channel"] == "whatsapp"
    assert plan["step"] == "ask_whatsapp"


def test_phone_chosen_means_the_phone_number_is_asked_next():
    history = [turn("assistant", PREF_QUESTION), turn("user", "a call is easiest")]
    plan = app_module.contact_plan(history, known={})
    assert plan["channel"] == "phone"
    assert plan["step"] == "ask_phone"


def test_email_chosen_goes_straight_to_the_address():
    history = [turn("assistant", PREF_QUESTION), turn("user", "email is fine")]
    plan = app_module.contact_plan(history, known={})
    assert plan["channel"] == "email"
    assert plan["step"] == "ask_email"


def test_the_number_is_still_wanted_after_an_email_is_already_on_file():
    """The live bug. The email was collected first, the preference question
    came afterwards, the answer was WhatsApp - and because an email already
    existed the step was treated as finished and no number was ever asked."""
    history = [
        turn("assistant", "Could you share your email address?"),
        turn("user", "imran@example.com"),
        turn("assistant", PREF_QUESTION),
        turn("user", "WhatsApp"),
    ]
    plan = app_module.contact_plan(history, known={"email": "imran@example.com"})
    assert plan["step"] == "ask_whatsapp"


def test_the_email_is_asked_after_the_number():
    history = [
        turn("assistant", PREF_QUESTION),
        turn("user", "WhatsApp"),
        turn("assistant", "What's your WhatsApp number?"),
        turn("user", "+971 50 123 4567"),
    ]
    plan = app_module.contact_plan(history, known={"whatsapp_number": "+971 50 123 4567"})
    assert plan["step"] == "ask_email"


def test_the_step_is_done_once_both_are_in():
    history = [
        turn("assistant", PREF_QUESTION),
        turn("user", "WhatsApp"),
        turn("assistant", "What's your WhatsApp number?"),
        turn("user", "+971 50 123 4567"),
        turn("assistant", "And the best email for the confirmation?"),
        turn("user", "imran@example.com"),
    ]
    plan = app_module.contact_plan(
        history, known={"email": "imran@example.com", "whatsapp_number": "+971501234567"})
    assert plan["step"] == "done"


def test_a_declined_number_ends_the_number_ask():
    history = [
        turn("assistant", PREF_QUESTION),
        turn("user", "whatsapp"),
        turn("assistant", "What's your WhatsApp number?"),
        turn("user", "I'd rather not share it"),
    ]
    plan = app_module.contact_plan(history, known={"email": "imran@example.com"})
    assert plan["declined_number"] is True
    assert plan["step"] == "done"


def test_a_number_is_not_a_refusal():
    """'no' is a substring of 'number', 'now' and 'nothing', so substring
    matching read "my number is 555..." as a decline."""
    assert app_module.declines_to_share("my number is +44 7700 900123") is False
    assert app_module.declines_to_share("no problem, 0300 1234567") is False
    assert app_module.declines_to_share("no thanks") is True
    assert app_module.declines_to_share("rather not") is True


def test_a_vague_answer_names_no_channel():
    assert app_module.channel_in("whatever is easiest for you") is None
    assert app_module.channel_in("WhatsApp") == "whatsapp"
    assert app_module.channel_in("you can call me") == "phone"
    assert app_module.channel_in("e-mail") == "email"


def test_email_and_whatsapp_still_needs_the_number():
    history = [turn("assistant", PREF_QUESTION), turn("user", "email and whatsapp both")]
    plan = app_module.contact_plan(history, known={"email": "imran@example.com"})
    assert plan["step"] == "ask_whatsapp"


# ── the prompt the model actually receives ───────────────────────────

def test_the_close_gate_is_in_the_prompt_until_the_step_is_finished():
    unfinished = app_module.contact_context(
        app_module.contact_plan([turn("assistant", PREF_QUESTION), turn("user", "whatsapp")],
                                known={}))
    assert "WhatsApp number" in unfinished
    assert "Do NOT end the conversation" in unfinished


def test_a_finished_step_tells_the_model_to_stop_asking():
    done = app_module.contact_context({"step": "done"})
    assert "complete" in done
    assert "Never ask" in done


def test_the_buyer_sequence_puts_the_preference_before_the_email():
    source = open(app_module.__file__, encoding="utf-8").read()
    buyer = source.split("INFORMATION TO COLLECT FROM A BUYER OR RENTER")[1][:1600]
    assert buyer.index("Contact preference FIRST") < buyer.index("Email address, if you still")


def test_the_seller_sequence_puts_the_preference_before_the_email():
    source = open(app_module.__file__, encoding="utf-8").read()
    seller = source.split("INFORMATION TO COLLECT FROM THIS OWNER")[1][:2200]
    assert seller.index("Contact preference FIRST") < seller.index("Email address, if you still")


# ── 4. a reply from the seller must reach the agency ─────────────────

def test_customer_mail_replies_to_the_agency_not_to_us(monkeypatch):
    sent = {}

    class FakeResponse:
        status_code = 201
        text = ""

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.update(json or {})
        return FakeResponse()

    monkeypatch.setenv("BREVO_API_KEY", "test-key")
    monkeypatch.setattr(app_module.httpx, "post", fake_post)

    app_module.send_email_brevo("seller@example.com", "Your property is now listed",
                                "body", reply_to="agency@example.com")
    assert sent["replyTo"]["email"] == "agency@example.com"


def test_platform_mail_still_replies_to_support(monkeypatch):
    sent = {}

    class FakeResponse:
        status_code = 201
        text = ""

    def fake_post(url, headers=None, json=None, timeout=None):
        sent.update(json or {})
        return FakeResponse()

    monkeypatch.setenv("BREVO_API_KEY", "test-key")
    monkeypatch.setattr(app_module.httpx, "post", fake_post)

    app_module.send_email_brevo("owner@example.com", "New lead", "body")
    assert sent["replyTo"]["email"] == app_module.REPLY_TO_EMAIL


def test_the_seller_approval_email_passes_the_agency_address():
    """The email that says "just reply to this email" has to carry a
    reply-to the seller's reply can actually land in."""
    source = open(app_module.__file__, encoding="utf-8").read()
    block = source.split("Your property is now listed with")[1][:900]
    assert "just reply to this email" in block
    assert "reply_to=agency.email" in block


def test_the_appointment_confirmation_replies_to_the_agency():
    source = open(app_module.__file__, encoding="utf-8").read()
    block = source.split("sent_customer = send_email_brevo(")[1][:260]
    assert "reply_to=agency.email" in block
