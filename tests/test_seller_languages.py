"""The owner's side of the conversation, in every language the widget speaks.

Asked for on 16 November 2026: "Please implement multi-language support on
the seller side, just as it exists on the buyer side - this is essential."

The widget speaks nine languages (static/widget.js: en, fr, es, pt, pt-br,
de, it, nl, ar) and the assistant answers in the visitor's. But the code
that reads the conversation - is this an owner? what is the property? what
price did they give? how soon? how do they want to be reached? - knew a
handful of non-English words. An owner writing "Quiero vender mi casa" was
read as a buyer, a bare "600000" after "¿Qué precio pide?" was not a price,
and "lo antes posible" was no timeline at all.

Every sentence below is something an owner would actually write.
"""
import uuid

import pytest

import app as app_module

from tests.test_route_guards import make_agency


def talk(*texts):
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": t}
            for i, t in enumerate(texts)]


# ── an owner, in their own words ──────────────────────────────────────

@pytest.mark.parametrize("said, intent", [
    ("Quiero vender mi casa en Marbella", "sell"),
    ("Vendo mi piso", "sell"),
    ("Quiero alquilar mi apartamento", "rent_out"),
    ("Je veux vendre ma maison", "sell"),
    ("Je voudrais louer mon appartement", "rent_out"),
    ("Ich möchte meine Wohnung verkaufen", "sell"),
    ("Ich möchte mein Haus vermieten", "rent_out"),
    ("Vorrei vendere la mia casa", "sell"),
    ("Voglio affittare il mio appartamento", "rent_out"),
    ("Quero vender o meu apartamento", "sell"),
    ("Quero alugar a minha casa", "rent_out"),
    ("Ik wil mijn huis verkopen", "sell"),
    ("Ik wil mijn appartement verhuren", "rent_out"),
    ("أريد بيع شقتي في دبي", "sell"),
    ("أريد تأجير فيلتي", "rent_out"),
])
def test_an_owner_is_recognised(said, intent):
    assert app_module.owner_intent_in(said.lower()) == intent
    assert app_module.detect_chat_intent(talk(said)) == intent


@pytest.mark.parametrize("said, intent", [
    ("Quiero alquilar un apartamento en Madrid", "rent"),
    ("Quiero comprar una villa", "buy"),
    ("Je cherche un appartement à louer", "rent"),
    ("Ich möchte eine Wohnung mieten", "rent"),
    ("Vorrei affittare un appartamento a Roma", "rent"),
    ("Quero alugar um apartamento em Lisboa", "rent"),
    ("Ik wil een appartement huren", "rent"),
    ("أريد استئجار شقة في دبي", "rent"),
    ("أريد شراء فيلا", "buy"),
])
def test_somebody_looking_is_not_an_owner(said, intent):
    """The verb is shared - alquilar, louer, affittare, alugar mean "rent"
    and "rent out" - so "MY flat" is what makes an owner, and "A flat" is
    somebody looking."""
    assert app_module.owner_intent_in(said.lower()) is None
    assert app_module.seeker_statements_in(said.lower()) == [intent]


@pytest.mark.parametrize("said", [
    "Haben Sie Häuser zu verkaufen?",      # a for-sale sign, quoted by a buyer
    "Is de villa nog te koop?",
    "هل الفيلا للبيع؟",
])
def test_asking_what_is_for_sale_is_not_selling(said):
    assert app_module.owner_intent_in(said.lower()) is None


# ── the property ─────────────────────────────────────────────────────

@pytest.mark.parametrize("said, kind, beds, baths", [
    ("es un piso de 3 dormitorios y 2 baños", "apartment", 3, 2),
    ("une maison avec 4 chambres et 2 salles de bain", "house", 4, 2),
    ("ein Einfamilienhaus mit fünf Schlafzimmern und 3 Badezimmern", "house", 5, 3),
    ("un attico con tre camere da letto e due bagni", "penthouse", 3, 2),
    ("uma moradia com 4 quartos e 3 casas de banho", "house", 4, 3),
    ("een rijtjeshuis met 3 slaapkamers en 2 badkamers", "townhouse", 3, 2),
    ("شقتي فيها ٣ غرف نوم و٢ حمام", "apartment", 3, 2),
])
def test_the_property_in_their_words(said, kind, beds, baths):
    facts = app_module.property_facts_in(talk(said.lower()))
    assert facts == {"property_type": kind, "bedrooms": beds, "bathrooms": baths}


