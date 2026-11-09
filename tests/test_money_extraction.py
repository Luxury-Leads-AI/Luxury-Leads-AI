"""What a person says when they say money.

Moaz rented out an apartment to his own chatbot and got nothing: no
lead, no email, no listing waiting for approval. The logs blamed a
missing budget. The budget was missing because he typed **2000$** and
the extractor only understood a dollar sign in front.

That was not a seller bug. `budget` is REQUIRED by the buyer gate and
the seller gate alike, so every miss threw a whole lead away - and the
old patterns were written for luxury SALE prices ("2.5M", "$1.8
million") and missed most of how a person states a number. Rent is
almost always a plain number, so the entire rental side was failing in
both directions.

The two sources of truth here:

  1. money stated explicitly - a sign, a currency word, or a scale word
     makes it unambiguous, and the sign may be on either side;
  2. a bare number answering a question about money. "What monthly rent
     are you asking?" -> "2000". No regex on the user's words alone can
     tell 2000-the-rent from 2000-the-square-feet; the question is what
     makes it readable, and we have the question.

The second half matters as much as the first: "2 beds 3 baths" must
never become a budget of 2.
"""
import pytest

import app as app_module


def chat(*turns):
    """(assistant question, user reply) pairs -> a history."""
    history = []
    for question, answer in turns:
        if question:
            history.append({'role': 'assistant', 'content': question})
        history.append({'role': 'user', 'content': answer})
    return history


MONEY_Q = "And what's your budget, or the monthly rent you're asking?"


# ── the exact conversation that found this ──

JORDAN = [
    ("Hello! May I know who I'm speaking with?", "hi"),
    ("May I know who I'm speaking with?", "I am Jordan."),
    ("I can help you buy, sell, or rent - which brings you in today?",
     "Well Momo I want to rent out my apartment."),
    ("Great! Whereabouts is the property?", "Its in Newyork."),
    ("What kind of property is it - villa, house, apartment?", "Its an aprtment."),
    ("How many bedrooms does it have?", "It has 2 beds 3 baths."),
    ("Anything that makes it stand out - pool, garden, solar, parking?",
     "Solar system and parkin available."),
    ("What monthly rent are you asking?", "2000$"),
    ("How soon are you hoping to have it rented out?", "As early as possible."),
    ("Could you please share your email address?", "sovipib884@macsnipe.com"),
    ("What's the best way to reach you - WhatsApp, phone, or email?", "Email"),
]


def test_jordan_gets_his_rent_read(client):
    """8 November 2026. The whole report in one assertion."""
    history = chat(*JORDAN)
    budget = app_module.budget_from_conversation(history)
    assert budget, "a landlord said 2000$ and the engine heard nothing"
    assert '2,000' in budget


def test_jordan_now_qualifies_as_a_seller(client):
    history = chat(*JORDAN)
    data = app_module.extract_lead_data(1, history)
    intent = app_module.detect_chat_intent(history)
    assert intent == 'rent_out'
    assert app_module.is_seller_lead_qualified(data, history, intent), \
        "the landlord still produces no lead"


def test_the_bedrooms_in_that_chat_were_not_mistaken_for_a_price(client):
    """He said "2 beds 3 baths" two questions before the rent. A reader
    that grabs the first number it sees would have banked 2."""
    history = chat(*JORDAN)
    budget = app_module.budget_from_conversation(history)
    assert '2,000' in budget
    assert budget.strip() not in ('2', '3', '2 USD', '3 USD')


# ── money, stated explicitly ──

@pytest.mark.parametrize('said,expected', [
    ('$2000', '2,000'), ('2000$', '2,000'),
    ('2000 usd', '2,000'), ('USD 2000', '2,000'),
    ('2000 dollars', '2,000'),
    ('£2000', '2,000'), ('€2000', '2,000'), ('AED 7000', '7,000'),
    ('50,000 PKR', '50,000'),
    ('2k', '2,000'), ('$2k', '2,000'), ('500k', '500,000'),
    ('2.5M', '2.5 million'), ('1.2 million', '1.2 million'),
])
def test_money_stated_outright_is_read(said, expected):
    amount, _ = app_module.money_in(said)
    assert amount is not None, f"{said!r} was not read as money at all"
    assert expected in app_module._format_money(amount, '')