@pytest.mark.parametrize("question, answer, field, value", [
    ("How many bathrooms does it have?", "8", "bathrooms", 8),
    ("How many bedrooms does it have?", "5.", "bedrooms", 5),
    ("¿Cuántos dormitorios tiene?", "tres", "bedrooms", 3),
    ("Combien de salles de bain ?", "2", "bathrooms", 2),
    ("كم عدد غرف النوم؟", "٤", "bedrooms", 4),
])
def test_a_bare_count_answers_the_room_question(question, answer, field, value):
    """Fimi answered "How many bathrooms does it have?" with "8"."""
    assert app_module.property_facts_in(talk("x", question, answer))[field] == value


def test_a_count_answering_both_rooms_at_once_is_left_alone():
    facts = app_module.property_facts_in(talk("x", "How many bedrooms and bathrooms?", "3"))
    assert facts["bedrooms"] is None and facts["bathrooms"] is None


# ── the asking price ─────────────────────────────────────────────────

@pytest.mark.parametrize("question, answer, price", [
    ("¿Qué precio pide por la casa?", "1,5 millones de euros", "1.5 million EUR"),
    ("¿Qué precio pide por la casa?", "600.000 €", "600,000 EUR"),
    ("¿Qué precio pide por la casa?", "600 mil euros", "600,000 EUR"),
    ("¿Cuánto pide de alquiler al mes?", "2500", "2,500"),
    ("Quel prix souhaitez-vous ?", "850 000 €", "850,000 EUR"),
    ("Quel loyer demandez-vous par mois ?", "1800", "1,800"),
    ("Welchen Kaufpreis stellen Sie sich vor?", "2,5 Mio. Euro", "2.5 million EUR"),
    ("Welche Miete möchten Sie?", "1.200 €", "1,200 EUR"),
    ("Che prezzo chiede?", "450 mila euro", "450,000 EUR"),
    ("Qual é o preço que pretende?", "350 mil euros", "350,000 EUR"),
    ("Qual o valor do aluguel?", "3.500 reais", "3,500 BRL"),
    ("Wat is uw vraagprijs?", "675.000 euro", "675,000 EUR"),
    ("ما هو السعر الذي تطلبه؟", "٦ مليون درهم", "6 million AED"),
    ("كم الإيجار الشهري الذي تريده؟", "15000", "15,000"),
])
def test_the_price_they_give(question, answer, price):
    assert app_module.budget_from_conversation(talk("x", question, answer)[1:]) == price


@pytest.mark.parametrize("question, answer", [
    ("¿Cuántos dormitorios tiene?", "3"),
    ("Combien de chambres ?", "4"),
    ("Wie viele Schlafzimmer hat sie?", "5"),
])
def test_a_room_count_is_not_a_price(question, answer):
    assert app_module.budget_from_conversation(talk("x", question, answer)[1:]) == ""


# ── how soon ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("question, answer, bucket", [
    ("¿Para cuándo quiere vender la casa?", "Lo antes posible", "immediate"),
    ("¿En cuánto tiempo le gustaría alquilarlo?", "en 2 o 3 meses", "1_3_months"),
    ("Dans quels délais souhaitez-vous vendre ?", "Dès que possible", "immediate"),
    ("Quand souhaitez-vous louer votre appartement ?", "dans six mois", "3_6_months"),
    ("Wann möchten Sie verkaufen?", "So schnell wie möglich", "immediate"),
    ("Wann möchten Sie die Wohnung vermieten?", "keine Eile", "browsing"),
    ("Entro quando vorrebbe vendere?", "il prima possibile", "immediate"),
    ("Quando gostaria de vender?", "o mais rápido possível", "immediate"),
    ("Em quanto tempo pretende alugar?", "daqui a 4 meses", "3_6_months"),
    ("Wanneer wilt u verkopen?", "zo snel mogelijk", "immediate"),
    ("Wanneer wilt u verhuren?", "volgend jaar", "6_12_months"),
    ("متى تريد بيع العقار؟", "في أقرب وقت ممكن", "immediate"),
    ("متى تريد تأجير الشقة؟", "خلال شهرين", "1_3_months"),
])
def test_how_soon_they_want_to_sell_or_let(question, answer, bucket):
    assert app_module.is_timeline_question(question)
    assert app_module.extract_timeline(talk("x", question, answer)[1:])[0] == bucket


# ── how to reach them ────────────────────────────────────────────────

@pytest.mark.parametrize("question, answer, channel", [
    ("¿Cuál es la mejor manera de contactarte: WhatsApp, teléfono o correo?", "Por WhatsApp", "whatsapp"),
    ("Quelle est la meilleure façon de vous joindre : WhatsApp, téléphone ou e-mail ?", "par téléphone", "phone"),
    ("Wie erreichen wir Sie am besten - WhatsApp, Telefon oder E-Mail?", "E-Mail bitte", "email"),
    ("Qual a melhor forma de contacto: WhatsApp, telefone ou email?", "pode ligar", "phone"),
    ("Hoe kunnen we u het beste bereiken: WhatsApp, telefoon of e-mail?", "bellen graag", "phone"),
    ("ما هي أفضل طريقة للتواصل معك - واتساب، هاتف، أم بريد إلكتروني؟", "واتساب", "whatsapp"),
])
def test_how_they_want_to_be_reached(question, answer, channel):
    assert app_module.is_contact_question(question)
    assert app_module.channel_in(answer) == channel