@pytest.mark.parametrize('said,code', [
    ('$2000', 'USD'), ('2000$', 'USD'), ('£2000', 'GBP'), ('€2000', 'EUR'),
    ('AED 7000', 'AED'), ('50,000 PKR', 'PKR'), ('2000 dollars', 'USD'),
])
def test_the_currency_comes_through(said, code):
    _, currency = app_module.money_in(said)
    assert currency == code


def test_a_sign_on_either_side_is_the_same_money():
    assert app_module.money_in('$2000') == app_module.money_in('2000$')


# ── a bare number, read from the question it answers ──

@pytest.mark.parametrize('said', [
    '2000', '2,000', 'around 2000', 'about 2000 a month', '2000 per month',
    'rent is 2000', 'asking 2000', 'my budget is 2000', 'no more than 2000',
])
def test_a_plain_number_answering_a_money_question_is_money(said):
    history = chat((MONEY_Q, said))
    assert app_module.budget_from_conversation(history), \
        f"{said!r} after a question about money was not read"


@pytest.mark.parametrize('said', [
    '2 beds 3 baths', '1500 sq ft', '3 bedrooms', 'built in 1998',
    '2 floors', 'about 40 minutes from the centre',
])
def test_a_number_that_is_not_money_is_left_alone(said):
    """Asked about the property, not the price."""
    history = chat(("How many bedrooms does it have?", said))
    assert app_module.budget_from_conversation(history) == '', \
        f"{said!r} was banked as a budget"


def test_a_bare_number_with_no_question_about_money_is_ignored():
    """Without the question there is no honest way to read it."""
    history = chat(("Whereabouts are you looking?", "2000"))
    assert app_module.budget_from_conversation(history) == ''


def test_a_small_number_is_never_a_price():
    history = chat((MONEY_Q, "2"))
    assert app_module.budget_from_conversation(history) == ''


def test_a_paragraph_that_happens_to_contain_a_digit_is_not_a_price():
    history = chat((MONEY_Q, "I am not really sure yet, it depends what my "
                             "wife thinks when she sees the 3 places we "
                             "shortlisted last weekend honestly"))
    assert app_module.budget_from_conversation(history) == ''


# ── ranges, and changing your mind ──

@pytest.mark.parametrize('said', [
    'between 1500 and 2000', '1500 to 2000', '1500-2000',
])
def test_a_range_is_read_as_its_ceiling(said):
    """Somebody who can go to 2000 should be shown things at 2000.
    Taking the floor hides half of what they asked for."""
    history = chat((MONEY_Q, said))
    assert '2,000' in app_module.budget_from_conversation(history)


def test_the_last_figure_wins():
    """A person who corrects themselves means the correction."""
    history = chat((MONEY_Q, "about 2000"),
                   ("Noted. Anything else?", "actually make that 3000"))
    assert '3,000' in app_module.budget_from_conversation(history)


# ── the whole point: a lead exists at the end of it ──

def test_a_tenant_stating_a_plain_monthly_budget_qualifies(client):
    history = chat(
        ("May I know who I'm speaking with?", "I'm Sam"),
        ("What brings you in today?", "looking to rent a flat"),
        ("Whereabouts?", "Chelsea"),
        ("What sort of place?", "two bed flat"),
        ("What's your monthly budget?", "2000"),
        ("How soon are you looking to move?", "next month"),
        ("Could you share your email?", "sam@example.test"),
        ("Best way to reach you?", "email"),
    )
    data = app_module.extract_lead_data(1, history)
    assert data['budget'], "a tenant's plain monthly budget was not read"
    assert app_module.is_lead_qualified(data, history), \
        "a tenant with a name, an email and a budget still does not qualify"


def test_the_old_patterns_are_gone():
    """They are the bug. If they come back, so does it."""
    import inspect
    source = inspect.getsource(app_module)
    assert 'budget_patterns = [' not in source