@pytest.mark.parametrize("question", [
    "¿Cuál es tu número de teléfono?", "Quel est votre numéro WhatsApp ?",
    "ما هو رقم الواتساب الخاص بك؟", "Wat is uw telefoonnummer?",
])
def test_being_asked_for_a_number(question):
    assert app_module.is_number_question(question)


@pytest.mark.parametrize("answer", ["prefiero no", "nee", "لا", "liever niet", "لاحقاً"])
def test_declining_a_number(answer):
    assert app_module.declines_to_share(answer)


# ── a whole owner conversation, in Spanish ───────────────────────────

LUCIA = [
    ("Hola", "¡Hola! ¿Con quién tengo el gusto de hablar?"),
    ("Soy Lucía", "Encantada, Lucía. ¿Le interesa comprar, vender o alquilar?"),
    ("Quiero vender mi piso en Madrid", "¿En qué zona de Madrid está el piso?"),
    ("En Chamberí", "¿Cuántos dormitorios tiene?"),
    ("Tiene 3 dormitorios y 2 baños", "¿Algo que lo haga destacar?"),
    ("Terraza y garaje", "¿Qué precio pide por el piso?"),
    ("850.000 €", "¿Para cuándo quiere venderlo?"),
    ("Lo antes posible", "¿Cuál es la mejor manera de contactarle: WhatsApp, teléfono o correo?"),
    ("Por WhatsApp", "¿Cuál es su número de WhatsApp?"),
    ("+34 612 345 678", "¿Y su correo electrónico para la confirmación por escrito?"),
    ("lucia@example.test", "Gracias, Lucía. Los datos de su piso ya están con el equipo."),
]


def test_a_spanish_owner_becomes_a_seller_lead(client, monkeypatch):
    agency = make_agency()
    replies = [reply for _, reply in LUCIA]
    calls = []

    def model(**kwargs):
        calls.append(kwargs["messages"])
        message = type("Message", (), {"content": replies.pop(0)})()
        return type("Response", (), {"choices": [type("Choice", (), {"message": message})()]})()
    monkeypatch.setattr(app_module.client.chat.completions, "create", model)
    monkeypatch.setattr(app_module, "extract_seller_property", lambda history, intent: {
        "title": "Chamberí apartment", "location": "Chamberí, Madrid", "property_type": "apartment",
        "bedrooms": 3, "bathrooms": 2.0, "features": "terrace, garage", "price_raw": "850.000 €",
        "description": ""})
    monkeypatch.setattr(app_module, "generate_lead_summary", lambda history, name: "summary")
    monkeypatch.setattr(app_module, "send_email_brevo", lambda *a, **k: True)
    session_id = f"lucia-{uuid.uuid4().hex[:10]}"
    try:
        for said, _ in LUCIA:
            res = client.post("/chat", json={"agency_id": agency.id, "message": said,
                                             "session_id": session_id})
            assert res.status_code == 200, res.get_data(as_text=True)
        lead = app_module.Lead.query.filter_by(agency_id=agency.id).one()
        assert lead.lead_type == "seller"
        assert lead.budget == "850,000 EUR"
        assert lead.timeline == "immediate"
        assert lead.whatsapp_number == "+34 612 345 678"
        listing = app_module.Listing.query.filter_by(seller_lead_id=lead.id).one()
        assert listing.status == "pending" and listing.listing_purpose == "sale"
        # She was an owner from her third message on, never shown listings.
        assert "PROPERTY OWNER WHO WANTS TO SELL" in calls[2][0]["content"]
        assert "Property type: apartment (ALREADY ANSWERED" in calls[3][0]["content"]
        assert "Bedrooms: 3 (ALREADY ANSWERED)" in calls[5][0]["content"]
        assert "CONTACT DETAILS: complete" in calls[-1][0]["content"]
    finally:
        app_module.ConversationSession.query.filter(
            app_module.ConversationSession.session_key.like(f"%{session_id}")
        ).delete(synchronize_session=False)
        for model_ in (app_module.Listing, app_module.Lead):
            for row in model_.query.filter_by(agency_id=agency.id).all():
                app_module.db.session.delete(row)
        app_module.db.session.delete(agency)
        app_module.db.session.commit()
