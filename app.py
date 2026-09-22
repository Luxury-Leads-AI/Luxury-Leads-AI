from flask import Flask, request, jsonify, render_template, Response, redirect, session
from flask_sqlalchemy import SQLAlchemy
from openai import OpenAI
from dotenv import load_dotenv
from pathlib import Path
from flask_cors import CORS
from datetime import datetime, timedelta
from openpyxl import Workbook
from openpyxl.styles import Font
from io import BytesIO, StringIO
import os
import re
import json
import csv
from werkzeug.security import generate_password_hash, check_password_hash
import hashlib
import secrets
import pytz
from collections import defaultdict
import httpx  # used for Brevo email API + webhooks
from flask_limiter import Limiter

# -------------------------
# LOAD ENV VARIABLES
# -------------------------
BASE_DIR = Path(__file__).resolve().parent
env_path = BASE_DIR / ".env"
load_dotenv(dotenv_path=env_path)

# -------------------------
# APP SETUP
# -------------------------
app = Flask(__name__, static_folder="static", template_folder="templates")
CORS(app, resources={r"/*": {"origins": "*"}})

# -------------------------
# PUBLIC ADDRESS
# -------------------------
# Every link the app writes into an email, and the embed code it shows an
# agency, starts with this one value. Today that is the Render address.
# When the custom domain is live, set PUBLIC_BASE_URL on Render
# (for example https://app.yourdomain.com) and every link moves with it -
# no code change. The old onrender.com address keeps working either way.
DEFAULT_PUBLIC_BASE_URL = 'https://luxury-leads-ai.onrender.com'

def normalize_base_url(value):
    """'app.example.com/' -> 'https://app.example.com'. Blank -> the default."""
    value = (value or '').strip().rstrip('/')
    if not value:
        return DEFAULT_PUBLIC_BASE_URL
    if not value.startswith(('http://', 'https://')):
        value = 'https://' + value
    return value

PUBLIC_BASE_URL = normalize_base_url(os.getenv('PUBLIC_BASE_URL'))
app.jinja_env.globals['public_base_url'] = PUBLIC_BASE_URL

import re as _re
app.jinja_env.filters['regex_replace'] = lambda s, find, replace: _re.sub(find, replace, s)

# -------------------------
# DATABASE URL FIX
# -------------------------
database_url = os.getenv('DATABASE_URL', 'sqlite:///luxury_leads.db')
if database_url.startswith('postgresql://'):
    database_url = database_url.replace('postgresql://', 'postgresql+psycopg://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'change-this-in-production')

# Every login session is signed with SECRET_KEY. The fallback above is
# written in this file, so anyone who reads the code could use it to forge
# a super admin session. It is fine on your own computer; on Render the app
# refuses to start with it. Render sets RENDER=true on every service.
_INSECURE_SECRET_KEYS = {'', 'change-this-in-production'}

def secret_key_problem(secret_key, on_render):
    """Why this key must not be used in production, or None if it's fine."""
    if not on_render:
        return None
    if (secret_key or '').strip() in _INSECURE_SECRET_KEYS:
        return ("SECRET_KEY is not set on Render, so sessions would be signed "
                "with the public fallback value. Add SECRET_KEY in Render -> "
                "Environment (a long random value), then redeploy.")
    return None

_ON_RENDER = os.getenv('RENDER', '').strip().lower() == 'true'
_secret_problem = secret_key_problem(os.getenv('SECRET_KEY'), _ON_RENDER)
if _secret_problem:
    raise RuntimeError(_secret_problem)
if _ON_RENDER and len(os.getenv('SECRET_KEY', '')) < 32:
    print("⚠️ SECRET_KEY is shorter than 32 characters - consider a longer one.")

# Super Admin panel password (protects /owner, /agencies, /delete-agency/<id>).
# Must be set in the environment on Render - if it's blank, the login route
# refuses every attempt rather than silently allowing access.
SUPER_ADMIN_PASSWORD = os.getenv('SUPER_ADMIN_PASSWORD', '')

# Feature flag: whether the Corporation tier is shown on the signup page and
# pricing page. Nothing about the tier's backend logic (TIER_LIMITS, the
# agency.tier in ['agency', 'corporation'] checks elsewhere) is removed -
# this only controls whether a new visitor can see/pick it. Flip to "true"
# on Render (no code change needed) to bring it back when it's ready.
SHOW_TIER_3 = os.getenv('SHOW_TIER_3', 'false').strip().lower() == 'true'

db = SQLAlchemy(app)

# -------------------------
# RATE LIMITS
# -------------------------
# Public endpoints cost money (every /chat message is an OpenAI call) or
# guard a login, so each visitor gets a sensible ceiling. Counters live in
# this process's memory: right for one Render instance with one worker.
# More than one instance would need a Redis-style store (Flask-Limiter
# cannot keep its counters in Postgres).
def client_ip():
    """The visitor's own address. On Render every request arrives through
    Render's proxy, so request.remote_addr is the proxy; Render puts the
    real client address first in X-Forwarded-For."""
    forwarded = request.headers.get('X-Forwarded-For', '')
    first = forwarded.split(',')[0].strip()
    return first or request.remote_addr or 'unknown'

limiter = Limiter(
    key_func=client_ip,
    app=app,
    storage_uri="memory://",
    default_limits=[],
    swallow_errors=True,   # a limiter fault must never take /chat down
)

CHAT_LIMIT = "20 per minute;200 per hour"
SIGNUP_LIMIT = "5 per hour;20 per day"
LOGIN_LIMIT = "10 per 15 minutes"
SUPER_ADMIN_LOGIN_LIMIT = "5 per 15 minutes"
PASSWORD_RESET_LIMIT = "5 per hour"

_LOGIN_PAGES = {'/owner-login', '/agent-login', '/super-admin-login'}

@app.errorhandler(429)
def too_many_requests(e):
    """Answer a blocked request in the shape its caller already understands,
    so the widget and the forms show a readable message, not a blank error."""
    path = request.path
    if path == '/chat':
        return jsonify({
            "reply": "You're sending messages a little too fast. "
                     "Please wait a minute and try again.",
            "error": "rate_limited",
        }), 429
    if path == '/create-agency':
        return jsonify({"error": "Too many sign-up attempts from your network. "
                                 "Please try again in an hour."}), 429
    if path in _LOGIN_PAGES:
        return redirect(f"{path}?error=Too+many+attempts.+Please+wait+15+minutes+and+try+again.")
    if path == '/forgot-password':
        return render_template(
            "forgot_password.html",
            message="Too many reset requests from your network. "
                    "Please wait an hour and try again."), 429
    return jsonify({"error": "Too many requests. Please slow down."}), 429

# -------------------------
# OPENAI CLIENT
# -------------------------
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise ValueError("OPENAI_API_KEY is not set.")

client = OpenAI(api_key=api_key)

# -------------------------
# EMAIL CONFIG
# -------------------------
SMTP_EMAIL = os.getenv("SMTP_EMAIL")

# -------------------------
# APPOINTMENT CONFIG
# -------------------------
TIME_SLOTS = ['10:00 AM', '12:00 PM', '2:00 PM', '4:00 PM', '6:00 PM']
PK_TZ = pytz.timezone('Asia/Karachi')

# -------------------------
# TIER CONFIGURATION (Paddle-ready)
# -------------------------
TIER_LIMITS = {
    'solo':        {'agents': 1,   'branches': 0,   'label': 'Solo Agent',  'price': 197, 'paddle_price_id': os.getenv('PADDLE_PRICE_SOLO', '')},
    'agency':      {'agents': 10,  'branches': 0,   'label': 'Agency',      'price': 497, 'paddle_price_id': os.getenv('PADDLE_PRICE_AGENCY', '')},
    'corporation': {'agents': 999, 'branches': 999, 'label': 'Corporation', 'price': 997, 'paddle_price_id': os.getenv('PADDLE_PRICE_CORP', '')},
}

def get_tier_limits(agency):
    """Single source of truth for what an agency can do."""
    return TIER_LIMITS.get((agency.tier or 'solo'), TIER_LIMITS['solo'])

def has_dashboard_access(agency):
    """Billing-state gate. Pre-Paddle: everyone passes."""
    return (agency.subscription_status or 'active') in ('active', 'trialing', 'past_due')

def agent_covers_location(agent, target_location):
    """True if this agent's stated coverage area overlaps the property's
    location. Both sides are free text ("Miami, Orlando" vs "Miami, FL"),
    so each is split into parts and compared on whole words - "Miami"
    matches "Miami Beach, FL", "Orlando" does not match "Miami, FL".
    An agent with no location set covers nothing in particular, so they
    simply never win the location round (they stay eligible for the
    round-robin fallback)."""
    if not agent or not agent.location or not target_location:
        return False
    target = target_location.lower()
    for part in re.split(r'[,/;|]', agent.location.lower()):
        part = part.strip()
        if len(part) >= 3 and re.search(r'\b' + re.escape(part) + r'\b', target):
            return True
    return False


def assign_next_agent(agency, target_location=None):
    """Pick the agent for a new lead. Agents covering the property's area
    come first (least-busy among them); everyone else is the fallback, so
    a lead is never dropped just because nobody covers that city.
    Returns None for solo tier or when no active agents exist."""
    if (agency.tier or 'solo') == 'solo':
        return None
    agents = Agent.query.filter_by(agency_id=agency.id, status='active').all()
    if not agents:
        return None
    counts = {a.id: Lead.query.filter_by(agency_id=agency.id, agent_id=a.id).count()
              for a in agents}
    local = [a for a in agents if agent_covers_location(a, target_location)]
    pool = local or agents
    best = min(pool, key=lambda a: (counts[a.id], a.id))
    why = f"covers '{target_location}'" if local else "round-robin"
    print(f"👥 Lead → agent {best.name} (ID {best.id}, {counts[best.id]} leads, {why})")
    return best

# ─────────────────────────────────────────────────────
# MULTILINGUAL DICTIONARIES
# Supported: EN, ES, DE, FR, IT, PT, PL, NL, TR (+ Roman Urdu/Hindi affirmatives)
# ─────────────────────────────────────────────────────

MONTH_NAMES = {
    'january': 1, 'jan': 1, 'januar': 1, 'janvier': 1, 'enero': 1, 'gennaio': 1, 'janeiro': 1, 'stycznia': 1, 'styczeń': 1, 'januari': 1, 'ocak': 1,
    'february': 2, 'feb': 2, 'februar': 2, 'février': 2, 'febrero': 2, 'febbraio': 2, 'fevereiro': 2, 'lutego': 2, 'luty': 2, 'februari': 2, 'şubat': 2, 'subat': 2,
    'march': 3, 'mar': 3, 'märz': 3, 'mars': 3, 'marzo': 3, 'março': 3, 'marco': 3, 'marca': 3, 'marzec': 3, 'maart': 3, 'mart': 3,
    'april': 4, 'apr': 4, 'avril': 4, 'abril': 4, 'aprile': 4, 'kwietnia': 4, 'kwiecień': 4, 'nisan': 4,
    'may': 5, 'mai': 5, 'mayo': 5, 'maggio': 5, 'maio': 5, 'maja': 5, 'maj': 5, 'mei': 5, 'mayıs': 5, 'mayis': 5,
    'june': 6, 'jun': 6, 'juni': 6, 'juin': 6, 'junio': 6, 'giugno': 6, 'junho': 6, 'czerwca': 6, 'czerwiec': 6, 'haziran': 6,
    'july': 7, 'jul': 7, 'juli': 7, 'juillet': 7, 'julio': 7, 'luglio': 7, 'julho': 7, 'lipca': 7, 'lipiec': 7, 'temmuz': 7,
    'august': 8, 'aug': 8, 'août': 8, 'aout': 8, 'agosto': 8, 'sierpnia': 8, 'sierpień': 8, 'augustus': 8, 'ağustos': 8, 'agustos': 8,
    'september': 9, 'sep': 9, 'sept': 9, 'septembre': 9, 'septiembre': 9, 'settembre': 9, 'setembro': 9, 'września': 9, 'wrzesień': 9, 'eylül': 9, 'eylul': 9,
    'october': 10, 'oct': 10, 'oktober': 10, 'octobre': 10, 'octubre': 10, 'ottobre': 10, 'outubro': 10, 'października': 10, 'październik': 10, 'ekim': 10,
    'november': 11, 'nov': 11, 'novembre': 11, 'noviembre': 11, 'novembro': 11, 'listopada': 11, 'listopad': 11, 'kasım': 11, 'kasim': 11,
    'december': 12, 'dec': 12, 'dezember': 12, 'décembre': 12, 'decembre': 12, 'diciembre': 12, 'dicembre': 12, 'dezembro': 12, 'grudnia': 12, 'grudzień': 12, 'aralık': 12, 'aralik': 12,
}

WEEKDAY_WORDS = {
    'monday': 'monday', 'montag': 'monday', 'lundi': 'monday', 'lunes': 'monday', 'lunedì': 'monday', 'lunedi': 'monday', 'segunda-feira': 'monday', 'segunda': 'monday', 'poniedziałek': 'monday', 'poniedzialek': 'monday', 'maandag': 'monday', 'pazartesi': 'monday',
    'tuesday': 'tuesday', 'dienstag': 'tuesday', 'mardi': 'tuesday', 'martes': 'tuesday', 'martedì': 'tuesday', 'martedi': 'tuesday', 'terça-feira': 'tuesday', 'terça': 'tuesday', 'terca': 'tuesday', 'wtorek': 'tuesday', 'dinsdag': 'tuesday', 'salı': 'tuesday', 'sali': 'tuesday',
    'wednesday': 'wednesday', 'mittwoch': 'wednesday', 'mercredi': 'wednesday', 'miércoles': 'wednesday', 'miercoles': 'wednesday', 'mercoledì': 'wednesday', 'mercoledi': 'wednesday', 'quarta-feira': 'wednesday', 'quarta': 'wednesday', 'środa': 'wednesday', 'sroda': 'wednesday', 'woensdag': 'wednesday', 'çarşamba': 'wednesday', 'carsamba': 'wednesday',
    'thursday': 'thursday', 'donnerstag': 'thursday', 'jeudi': 'thursday', 'jueves': 'thursday', 'giovedì': 'thursday', 'giovedi': 'thursday', 'quinta-feira': 'thursday', 'quinta': 'thursday', 'czwartek': 'thursday', 'donderdag': 'thursday', 'perşembe': 'thursday', 'persembe': 'thursday',
    'friday': 'friday', 'freitag': 'friday', 'vendredi': 'friday', 'viernes': 'friday', 'venerdì': 'friday', 'venerdi': 'friday', 'sexta-feira': 'friday', 'sexta': 'friday', 'piątek': 'friday', 'piatek': 'friday', 'vrijdag': 'friday', 'cuma': 'friday',
    'saturday': 'saturday', 'samstag': 'saturday', 'samedi': 'saturday', 'sábado': 'saturday', 'sabado': 'saturday', 'sabato': 'saturday', 'sobota': 'saturday', 'zaterdag': 'saturday', 'cumartesi': 'saturday',
    'tomorrow': 'tomorrow', 'morgen': 'tomorrow', 'demain': 'tomorrow', 'mañana': 'tomorrow', 'manana': 'tomorrow', 'domani': 'tomorrow', 'amanhã': 'tomorrow', 'amanha': 'tomorrow', 'jutro': 'tomorrow', 'yarın': 'tomorrow', 'yarin': 'tomorrow', 'kal': 'tomorrow',
    'today': 'today', 'heute': 'today', "aujourd'hui": 'today', 'hoy': 'today', 'oggi': 'today', 'hoje': 'today', 'dzisiaj': 'today', 'dziś': 'today', 'vandaag': 'today', 'bugün': 'today', 'bugun': 'today', 'aaj': 'today',
}

AFFIRMATIVE_WORDS = [
    'yes', 'sure', 'sounds good', 'definitely', 'absolutely', "let's", 'ok', 'okay', 'yeah', 'yep', 'of course',
    'si', 'sí', 'claro', 'por supuesto', 'vale', 'perfecto', 'genial',
    'ja', 'sicher', 'klar', 'natürlich', 'natuerlich', 'na klar', 'gerne', 'gut', 'passt',
    'oui', 'bien sûr', 'bien sur', "d'accord", 'daccord', 'volontiers', 'parfait',
    'sì', 'certo', 'va bene', 'certamente', 'volentieri',
    'sim', 'com certeza', 'certamente', 'pode ser',
    'tak', 'oczywiście', 'oczywiscie', 'jasne', 'pewnie', 'chętnie', 'chetnie',
    'zeker', 'graag', 'prima', 'natuurlijk',
    'evet', 'tabii', 'tabi', 'elbette', 'olur',
    'haan', 'han', 'jee', 'ji', 'bilkul', 'zaroor',
]

# Spelled-out numbers 1-10 across all supported languages, since customers
# very commonly say "five bedrooms" / "fünf Schlafzimmer" rather than a
# digit, and bedroom/bathroom counts in real estate are almost always
# small numbers in this range.
NUMBER_WORDS = {
    # English
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10,
    # Spanish
    'uno': 1, 'una': 1, 'dos': 2, 'tres': 3, 'cuatro': 4, 'cinco': 5, 'seis': 6, 'siete': 7, 'ocho': 8, 'nueve': 9, 'diez': 10,
    # German
    'ein': 1, 'eine': 1, 'eins': 1, 'zwei': 2, 'drei': 3, 'vier': 4, 'fünf': 5, 'funf': 5, 'sechs': 6, 'sieben': 7, 'acht': 8, 'neun': 9, 'zehn': 10,
    # French
    'un': 1, 'une': 1, 'deux': 2, 'trois': 3, 'quatre': 4, 'cinq': 5, 'sept': 7, 'huit': 8, 'neuf': 9, 'dix': 10,
    # Italian
    'due': 2, 'tre': 3, 'quattro': 4, 'cinque': 5, 'sei': 6, 'sette': 7, 'otto': 8, 'nove': 9, 'dieci': 10,
    # Portuguese
    'um': 1, 'uma': 1, 'dois': 2, 'duas': 2, 'três': 3, 'quatro': 4, 'sete': 7, 'oito': 8, 'dez': 10,
    # Polish
    'jeden': 1, 'jedna': 1, 'dwa': 2, 'trzy': 3, 'cztery': 4, 'pięć': 5, 'piec': 5, 'sześć': 6, 'szesc': 6, 'siedem': 7, 'osiem': 8, 'dziewięć': 9, 'dziewiec': 9, 'dziesięć': 10, 'dziesiec': 10,
    # Dutch
    'een': 1, 'twee': 2, 'drie': 3, 'vijf': 5, 'zes': 6, 'zeven': 7, 'negen': 9, 'tien': 10,
    # Turkish
    'bir': 1, 'iki': 2, 'üç': 3, 'uc': 3, 'dört': 4, 'dort': 4, 'beş': 5, 'bes': 5, 'altı': 6, 'alti': 6, 'yedi': 7, 'sekiz': 8, 'dokuz': 9,
}

BEDROOM_WORDS = [
    'bed', 'beds', 'bedroom', 'bedrooms',
    'habitacion', 'habitación', 'habitaciones', 'dormitorio', 'dormitorios', 'recamara', 'recamaras',
    'schlafzimmer',
    'chambre', 'chambres',
    'camera da letto', 'camere da letto',
    'quarto', 'quartos',
    'sypialnia', 'sypialnie', 'sypialni',
    'slaapkamer', 'slaapkamers',
    'yatak odası', 'yatak odasi', 'yatak odaları', 'yatak odalari',
]

BATHROOM_WORDS = [
    'bath', 'baths', 'bathroom', 'bathrooms',
    'baño', 'baños', 'bano', 'banos', 'aseo', 'aseos',
    'badezimmer', 'bad',
    'salle de bain', 'salles de bain',
    'bagno', 'bagni',
    'banheiro', 'banheiros', 'casa de banho',
    'łazienka', 'łazienki', 'lazienka', 'lazienki',
    'badkamer', 'badkamers',
    'banyo', 'banyolar',
]

NAME_INTRO_PREFIXES = [
    # English
    r"^i\s*am\b", r"^i'?m\b", r"^my\s+name\s+is\b", r"^this\s+is\b", r"^call\s+me\b", r"^it'?s\b", r"^name'?s\b",
    # Spanish
    r"^soy\b", r"^me\s+llamo\b", r"^mi\s+nombre\s+es\b",
    # German
    r"^ich\s+bin\b", r"^ich\s+hei(?:sse|\u00dfe)\b", r"^mein\s+name\s+ist\b",
    # French
    r"^je\s+suis\b", r"^je\s+m'?appelle\b", r"^mon\s+nom\s+est\b",
    # Italian
    r"^sono\b", r"^mi\s+chiamo\b", r"^il\s+mio\s+nome\s+\u00e8\b",
    # Portuguese
    r"^eu\s+sou\b", r"^meu\s+nome\s+\u00e9\b", r"^me\s+chamo\b",
    # Polish
    r"^jestem\b", r"^nazywam\s+si\u0119\b", r"^mam\s+na\s+imi\u0119\b",
    # Dutch
    r"^ik\s+ben\b", r"^mijn\s+naam\s+is\b",
    # Turkish
    r"^ben(?:im)?\s+ad[\u0131i]m\b", r"^ismim\b", r"^benim\b",
    # Roman Urdu/Hindi
    r"^mera\s+naam\b", r"^main\s+hoon\b",
]
NAME_GREETING_PREFIXES = [
    'hello', 'hi', 'hey', 'hola', 'ciao', 'hallo', 'bonjour', 'salut',
    'witam', 'cze\u015b\u0107', 'czesc', 'merhaba', 'ol\u00e1', 'ola',
]

VIEWING_OFFER_PHRASES = [
    'see it in person', 'seeing it in person', 'view it in person', 'would you like to see', 'would you like to view',
    'in person', 'interested in seeing', 'interested in viewing', 'want to view', 'schedule a viewing',
    'arrange a viewing', 'book a viewing', 'see the property', 'visit the property', 'like to view', 'like to see',
    'en persona', 'una visita', 'ver la propiedad', 'ver alguna', 'visitar la propiedad', 'agendar una visita',
    'besichtigung', 'besichtigen', 'vor ort', 'anschauen', 'ansehen',
    'en personne', 'une visite', 'visiter', 'voir le bien', 'voir la propriété',
    'di persona', 'una visita', 'visitare', 'vedere la proprietà', 'vedere la proprieta',
    'pessoalmente', 'uma visita', 'visitar', 'ver o imóvel', 'ver o imovel',
    'osobiście', 'osobiscie', 'obejrzeć', 'obejrzec', 'zobaczyć', 'zobaczyc', 'umówić', 'umowic',
    'in persoon', 'bezichtiging', 'bezichtigen', 'bekijken',
    'yerinde görmek', 'gormek ister', 'görmek ister', 'ziyaret',
]

BOOKING_KEYWORDS = [
    'schedule', 'appointment', 'viewing', 'visit', 'see the property', 'book a visit', 'arrange a viewing',
    'show me', 'can i see', 'i would like to see', 'i want to see', 'visit the property', 'see it',
    'quiero ver', 'ver a', 'ver la', 'ver los', 'ver las', 'visita', 'visitar', 'cita',
    'besichtigung', 'besichtigen', 'sehen', 'termin',
    'visite', 'visiter', 'voir', 'rendez-vous',
    'visita', 'visitare', 'vedere', 'appuntamento',
    'visita', 'visitar', 'ver', 'agendar',
    'zobaczyć', 'zobaczyc', 'obejrzeć', 'obejrzec', 'wizyta', 'spotkanie',
    'bezichtiging', 'bezichtigen', 'zien', 'afspraak',
    'görmek', 'gormek', 'ziyaret', 'randevu',
    'dekhna', 'dekh', 'mulaqat',
]


def clean_whatsapp_number(number):
    if not number:
        return None
    cleaned = re.sub(r'\D', '', number)
    cleaned = cleaned.lstrip('0')
    return cleaned if len(cleaned) >= 9 else None


def send_email_brevo(to_email, subject, body):
    """Central email sender via Brevo API. Returns True/False."""
    BREVO_API_KEY = os.getenv("BREVO_API_KEY")
    if not BREVO_API_KEY:
        print("⚠️ BREVO_API_KEY not set - email not sent")
        return False
    if not to_email:
        return False
    try:
        response = httpx.post(
            "https://api.brevo.com/v3/smtp/email",
            headers={
                "api-key": BREVO_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "sender": {"name": "Luxury Leads AI", "email": SMTP_EMAIL},
                "to": [{"email": to_email}],
                "subject": subject,
                "textContent": body
            },
            timeout=10
        )
        if response.status_code in (200, 201):
            print(f"✅ EMAIL SENT via Brevo to: {to_email}")
            return True
        else:
            print(f"⚠️ Brevo failed ({response.status_code}): {response.text[:200]}")
            return False
    except Exception as e:
        print(f"⚠️ Brevo error: {e}")
        return False


# ─────────────────────────────────────────────────────
# LANGUAGE-AGNOSTIC QUESTION DETECTION
# ─────────────────────────────────────────────────────

def is_contact_question(ai_text):
    """True if the AI message is asking for contact preference - any language.
    Universal signal: 'whatsapp' appearing alongside an email/phone word."""
    ai_lower = ai_text.lower()
    english_patterns = [
        "best way to reach you", "how can i reach you", "reach you",
        "contact you", "whatsapp, phone, or email", "phone, or email"
    ]
    if any(p in ai_lower for p in english_patterns):
        return True
    if 'whatsapp' in ai_lower:
        contact_words = ['email', 'e-mail', 'mail', 'correo', 'phone', 'telefon',
                          'teléfono', 'telefono', 'téléphone', 'telephone', 'telefone',
                          'telefoon', 'numer', 'número', 'numero']
        if any(w in ai_lower for w in contact_words):
            return True
    return False


def is_number_question(ai_text):
    """True if the AI message is asking for a phone/WhatsApp number - any language."""
    ai_lower = ai_text.lower()
    english_patterns = ["whatsapp number", "phone number", "your number",
                         "share your number", "what's your"]
    if any(p in ai_lower for p in english_patterns):
        return True
    number_words = ['nummer', 'número', 'numero', 'numéro', 'numer', 'numara']
    if any(w in ai_lower for w in number_words) and ('whatsapp' in ai_lower or 'telefon' in ai_lower or 'phone' in ai_lower):
        return True
    return False


def is_timeline_question(ai_text):
    """True if the AI message is asking WHEN the customer wants to move,
    buy, or sell. Timeline is the single strongest predictor of whether a
    lead is worth an agent's afternoon, and until now nobody was asking."""
    ai_lower = (ai_text or '').lower()
    english_patterns = [
        "how soon", "what's your timeline", "what is your timeline",
        "your timeline", "time frame", "timeframe", "how quickly",
        "when are you hoping", "when are you looking", "when would you like to move",
        "when do you want to move", "when do you need", "when are you planning",
        "looking to move", "hoping to move", "hoping to sell", "looking to sell by",
        "planning to sell", "when would you want to", "how urgent",
    ]
    if any(pat in ai_lower for pat in english_patterns):
        return True
    # Other languages: a "when" word next to a move/buy/sell verb.
    when_words = ['cuándo', 'cuando', 'quand', 'wann', 'quando', 'wanneer',
                  'ne zaman', 'kiedy', 'kogda']
    move_words = ['mudar', 'mudanza', 'umziehen', 'déménager', 'verhuizen',
                  'comprar', 'kaufen', 'acheter', 'vender', 'verkaufen', 'vendre',
                  'taşın', 'przeprowad', 'kupi']
    if any(w in ai_lower for w in when_words) and any(w in ai_lower for w in move_words):
        return True
    return False


# ─────────────────────────────────────────────────────
# APPOINTMENT DATE + CAPACITY HELPERS
# ─────────────────────────────────────────────────────

def resolve_next_date(day_word):
    """
    Convert 'monday', 'tomorrow', 'today' → real date info.
    Returns {'date': date_obj, 'display': 'Monday, July 13, 2026', 'iso': '2026-07-13'} or None.
    Sundays return None (closed).
    """
    if not day_word:
        return None
    day_word = day_word.lower().strip()
    today = datetime.now(PK_TZ).date()
    weekdays = {'monday': 0, 'tuesday': 1, 'wednesday': 2,
                'thursday': 3, 'friday': 4, 'saturday': 5}

    if day_word == 'today':
        target = today
    elif day_word == 'tomorrow':
        target = today + timedelta(days=1)
    elif day_word in weekdays:
        days_ahead = (weekdays[day_word] - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        target = today + timedelta(days=days_ahead)
    else:
        return None

    if target.weekday() == 6:
        return None

    return {
        'date': target,
        'display': target.strftime('%A, %B %d, %Y'),
        'iso': target.strftime('%Y-%m-%d')
    }


def find_all_dates_in_text(text):
    """Language-agnostic: finds ALL valid calendar dates mentioned in a
    single piece of text, supporting both 'DD Month' (17 August) and
    'Month DD' (August 17) orderings - our own AI always writes dates in
    the US 'Month DD' order, so both must be supported. Returns a list of
    {'date','display','iso','pos'} dicts in the order they appear, so a
    caller can pair multiple dates against multiple times mentioned
    together in the same message."""
    today = datetime.now(PK_TZ).date()
    found = []
    seen_iso = set()

    def try_add(day_num, month_num, pos):
        if not month_num or day_num < 1 or day_num > 31:
            return
        for year in (today.year, today.year + 1):
            try:
                candidate = datetime(year, month_num, day_num).date()
            except ValueError:
                continue
            if candidate >= today and candidate.weekday() != 6:
                iso = candidate.strftime('%Y-%m-%d')
                if iso not in seen_iso:
                    seen_iso.add(iso)
                    found.append({
                        'date': candidate,
                        'display': candidate.strftime('%A, %B %d, %Y'),
                        'iso': iso,
                        'pos': pos
                    })
                break

    # Pattern A: "DD Month" (e.g. "17 August", "20 de julio")
    pattern_dm = r'\b(\d{1,2})\s*(?:st|nd|rd|th)?[.,]?\s*(?:de\s+|den\s+|of\s+|di\s+|van\s+)?([A-Za-zÀ-ÖØ-öø-ÿążćęłńóśźŻĄĆĘŁŃÓŚŹ]+)'
    for match in re.finditer(pattern_dm, text, re.IGNORECASE):
        month_num = MONTH_NAMES.get(match.group(2).lower())
        try_add(int(match.group(1)), month_num, match.start())

    # Pattern B: "Month DD" (e.g. "August 17") - the format our own AI uses
    pattern_md = r'\b([A-Za-zÀ-ÖØ-öø-ÿążćęłńóśźŻĄĆĘŁŃÓŚŹ]+)\s+(\d{1,2})(?:st|nd|rd|th)?\b'
    for match in re.finditer(pattern_md, text, re.IGNORECASE):
        month_num = MONTH_NAMES.get(match.group(1).lower())
        try_add(int(match.group(2)), month_num, match.start())

    found.sort(key=lambda d: d['pos'])
    return found


def resolve_date_from_daymonth(user_text):
    """Convenience wrapper: returns only the LAST-mentioned valid date in
    the text (used where a single date is expected)."""
    all_dates = find_all_dates_in_text(user_text)
    return all_dates[-1] if all_dates else None


def is_slot_within_booking_window(slot_date, today, max_days_ahead=30):
    """True if slot_date is not in the past and not more than max_days_ahead
    days from today. Only blocks genuinely nonsensical dates - NOT a tight
    ceiling. The availability list shown to the customer is recalculated
    fresh from "now" on every message, so a date that was validly offered
    can legitimately fall outside a narrow window by the time they confirm
    it later in the same conversation (customers often take minutes or
    hours between replies). Rejecting a date the AI already confirmed to
    the customer, silently, is worse than allowing a generous buffer here -
    the slot-capacity check remains the real business constraint."""
    return today <= slot_date <= today + timedelta(days=max_days_ahead)


def slot_booked_count(agency_id, date_iso, time_label):
    """Count non-cancelled appointments for an exact date+time slot."""
    return Appointment.query.filter(
        Appointment.agency_id == agency_id,
        Appointment.appointment_date_iso == date_iso,
        Appointment.appointment_time == time_label,
        Appointment.status != 'cancelled'
    ).count()


def get_slot_capacity(agency):
    """Effective bookings allowed per time slot.
    Solo: agency's max_viewings_per_slot (unchanged behavior).
    Tier 2/3: number of ACTIVE agents (each agent can host 1 viewing per slot);
    falls back to max_viewings_per_slot if no agents exist yet."""
    if (agency.tier or 'solo') == 'solo':
        return agency.max_viewings_per_slot or 2
    active = Agent.query.filter_by(agency_id=agency.id, status='active').count()
    return active if active > 0 else (agency.max_viewings_per_slot or 2)


def agent_busy_at(agent_id, date_iso, time_label):
    """True if this agent already has a non-cancelled booking at that exact slot."""
    return Appointment.query.filter(
        Appointment.agent_id == agent_id,
        Appointment.appointment_date_iso == date_iso,
        Appointment.appointment_time == time_label,
        Appointment.status != 'cancelled'
    ).count() > 0


def pick_agent_for_slot(agency, date_iso, time_label, preferred_agent_id=None,
                         target_location=None):
    """Choose the agent for a new booking:
    1. The customer's own agent (preferred) if free at that slot
    2. The least-busy free agent who covers the property's area
    3. Otherwise the least-busy free agent, so a viewing is never lost
       merely because no one is based in that city
    Returns None for solo tier or when nobody is free."""
    if (agency.tier or 'solo') == 'solo':
        return None
    agents = Agent.query.filter_by(agency_id=agency.id, status='active').all()
    if not agents:
        return None
    if preferred_agent_id:
        pref = next((a for a in agents if a.id == preferred_agent_id), None)
        if pref and not agent_busy_at(pref.id, date_iso, time_label):
            return pref
    free = [a for a in agents if not agent_busy_at(a.id, date_iso, time_label)]
    if not free:
        return None
    counts = {a.id: Appointment.query.filter(
        Appointment.agent_id == a.id,
        Appointment.status != 'cancelled').count() for a in free}
    local = [a for a in free if agent_covers_location(a, target_location)]
    pool = local or free
    chosen = min(pool, key=lambda a: (counts[a.id], a.id))
    if local:
        print(f"📍 Viewing → agent {chosen.name} (covers '{target_location}')")
    return chosen

def get_availability_context(agency_id, max_per_slot, booked_slots=None):
    """
    Build next-7-days real availability for the AI.
    Excludes Sundays and slots that are already full.

    `booked_slots` is what THIS customer has already booked in THIS
    conversation ("YYYY-MM-DD|10:00 AM"). Those exact times are removed from
    the offer, but the rest of that day stays open - a customer viewing one
    property at 6 PM can absolutely view another at 10 AM the same day, and
    the AI used to refuse that on its own.
    """
    try:
        taken = set(booked_slots or [])
        today = datetime.now(PK_TZ).date()
        lines = ["\nVIEWING AVAILABILITY - ONLY offer these exact dates and open time slots:"]
        any_open = False
        mine_by_day = {}
        for i in range(1, 8):
            d = today + timedelta(days=i)
            if d.weekday() == 6:
                continue
            iso = d.strftime('%Y-%m-%d')
            open_slots = []
            for s in TIME_SLOTS:
                if f"{iso}|{s}" in taken:
                    mine_by_day.setdefault(d.strftime('%A, %B %d'), []).append(s)
                    continue
                if slot_booked_count(agency_id, iso, s) < max_per_slot:
                    open_slots.append(s)
            if open_slots:
                any_open = True
                lines.append(f"- {d.strftime('%A, %B %d')}: {', '.join(open_slots)}")
        if not any_open:
            return ("\nVIEWING AVAILABILITY: All slots are fully booked for the next 7 days. "
                    "Apologize and tell the customer the agency will contact them directly to arrange a viewing time.")
        lines.append("When the customer wants a viewing, offer ALL of the above days (each with its date), not just one or two.")
        lines.append("Always say the full date when offering or confirming, e.g. 'Monday, July 13' - never just 'Monday'.")
        lines.append("If a customer asks for a day or time NOT listed above, say that slot is unavailable and offer the open options.")
        if mine_by_day:
            already = "; ".join(f"{day} at {', '.join(times)}" for day, times in mine_by_day.items())
            lines.append(
                f"This customer has ALREADY booked: {already}. Those exact times are taken, but the SAME DAY is "
                "still available at its other listed times."
            )
        lines.append(
            "A customer may book several viewings on the SAME DAY at different times - that is normal and allowed. "
            "Never tell them a whole day is unavailable just because they already booked another property that day; "
            "only a specific TIME that is taken or full is unavailable. If they pick a day they already have a "
            "booking on, simply offer that day's remaining times."
        )
        return "\n".join(lines)
    except Exception as e:
        print(f"⚠️ Availability context error: {e}")
        return ""


def budget_string_to_numeric(budget_str):
    """Converts a budget string like '15 million USD' or '750 thousand' to a numeric USD estimate."""
    if not budget_str:
        return None
    s = budget_str.lower()
    m = re.search(r'(\d+(?:\.\d+)?)\s*(million|thousand)?', s)
    if not m:
        return None
    amount = float(m.group(1))
    unit = m.group(2)
    if unit == 'million':
        amount *= 1_000_000
    elif unit == 'thousand':
        amount *= 1_000
    return amount


BUY_WORDS = ['buy', 'buying', 'purchase', 'purchasing', 'own', 'ownership',
             'comprar', 'kaufen', 'acheter', 'acquistare', 'kupić', 'kupic']
RENT_WORDS = ['rent', 'renting', 'rental', 'lease', 'leasing',
              'alquiler', 'miete', 'louer', 'affitto', 'wynajem']

def detect_purpose(conversation_history):
    """Language-lite: scans user messages for buy/rent intent."""
    user_text = " ".join([m['content'] for m in conversation_history if m['role'] == 'user']).lower()
    if any(re.search(r'\b' + w + r'\b', user_text) for w in RENT_WORDS):
        return 'rent'
    if any(re.search(r'\b' + w + r'\b', user_text) for w in BUY_WORDS):
        return 'sale'
    return None


# ─────────────────────────────────────────────────────
# SELLER SIDE - the other half of a real agency's day. Someone arriving
# to LIST a property needs a completely different set of questions from
# someone arriving to buy one. Until now the AI treated every visitor as
# a buyer: it asked a seller for their "budget" and promised to send them
# listings.
# ─────────────────────────────────────────────────────

# Checked before the buy/rent words, and deliberately tighter: "I want to
# rent out my flat" contains "rent" and would otherwise read as a renter
# looking for a place.
SELL_PATTERNS = [
    r'\b(?:want|wish|would like|looking|trying|need|like)\s+to\s+sell\b',
    r'\bsell(?:ing)?\s+(?:my|our|a|the|this)\b',
    r'\bput\s+(?:my|our|the)\s+[\w\s]{0,20}?on\s+the\s+market\b',
    r'\blist\s+(?:my|our)\s+(?:property|house|home|villa|apartment|condo|flat)\b',
    r'\bfor\s+sale\s+by\s+owner\b',
    r'\bvender\s+mi\b', r'\bverkaufen\b', r'\bvendre\s+m',
    r'\bvendere\s+la\s+mia\b', r'\bsprzeda',
]

RENT_OUT_PATTERNS = [
    r'\brent(?:ing)?\s+out\b',
    r'\blease\s+out\b',
    r'\blet\s+out\b',
    r'\brent\s+(?:my|our)\s+(?:property|house|home|villa|apartment|condo|flat)\b',
    r'\bi\s+am\s+(?:a\s+)?landlord\b',
    r'\balquilar\s+mi\b', r'\bvermieten\b',
]


def detect_chat_intent(conversation_history):
    """Which side of the transaction is this visitor on?
    Returns 'sell', 'rent_out', 'buy', 'rent', or None if not yet stated."""
    if not conversation_history:
        return None
    user_text = " ".join(m['content'] for m in conversation_history
                         if m['role'] == 'user').lower()
    for pattern in SELL_PATTERNS:
        if re.search(pattern, user_text):
            return 'sell'
    for pattern in RENT_OUT_PATTERNS:
        if re.search(pattern, user_text):
            return 'rent_out'
    purpose = detect_purpose(conversation_history)
    if purpose == 'rent':
        return 'rent'
    if purpose == 'sale':
        return 'buy'
    return None


def is_seller_intent(intent):
    return intent in ('sell', 'rent_out')


GENERIC_PROPERTY_TYPES =['villa', 'condo', 'condominium', 'apartment', 'house', 'home', 'townhouse',
                           'town house', 'single family', 'mansion', 'estate', 'loft', 'penthouse',
                           'duplex', 'bungalow', 'cottage']

# Maps colloquial/generic terms a customer might say to the actual type
# strings that show up in a listings database, since real inventories
# rarely use the word "house" literally (they say "Single Family"), and a
# naive substring check would otherwise let "house" wrongly match
# "Townhouse" just because the letters happen to appear inside it.
TYPE_SYNONYMS = {
    'house': {'single family', 'house'},
    'home': {'single family', 'house', 'home'},
    'single family': {'single family'},
    'single-family': {'single family'},
    'condo': {'condo', 'condominium'},
    'condominium': {'condo', 'condominium'},
    'apartment': {'condo', 'apartment'},
    'villa': {'villa'},
    'townhouse': {'townhouse'},
    'town house': {'townhouse'},
    'mansion': {'luxury estate', 'mansion', 'estate'},
    'estate': {'luxury estate', 'estate'},
    'luxury estate': {'luxury estate'},
    'penthouse': {'penthouse', 'condo'},
    'duplex': {'duplex'},
    'bungalow': {'bungalow', 'single family'},
    'cottage': {'cottage', 'single family'},
    'loft': {'loft', 'condo'},
}


def expand_type_synonyms(prop_type):
    """A customer's stated type ('house') expands to every DB type string
    that should count as a match ('single family', 'house')."""
    if not prop_type:
        return set()
    return TYPE_SYNONYMS.get(prop_type, {prop_type})


def detect_property_type(agency_id, conversation_history):
    """Matches conversation text against this agency's actual listing types + generic terms."""
    user_text = " ".join([m['content'] for m in conversation_history if m['role'] == 'user']).lower()
    db_types = [t[0].lower() for t in db.session.query(Listing.property_type)
                .filter_by(agency_id=agency_id).distinct().all() if t[0]]
    candidates = set(db_types) | set(GENERIC_PROPERTY_TYPES)
    # Longest phrases first so "single family" matches before a shorter
    # coincidental overlap would.
    ordered = sorted(candidates, key=len, reverse=True)
    for c in ordered:
        if c and re.search(r'\b' + re.escape(c) + r'\b', user_text):
            return c
    # Fallback: plain substring match (no word boundaries). Needed for
    # compound-word languages like German, where a qualifier attaches
    # directly to the noun with no space ("Luxusvilla" = "Luxus"+"villa"),
    # so a strict \bvilla\b never matches even though the word is right
    # there. Minimum length guards against short-word false positives.
    for c in ordered:
        if c and len(c) >= 4 and c in user_text:
            return c
    return None


def detect_location(agency_id, conversation_history):
    """Matches conversation text against this agency's actual listing
    locations (city and state parts), DB-driven so it adapts to whatever
    markets the agency actually serves. Only matches city names and full
    words 3+ letters long - short 2-letter state codes are deliberately
    excluded because several collide with common English words (e.g. 'OR'
    for Oregon, 'IN' for Indiana, 'HI' for Hawaii) and would false-match
    constantly. This also means a customer saying just the city name
    ('Boston') matches correctly even though listings store the full
    'Boston, MA' - the state code was never part of the candidate set.

    Returns a LIST of every distinct city mentioned, in the order they
    appear in the conversation - a customer saying "New York or Nashville"
    means EITHER is acceptable, not just whichever one our old
    single-value version happened to check first. Also recognizes the
    Spanish-translated form of city names containing 'New' (e.g. 'Nueva
    York' -> 'New York'), since that's the one common case where a US
    city name genuinely changes across our supported languages."""
    if not conversation_history:
        return []
    user_text = " ".join([m['content'] for m in conversation_history if m['role'] == 'user']).lower()
    db_locations = [l[0] for l in db.session.query(Listing.location)
                     .filter_by(agency_id=agency_id).distinct().all() if l[0]]
    # candidate -> canonical city name (usually itself, except translated aliases)
    candidates = {}
    for loc in db_locations:
        for part in loc.split(','):
            part = part.strip().lower()
            if len(part) >= 3:
                candidates[part] = part
                if part.startswith('new '):
                    candidates['nueva ' + part[4:]] = part  # Spanish "Nueva York" -> "new york"

    found = []
    seen_canonical = set()
    for c in sorted(candidates.keys(), key=len, reverse=True):
        m = re.search(r'\b' + re.escape(c) + r'\b', user_text)
        if m:
            canonical = candidates[c]
            if canonical not in seen_canonical:
                seen_canonical.add(canonical)
                found.append((m.start(), canonical))
    found.sort(key=lambda x: x[0])
    return [c for _, c in found]


def detect_listing_titles_in_text(agency_id, text):
    """Returns this agency's listing titles that are literally mentioned in
    a piece of text (case-insensitive), longest-first so a more specific
    title is preferred over a shorter overlapping one. Used to figure out
    WHICH property a day/time is being booked for - titles are proper
    nouns the AI carries through unchanged even in non-English replies, so
    this stays multilingual-safe without any translation logic."""
    if not text:
        return []
    titles = [t[0] for t in db.session.query(Listing.title)
              .filter_by(agency_id=agency_id).distinct().all() if t[0]]
    text_lower = text.lower()
    found = []
    for title in sorted(titles, key=len, reverse=True):
        if title.lower() in text_lower:
            found.append(title)
    return found


def infer_budget_from_discussed_listings(agency_id, conversation_history):
    """When no explicit number was ever stated, use the HIGHEST price among
    listings that were actually named anywhere in the conversation as a
    reasonable stand-in for the customer's budget ceiling - most relevant
    once they've gone as far as asking questions about, or booking a
    viewing for, one of those specific properties. Replaces the old
    'next message must be a bare yes/sure' check, which was too brittle
    for normal conversational replies like 'That's great, tell me more'."""
    if not conversation_history:
        return None
    all_text = " ".join(m['content'] for m in conversation_history)
    rows = db.session.query(Listing.title, Listing.price_numeric) \
        .filter_by(agency_id=agency_id).all()
    all_text_lower = all_text.lower()
    mentioned_prices = [price for title, price in rows
                         if title and price and title.lower() in all_text_lower]
    if not mentioned_prices:
        return None
    highest = max(mentioned_prices)
    if highest >= 1_000_000:
        formatted = f"{highest / 1_000_000:.1f}".rstrip('0').rstrip('.')
        return f"{formatted} million USD (based on properties discussed)"
    elif highest >= 1_000:
        return f"{highest / 1_000:.0f} thousand USD (based on properties discussed)"
    return f"{highest:.0f} USD (based on properties discussed)"


def format_num(x):
    """4.0 -> '4', 4.5 -> '4.5' - clean display for bedroom/bathroom counts."""
    if x is None:
        return None
    try:
        return str(int(x)) if float(x) == int(x) else str(x)
    except (TypeError, ValueError):
        return str(x)


_NUMBER_WORD_ALT = '|'.join(re.escape(w) for w in sorted(NUMBER_WORDS.keys(), key=len, reverse=True))
_BED_WORD_ALT = '|'.join(re.escape(w) for w in sorted(BEDROOM_WORDS, key=len, reverse=True))
_BATH_WORD_ALT = '|'.join(re.escape(w) for w in sorted(BATHROOM_WORDS, key=len, reverse=True))
_BED_PATTERN = re.compile(rf'\b(\d+|{_NUMBER_WORD_ALT})\s*\+?\s*(?:{_BED_WORD_ALT})', re.IGNORECASE)
_BATH_PATTERN = re.compile(rf'\b(\d+|{_NUMBER_WORD_ALT})\s*\+?\s*(?:{_BATH_WORD_ALT})', re.IGNORECASE)


def _number_token_to_int(token):
    if token.isdigit():
        return int(token)
    return NUMBER_WORDS.get(token.lower())


def detect_bed_bath_requirements(conversation_history):
    """Scans ALL user messages for bedroom/bathroom count requirements, in
    any of our supported languages, and accepts BOTH digits ('5 bedrooms')
    and spelled-out numbers ('fünf Schlafzimmer', 'cinco habitaciones').
    Any number mentioned is treated as a MINIMUM threshold (the way real
    estate searches conventionally work: '4 beds' or 'at least 4 baths'
    both mean >= 4). The LAST mention in the conversation wins, so a
    customer can revise their requirement mid-chat."""
    if not conversation_history:
        return None, None
    user_msgs = [m['content'] for m in conversation_history if m['role'] == 'user']
    min_beds, min_baths = None, None
    for msg in user_msgs:
        for m in _BED_PATTERN.finditer(msg):
            n = _number_token_to_int(m.group(1))
            if n:
                min_beds = n
        for m in _BATH_PATTERN.finditer(msg):
            n = _number_token_to_int(m.group(1))
            if n:
                min_baths = n
    return min_beds, min_baths


def get_listings_context(agency_id, conversation_history=None):
    """Filters and ranks listings by the customer's stated budget, property
    type, buy/rent purpose, AND bedroom/bathroom requirements BEFORE handing
    anything to the AI - so the model only ever sees relevant, correctly-
    scoped options and never has to eyeball a bed/bath match itself."""
    try:
        listings = Listing.query.filter_by(agency_id=agency_id, status='available').all()
        if not listings:
            return ""

        budget_val, purpose, prop_type, min_beds, min_baths, location_val = None, None, None, None, None, []
        if conversation_history:
            lead_snapshot = extract_lead_data(agency_id, conversation_history)
            budget_val = budget_string_to_numeric(lead_snapshot.get('budget'))
            purpose = detect_purpose(conversation_history)
            prop_type = detect_property_type(agency_id, conversation_history)
            min_beds, min_baths = detect_bed_bath_requirements(conversation_history)
            location_val = detect_location(agency_id, conversation_history)

        def passes_bed_bath(l):
            if min_beds is not None and (l.bedrooms is None or l.bedrooms < min_beds):
                return False
            if min_baths is not None and (l.bathrooms is None or l.bathrooms < min_baths):
                return False
            return True

        def passes_location(l):
            if not location_val:
                return True
            listing_loc = (l.location or '').lower()
            return any(loc in listing_loc for loc in location_val)

        # Purpose is always a hard filter (never show a rental to a buyer or vice versa)
        purpose_ok = [l for l in listings if not purpose or not l.listing_purpose or l.listing_purpose == purpose]

        # Cascading hard-filter: try the most specific combination first
        # (location + bed/bath), then relax bed/bath before relaxing
        # location (a customer is usually firmer about which city they
        # want than about an exact bathroom count), so the AI always gets
        # the closest real alternatives instead of silently substituting
        # a completely different city with no disclosure.
        location_relaxed = False
        bed_bath_relaxed = False

        level1 = [l for l in purpose_ok if passes_location(l) and passes_bed_bath(l)]
        if level1:
            candidates = level1
        else:
            level2 = [l for l in purpose_ok if passes_location(l)]
            if level2:
                candidates = level2
                bed_bath_relaxed = bool(min_beds or min_baths)
            else:
                level3 = [l for l in purpose_ok if passes_bed_bath(l)]
                if level3:
                    candidates = level3
                    location_relaxed = bool(location_val)
                else:
                    candidates = purpose_ok
                    location_relaxed = bool(location_val)
                    bed_bath_relaxed = bool(min_beds or min_baths)

        scored = []
        for l in candidates:
            score = 0
            if prop_type:
                expanded_types = expand_type_synonyms(prop_type)
                type_field = (l.property_type or '').lower()
                title_field = (l.title or '').lower()
                if type_field in expanded_types:
                    score += 3
                elif any(re.search(r'\b' + re.escape(t) + r'\b', title_field) for t in expanded_types):
                    score += 2
            # Bed/bath proximity. This is what was missing: once the exact
            # requirement couldn't be met and bed/bath got relaxed, EVERY
            # listing scored the same here, so the cheapest-first tiebreak
            # below handed the AI the 8 smallest/cheapest homes - a customer
            # asking for 7 bedrooms was shown 2-bed starters while the 6-bed
            # estates never reached the model at all. Closeness to the asked
            # size now outranks price.
            if min_beds is not None:
                if l.bedrooms is None:
                    score -= 1
                elif l.bedrooms >= min_beds:
                    score += 4
                else:
                    score += max(0, 4 - (min_beds - l.bedrooms))
            if min_baths is not None and l.bathrooms is not None:
                if l.bathrooms >= min_baths:
                    score += 2
                else:
                    score += max(0, 2 - int(min_baths - l.bathrooms))
            if budget_val and l.price_numeric:
                lo, hi = budget_val * 0.7, budget_val * 1.3
                if lo <= l.price_numeric <= hi:
                    score += 3
                else:
                    closeness = min(l.price_numeric, budget_val) / max(l.price_numeric, budget_val)
                    if closeness >= 0.5:
                        score += 1
                    else:
                        continue  # too far outside budget - exclude
            scored.append((score, l))

        # Nothing matched at all? Fall back to purpose-correct listings
        # so the AI still has real options rather than nothing at all.
        if not scored and (purpose or prop_type or budget_val or location_val):
            scored = [(0, l) for l in purpose_ok]

        # Tiebreak depends on what the customer actually asked for. If they
        # named a size, bigger-is-closer wins (they asked for 7 beds - show
        # the 6-beds, not the 2-beds). With no size stated, cheapest-first is
        # the friendlier default for an open browse.
        if min_beds is not None or min_baths is not None:
            scored.sort(key=lambda x: (-x[0], -(x[1].bedrooms or 0), x[1].price_numeric or 0))
        else:
            scored.sort(key=lambda x: (-x[0], x[1].price_numeric or 0))
        top = [l for s, l in scored[:8]]

        if not top:
            return ("\n\nNo listings currently match this customer's stated criteria. "
                    "Do NOT invent or approximate a listing - tell them you'll keep an eye out and follow up.")

        lines = ["\n\nMATCHING PROPERTIES (already filtered/ranked for this customer - recommend ONLY from this list):"]
        for i, l in enumerate(top, 1):
            bed_bath = ""
            if l.bedrooms:
                bed_bath += f"{l.bedrooms}bed"
            if l.bathrooms:
                bed_bath += f"/{format_num(l.bathrooms)}bath"
            price_str = f"${l.price:,.0f}" if l.price else l.price_raw or "Price on request"
            purpose_tag = " [FOR RENT]" if l.listing_purpose == 'rent' else " [FOR SALE]"
            features_str = f" | {l.features}" if l.features else ""
            desc_str = f" - {l.description[:80]}..." if l.description and len(l.description) > 30 else (f" - {l.description}" if l.description else "")
            tags = []
            if location_val and not location_relaxed and passes_location(l):
                tags.append("MATCHES requested location")
            if (min_beds or min_baths) and not bed_bath_relaxed and passes_bed_bath(l):
                tags.append("MATCHES bedroom/bathroom requirement")
            match_tag = f" ✓ {', '.join(tags)}" if tags else ""
            lines.append(
                f"{i}. {l.title}{purpose_tag} | {l.location} | {price_str}"
                f"{' | ' + bed_bath if bed_bath else ''}"
                f"{features_str}"
                f"{desc_str}"
                f"{match_tag}"
            )
        if location_val and location_relaxed:
            location_list_str = "' or '".join(location_val)
            lines.append(
                f"\nNote: none of our listings are in '{location_list_str}' with the customer's other criteria - "
                "the list above are the closest alternatives in OTHER locations. Be upfront that these are not "
                "in the area(s) they asked for before describing them, then offer them as alternatives."
            )
        if (min_beds or min_baths) and bed_bath_relaxed:
            lines.append(
                f"\nNote: none of our listings have at least {min_beds or '?'} bed / {min_baths or '?'} bath exactly - "
                "the list above are the closest alternatives. Be honest that the exact combination isn't available "
                "and offer these as alternatives instead."
            )
        lines.append(
            "\nBe specific: mention price, bedrooms, bathrooms, location, and whether it's for sale or rent. "
            "If a listing is tagged with a ✓ match, confirm clearly that it meets what the customer asked for. "
            "If a listing is NOT tagged as matching location or bed/bath even though the customer specified one, "
            "be upfront about that mismatch before describing it - never present a different city or a smaller "
            "unit as if it were exactly what they asked for. Create mild urgency naturally."
        )

        # Truthful whole-inventory facts. The shortlist above is capped, so
        # without this the AI answers "what's the biggest you have?" from the
        # 8 rows it can see and confidently denies stock the agency really
        # holds. These numbers describe EVERY available listing.
        bed_values = [l.bedrooms for l in listings if l.bedrooms]
        price_values = [l.price_numeric for l in listings if l.price_numeric]
        city_values = sorted({(l.location or '').split(',')[0].strip()
                              for l in listings if l.location})
        facts = [f"{len(listings)} properties available in total"]
        if bed_values:
            facts.append(f"bedroom counts range {min(bed_values)} to {max(bed_values)}")
        if price_values:
            facts.append(f"prices range ${min(price_values):,.0f} to ${max(price_values):,.0f}")
        if city_values:
            facts.append(f"cities covered: {', '.join(city_values[:12])}")
        lines.append(
            "\nWHOLE-INVENTORY FACTS (true across the entire agency, not just the shortlist above): "
            + "; ".join(facts) + ". "
            "The shortlist above is only the closest matches, so NEVER use it to claim the agency has nothing "
            "in a size, price or city that these facts show it does have. If the customer asks about something "
            "outside the shortlist that these facts cover, tell them yes, those exist, and ask a question that "
            "narrows it down (budget or area) so the right ones can be pulled up for them."
        )
        return "\n".join(lines)
    except Exception as e:
        print(f"⚠️ Listings context error: {e}")
        return ""


def quality_reasons_text(lead):
    """The star rating spelled out, for emails. An owner who disagrees
    with a score should be able to see exactly which signal produced it
    rather than arguing with a number."""
    try:
        reasons = json.loads(lead.quality_reasons or '[]')
    except Exception:
        return ""
    if not reasons:
        return ""
    return "\n".join(f"   {pts}  {why}" for pts, why in reasons)


def send_lead_email(agency, lead):
    subject = f"🎯 New Qualified Lead for {agency.name}"
    contact_info = ""
    if lead.whatsapp_number:
        clean_num = clean_whatsapp_number(lead.whatsapp_number)
        wa_link = f"https://wa.me/{clean_num}" if clean_num else "N/A"
        contact_info = f"💬 WhatsApp: {lead.whatsapp_number}\n🔗 Click to Chat: {wa_link}"
    elif lead.phone:
        contact_info = f"📱 Phone:    {lead.phone}"
    else:
        contact_info = "📱 Phone:    Not provided"

    pref = lead.contact_preference or 'email'
    pref_display = {
        'email': 'Email', 'whatsapp': 'WhatsApp', 'phone': 'Phone',
        'email_and_whatsapp': 'Email & WhatsApp', 'email_and_phone': 'Email & Phone'
    }.get(pref, pref.title())

    body = f"""
New QUALIFIED Lead Received from {agency.name}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
👤 Name:    {lead.name or 'Not provided'}
📧 Email:   {lead.email or 'Not provided'}
{contact_info}
💰 Budget:  {lead.budget or 'Not provided'}
⏱️ Timeline: {timeline_label(lead.timeline)}{f" — “{lead.timeline_raw}”" if lead.timeline_raw else ""}
📞 Prefers: {pref_display}

📝 CUSTOMER INSIGHTS:
{lead.message or 'No summary available'}

🌟 Lead Quality: {"⭐" * (lead.intent_score or 1)} ({lead.intent_score}/5)
{quality_reasons_text(lead)}

📅 Date: {lead.created_at.strftime('%Y-%m-%d %H:%M:%S')}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Login to view all leads:
{PUBLIC_BASE_URL}/owner-login

Agency ID: {agency.id}
"""
    return send_email_brevo(agency.email, subject, body)


def send_appointment_confirmation(agency, appointment):
    assigned_agent_name = None
    if appointment.agent_id:
        assigned_agent = db.session.get(Agent, appointment.agent_id)
        if assigned_agent:
            assigned_agent_name = assigned_agent.name

    agent_line_customer = f"🧑‍💼 Your Agent:  {assigned_agent_name}\n" if assigned_agent_name else ""
    agent_line_agency = f"🧑‍💼 Assigned Agent: {assigned_agent_name}\n" if assigned_agent_name else ""

    customer_subject = f"✅ Appointment Confirmed - {agency.name}"
    customer_body = f"""
Dear {appointment.customer_name},

Your property viewing appointment has been confirmed!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🏠 Agency:       {agency.name}
{agent_line_customer}📅 Date:         {appointment.appointment_date}
🕐 Time:         {appointment.appointment_time}
🏡 Property:     {appointment.property_interest or 'To be discussed'}
📋 Status:       Confirmed
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

We look forward to meeting you!

Best regards,
{agency.name} Team
"""
    agency_subject = f"📅 New Appointment Booked - {appointment.customer_name}"
    agency_body = f"""
New Appointment Booked!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
👤 Customer:     {appointment.customer_name}
📧 Email:        {appointment.customer_email}
{agent_line_agency}📅 Date:         {appointment.appointment_date}
🕐 Time:         {appointment.appointment_time}
🏡 Interested In: {appointment.property_interest or 'General viewing'}
📋 Status:       {appointment.status.title()}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

View all appointments:
{PUBLIC_BASE_URL}/appointments/{agency.id}
"""
    sent_customer = send_email_brevo(appointment.customer_email, customer_subject, customer_body)
    sent_agency = send_email_brevo(agency.email, agency_subject, agency_body)
    return sent_customer or sent_agency

def notify_agent(agent, subject, body):
    if agent and agent.email:
        return send_email_brevo(agent.email, subject, body)
    return False


def get_related_appointments(agency_id, customer_email):
    """All appointments across the agency for this customer email, regardless
    of which agent they're assigned to - so every agent working with the
    same client can see the full picture, not just their own bookings."""
    if not customer_email:
        return []
    email_lower = customer_email.strip().lower()
    appts = Appointment.query.filter_by(agency_id=agency_id).all()
    matched = [a for a in appts if (a.customer_email or '').strip().lower() == email_lower]
    matched.sort(key=lambda a: a.created_at or datetime.min, reverse=True)
    return matched


def resolve_lead_identity(agency_id, email, new_name):
    """Single source of truth for 'who is this customer' whenever we're
    about to create a Lead or an Appointment. Looks up any existing lead
    under this email and reconciles the name:
      - no existing lead -> nothing to reconcile, just return the given name
      - existing lead with no name yet -> adopt the new name
      - existing lead with the SAME name (case-insensitive) -> no conflict
      - existing lead with a DIFFERENT name -> the same email is now being
        used under a different name (a genuine second person, a typo, or a
        shared inbox). We do NOT silently overwrite the established
        identity - the ORIGINAL name is kept as canonical (so the lead
        record and every appointment tied to it stay consistent with each
        other), and a visible system note is added flagging the conflict
        so staff can verify with the customer which name is correct.
    Returns (canonical_name, lead_id_or_None)."""
    if not email:
        return new_name, None
    existing = Lead.query.filter_by(agency_id=agency_id, email=email).first()
    if not existing:
        return new_name, None
    if not existing.name:
        if new_name:
            existing.name = new_name
            db.session.commit()
        return existing.name or new_name, existing.id
    if not new_name or existing.name.strip().lower() == new_name.strip().lower():
        return existing.name, existing.id
    # Names differ under the same email - flag it, but keep the ORIGINAL
    # name as canonical rather than silently switching identities.
    try:
        notes = json.loads(existing.notes or '[]')
    except Exception:
        notes = []
    notes.append({
        "id": len(notes) + 1,
        "text": (f"⚠️ Possible duplicate: this email was already used here as '{existing.name}'. "
                 f"A new conversation under the SAME email just used the name '{new_name}' instead. "
                 f"We kept '{existing.name}' as the name on file - please verify with the customer "
                 f"which name is correct, or whether this is a different person sharing the same email."),
        "author": "System",
        "timestamp": datetime.now(pytz.timezone('Asia/Karachi')).strftime('%B %d, %Y at %I:%M %p')
    })
    existing.notes = json.dumps(notes)
    db.session.commit()
    print(f"⚠️ Name conflict on {email}: kept '{existing.name}' (new session used '{new_name}') - flagged on lead {existing.id}")
    return existing.name, existing.id


def notify_other_agents_of_update(appt, acting_agent, action_desc):
    """If this appointment's customer is also a lead assigned to a DIFFERENT
    agent, let that agent know so nobody is left out of the loop when a
    client is being worked by more than one agent at the same agency."""
    if not appt.customer_email or not acting_agent:
        return
    email_lower = appt.customer_email.strip().lower()
    leads = Lead.query.filter_by(agency_id=appt.agency_id).all()
    for lead in leads:
        if (lead.email and lead.email.strip().lower() == email_lower
                and lead.agent_id and lead.agent_id != acting_agent.id):
            other_agent = db.session.get(Agent, lead.agent_id)
            if other_agent:
                notify_agent(other_agent,
                    f"🔔 Update on shared client: {appt.customer_name or lead.name or ''}",
                    f"Hi {other_agent.name},\n\n{acting_agent.name} just {action_desc} for a client you're also working with:\n\n"
                    f"Client: {lead.name or appt.customer_name}\nEmail: {appt.customer_email}\n"
                    f"Appointment date: {appt.appointment_date}\nTime: {appt.appointment_time}\n"
                    f"Status: {appt.status}\nNotes: {appt.notes or '-'}\n\n"
                    f"Login to see the full picture: {PUBLIC_BASE_URL}/agent-login")


# ─────────────────────────────────────────────────────
# ACTIVITY FEED - who changed what, and who gets told
# ─────────────────────────────────────────────────────

ACTIVITY_ICONS = {
    'lead_status': '📊', 'lead_note': '🗒️', 'lead_new': '🎯',
    'lead_reassigned': '🔄',
    'appointment_stage': '📅', 'appointment_note': '🗒️',
    'appointment_outcome': '🏁', 'appointment_new': '🆕',
    'appointment_reassigned': '🔄', 'appointment_cancelled': '❌',
    'listing_approved': '✅', 'listing_rejected': '🚫',
    'seller_lead': '🏠', 'customer_feedback': '💬',
}

LOGIN_URLS = {
    'owner': f'{PUBLIC_BASE_URL}/owner-login',
    'agent': f'{PUBLIC_BASE_URL}/agent-login',
}


def activity_icon(action):
    return ACTIVITY_ICONS.get(action, '🔔')


def record_activity(agency_id, action, summary, actor_type='system',
                    actor_name=None, subject_type=None, subject_id=None,
                    subject_name=None, agent_id=None, notify=True):
    """Write one feed row and tell the other side by email.

    Deliberately never raises: a failed notification must not roll back
    the status change the user actually asked for. The feed row is
    committed first for the same reason - if Brevo is down, the dashboard
    still shows what happened.

    'The other side' means: an agent acts, the owner hears about it; the
    owner acts, the assigned agent hears about it. Nobody is emailed
    about their own click."""
    event = None
    try:
        event = ActivityEvent(
            agency_id=agency_id, agent_id=agent_id,
            actor_type=actor_type, actor_name=actor_name or 'System',
            action=action, summary=summary[:400],
            subject_type=subject_type, subject_id=subject_id,
            subject_name=(subject_name or '')[:150] or None,
            # The side that did it has already seen it.
            seen_by_owner=1 if actor_type == 'owner' else 0,
            seen_by_agent=1 if actor_type == 'agent' else 0,
        )
        db.session.add(event)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"⚠️ Activity record failed: {e}")

    if not notify:
        return event

    try:
        agency = db.session.get(Agency, agency_id)
        subject_line = f"{activity_icon(action)} {summary[:120]}"
        who = subject_name or 'a client'

        def body_for(greeting, login_url):
            lines = [
                f"Hi {greeting},", "",
                summary, "",
                f"Client: {who}",
                f"When:   {event.created_at.strftime('%b %d, %Y at %I:%M %p') if event and event.created_at else 'just now'}",
                "", f"See the full picture: {login_url}",
            ]
            return "\n".join(lines)

        if actor_type == 'agent':
            if agency and agency.email:
                send_email_brevo(agency.email, subject_line,
                                 body_for(agency.owner_name or agency.name, LOGIN_URLS['owner']))
        elif actor_type == 'owner':
            if agent_id:
                agent = db.session.get(Agent, agent_id)
                if agent:
                    notify_agent(agent, subject_line, body_for(agent.name, LOGIN_URLS['agent']))
        else:
            # System events (a new lead, a customer's own feedback) concern
            # both sides, so both get told.
            if agency and agency.email:
                send_email_brevo(agency.email, subject_line,
                                 body_for(agency.owner_name or agency.name, LOGIN_URLS['owner']))
            if agent_id:
                agent = db.session.get(Agent, agent_id)
                if agent:
                    notify_agent(agent, subject_line, body_for(agent.name, LOGIN_URLS['agent']))
    except Exception as e:
        print(f"⚠️ Activity notification failed: {e}")

    return event


def acting_identity():
    """Who is making this request, from the session only. A client-supplied
    actor could otherwise put anyone's name against anyone's change."""
    agent_id = session.get('agent_id')
    if agent_id:
        agent = db.session.get(Agent, int(agent_id))
        return 'agent', (agent.name if agent else 'An agent'), int(agent_id)
    if session.get('agency_id'):
        agency = db.session.get(Agency, int(session['agency_id']))
        return 'owner', (agency.owner_name or agency.name if agency else 'The owner'), None
    if session.get('super_admin'):
        return 'owner', 'Platform admin', None
    return 'system', 'System', None


def recent_activity(agency_id, agent_id=None, limit=20):
    """The feed for one dashboard. The owner sees the whole agency; an
    agent sees only what touches their own leads and viewings, which is
    the difference between a useful feed and a noisy one."""
    q = ActivityEvent.query.filter_by(agency_id=agency_id)
    if agent_id is not None:
        q = q.filter(ActivityEvent.agent_id == agent_id)
    return q.order_by(ActivityEvent.created_at.desc(),
                      ActivityEvent.id.desc()).limit(limit).all()


def unseen_activity_count(agency_id, agent_id=None):
    q = ActivityEvent.query.filter_by(agency_id=agency_id)
    if agent_id is not None:
        q = q.filter(ActivityEvent.agent_id == agent_id,
                     ActivityEvent.seen_by_agent == 0)
    else:
        q = q.filter(ActivityEvent.seen_by_owner == 0)
    return q.count()


def send_crm_webhook(agency, lead):
    if not agency.webhook_url:
        return
    try:
        payload = {
            "event": "lead_qualified",
            "agency_id": agency.id, "agency_name": agency.name,
            "lead": {
                "id": lead.id, "name": lead.name, "email": lead.email,
                "phone": lead.phone, "whatsapp_number": lead.whatsapp_number,
                "contact_preference": lead.contact_preference,
                "budget": lead.budget, "summary": lead.message,
                "intent_score": lead.intent_score,
                "created_at": lead.created_at.isoformat() if lead.created_at else None
            }
        }
        response = httpx.post(agency.webhook_url, json=payload, timeout=5)
        print(f"✅ Webhook sent (Status: {response.status_code})")
    except Exception as e:
        print(f"⚠️ Webhook failed: {e}")


def send_followup_email(agency, lead, day):
    contact = lead.whatsapp_number or lead.phone or "Not provided"
    stars = "⭐" * (lead.intent_score or 1)
    if day == 1:
        subject = f"⏰ Day 1 Follow-up: {lead.name or 'New Lead'} | {agency.name}"
        body = f"""
Hi {agency.owner_name or agency.name},

Time to follow up with your qualified lead from yesterday!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
👤 Name:    {lead.name or '—'}
📧 Email:   {lead.email or '—'}
📱 Contact: {contact}
💰 Budget:  {lead.budget or '—'}
🌟 Quality: {stars} ({lead.intent_score}/5)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💡 Customer Insights:
{lead.message or 'No summary available'}

🎯 Suggested Action: Reach out via {(lead.contact_preference or 'email').title()} within 24 hours.

Login to view: {PUBLIC_BASE_URL}/owner-login
"""
    elif day == 7:
        subject = f"📅 7-Day Check-in: {lead.name or 'Lead'} | {agency.name}"
        body = f"""
Hi {agency.owner_name or agency.name},

It's been 7 days since {lead.name or 'this lead'} qualified. Time for a re-engagement!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
👤 Name:    {lead.name or '—'}
📧 Email:   {lead.email or '—'}
📱 Contact: {contact}
💰 Budget:  {lead.budget or '—'}
🌟 Quality: {stars} ({lead.intent_score}/5)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💬 Re-engagement script:
"Hey {lead.name or 'there'}, just checking in! Have you found anything you like yet?
I have a couple of new listings that might fit what you're looking for."

Login to view: {PUBLIC_BASE_URL}/owner-login
"""
    else:
        return False

    sent = send_email_brevo(agency.email, subject, body)

    # The assigned agent is the person who actually has to make this call -
    # sending the reminder only to the owner meant it never reached them.
    # The owner still gets their copy above for oversight.
    if lead.agent_id:
        agent = db.session.get(Agent, lead.agent_id)
        if agent and agent.email and agent.email.strip().lower() != (agency.email or '').strip().lower():
            agent_body = body.replace(
                f"Hi {agency.owner_name or agency.name},",
                f"Hi {agent.name},", 1
            ).replace(
                f"{PUBLIC_BASE_URL}/owner-login",
                f"{PUBLIC_BASE_URL}/agent-login"
            )
            agent_body = agent_body.replace(
                "your qualified lead", "your assigned lead", 1)
            sent = send_email_brevo(agent.email, subject, agent_body) or sent

    return sent


def process_pending_followups():
    try:
        now = datetime.utcnow()
        day1_count = 0
        day7_count = 0
        day1_cutoff = now - timedelta(hours=24)
        day1_leads = Lead.query.filter(Lead.follow_up_1_sent == 0, Lead.created_at <= day1_cutoff).all()
        for lead in day1_leads:
            agency = db.session.get(Agency, lead.agency_id)
            if agency and send_followup_email(agency, lead, 1):
                lead.follow_up_1_sent = 1
                day1_count += 1
        db.session.commit()
        day7_cutoff = now - timedelta(days=7)
        day7_leads = Lead.query.filter(Lead.follow_up_7_sent == 0, Lead.created_at <= day7_cutoff).all()
        for lead in day7_leads:
            agency = db.session.get(Agency, lead.agency_id)
            if agency and send_followup_email(agency, lead, 7):
                lead.follow_up_7_sent = 1
                day7_count += 1
        db.session.commit()
        print(f"✅ Follow-ups processed: D1={day1_count}, D7={day7_count}")
        return {"day1": day1_count, "day7": day7_count}
    except Exception as e:
        print(f"⚠️ Follow-up error: {e}")
        db.session.rollback()
        return {"error": str(e)}


# ─────────────────────────────────────────────────────
# POST-APPOINTMENT LOOP (Item 6) - the AI qualifies and books, but every
# comparative analysis flagged the same gap: nothing happens after the
# viewing. This closes it with two paths that both land on the same
# Appointment.outcome field:
#   1) automated: once the appointment's date/time is in the past, email
#      the CUSTOMER a 3-option check-in (buy / other options / not
#      interested) with a link back to /appointment-feedback/<token>.
#   2) manual: the agent (or owner) can set the same outcome directly
#      from their dashboard, for viewings the customer discusses by phone
#      instead of clicking the email link.
# "Wants to buy" notifies the assigned agent (or the owner, solo tier) -
# no automatic pipeline change, no e-signature, nothing legal, per Moaz's
# explicit instruction. "Wants other options" hands them straight back to
# the same AI chat widget a first-time visitor gets - full re-qualification
# and re-booking, no separate matching logic to build or maintain here.
# ─────────────────────────────────────────────────────

APPOINTMENT_OUTCOMES = ('wants_to_buy', 'wants_other_options', 'not_interested')

# One list, one dropdown, everywhere. Scheduling states and post-viewing
# results used to be two separate controls (status + outcome) that a user
# had to keep in sync by hand; they are now a single ordered stage. The two
# database columns survive underneath because the customer feedback page and
# the check-in cron both key off `outcome`.
APPOINTMENT_STAGES = (
    'pending', 'confirmed', 'completed',
    'wants_to_buy', 'wants_other_options', 'not_interested',
    'cancelled',
)

APPOINTMENT_STAGE_LABELS = {
    'pending': '⏳ Pending',
    'confirmed': '✅ Confirmed',
    'completed': '🏁 Viewing done',
    'wants_to_buy': '🎉 Wants to buy',
    'wants_other_options': '🔍 Wants other options',
    'not_interested': '🚫 Not interested',
    'cancelled': '❌ Cancelled',
}


def appointment_stage(appt):
    """The single value the UI shows. An outcome always implies the viewing
    happened, so it outranks the scheduling status."""
    if appt.status == 'cancelled':
        return 'cancelled'
    if appt.outcome in APPOINTMENT_OUTCOMES:
        return appt.outcome
    return appt.status or 'pending'


def apply_appointment_stage(appt, stage, source):
    """Write one chosen stage back to both underlying columns so every
    screen agrees. Returns False for an unknown stage."""
    if stage not in APPOINTMENT_STAGES:
        return False
    if stage in APPOINTMENT_OUTCOMES:
        # Recording a result also marks the viewing as having happened.
        appt.status = 'completed'
        return apply_appointment_outcome(appt, stage, source)
    appt.status = stage
    if stage in ('pending', 'confirmed'):
        # Rescheduling clears a result that no longer applies.
        appt.outcome = None
        appt.outcome_source = None
        appt.outcome_at = None
    db.session.commit()
    return True


def _appointment_datetime(appt):
    """Best-effort combine of appointment_date_iso + appointment_time (e.g.
    '2026-08-20' + '2:00 PM', matching the fixed TIME_SLOTS format) into a
    timezone-aware datetime in the business's own timezone. Returns None for
    rows where either half is missing or unparseable, so callers can safely
    skip anything they can't make sense of instead of guessing."""
    if not appt.appointment_date_iso or not appt.appointment_time:
        return None
    try:
        tz = pytz.timezone('Asia/Karachi')
        naive = datetime.strptime(
            f"{appt.appointment_date_iso} {appt.appointment_time}", "%Y-%m-%d %I:%M %p"
        )
        return tz.localize(naive)
    except (ValueError, TypeError):
        return None


def send_appointment_checkin_email(agency, appt, token):
    base_url = PUBLIC_BASE_URL
    subject = f"How did your viewing go? - {agency.name}"
    body = f"""
Hi {appt.customer_name or 'there'},

Thanks for viewing {appt.property_interest or 'the property'} with {agency.name} on {appt.appointment_date} at {appt.appointment_time}.

We'd love to know how it went — just click whichever fits:

✅ I want to move forward with this one:
{base_url}/appointment-feedback/{token}?choice=buy

🔍 Show me other options:
{base_url}/appointment-feedback/{token}?choice=other

🚫 Not interested right now:
{base_url}/appointment-feedback/{token}?choice=no

Thanks!
{agency.name}
"""
    return send_email_brevo(appt.customer_email, subject, body)


def notify_agent_customer_wants_to_buy(agency, appt):
    """The one automatic notification the post-appointment loop sends: the
    assigned agent (or the owner, if this is a solo agency or nobody was
    assigned) finds out a customer is ready to move forward. Nothing else
    is automated from here per Moaz's instruction - no pipeline change, no
    e-signature."""
    agent = db.session.get(Agent, appt.agent_id) if appt.agent_id else None
    subject = f"🎉 {appt.customer_name or 'A customer'} wants to move forward!"
    body = f"""
Hi {agent.name if agent else (agency.owner_name or agency.name)},

Great news — {appt.customer_name or 'your customer'} just confirmed after their viewing on {appt.appointment_date} that they want to move forward with {appt.property_interest or 'the property'}.

📧 Email: {appt.customer_email or '—'}

Reach out to them as soon as you can to keep the momentum going!
"""
    if agent:
        return notify_agent(agent, subject, body)
    return send_email_brevo(agency.email, subject, body)


def apply_appointment_outcome(appt, outcome, source):
    """Shared by the customer-facing feedback link and the owner/agent
    manual-entry routes. Returns True on a valid outcome, False otherwise -
    callers turn False into a 400. The auto-notify-the-agent email only
    fires for a customer's own click (source='customer') - a human
    recording the outcome manually already knows it, so there's nothing to
    tell them that they didn't just type in themselves."""
    if outcome not in APPOINTMENT_OUTCOMES:
        return False
    appt.outcome = outcome
    appt.outcome_source = source
    appt.outcome_at = datetime.utcnow()
    db.session.commit()
    if outcome == 'wants_to_buy' and source == 'customer':
        agency = db.session.get(Agency, appt.agency_id)
        if agency:
            notify_agent_customer_wants_to_buy(agency, appt)
    return True


def process_appointment_checkins():
    """Cron-driven half of the loop - finds appointments whose slot has
    already passed with no outcome recorded and no check-in email sent yet,
    and emails the customer. Mirrors process_pending_followups()'s
    only-mark-as-sent-on-success pattern so a Brevo hiccup just means it
    gets retried on the next run instead of silently going dark forever."""
    try:
        now = datetime.now(pytz.timezone('Asia/Karachi'))
        candidates = Appointment.query.filter(
            Appointment.status != 'cancelled',
            Appointment.outcome.is_(None),
            Appointment.checkin_sent_at.is_(None),
        ).all()
        sent = 0
        for appt in candidates:
            appt_dt = _appointment_datetime(appt)
            if not appt_dt or appt_dt > now or not appt.customer_email:
                continue
            agency = db.session.get(Agency, appt.agency_id)
            if not agency:
                continue
            token = secrets.token_urlsafe(32)
            if send_appointment_checkin_email(agency, appt, token):
                appt.checkin_token = token
                appt.checkin_sent_at = datetime.utcnow()
                db.session.commit()
                sent += 1
        print(f"✅ Appointment check-ins processed: {sent} sent")
        return {"checkins_sent": sent}
    except Exception as e:
        print(f"⚠️ Appointment check-in error: {e}")
        db.session.rollback()
        return {"error": str(e)}


# ─────────────────────────────────────────────────────
# DB-BACKED CONVERSATION SESSIONS (survive restarts)
# ─────────────────────────────────────────────────────

def clean_expired_sessions():
    """Delete conversation sessions inactive for 30+ minutes (DB-backed)."""
    try:
        cutoff = datetime.utcnow() - timedelta(minutes=30)
        deleted = ConversationSession.query.filter(
            ConversationSession.updated_at < cutoff
        ).delete()
        if deleted:
            db.session.commit()
            print(f"🧹 {deleted} expired session(s) cleared")
    except Exception as e:
        print(f"⚠️ Session cleanup error: {e}")
        db.session.rollback()


def load_session(session_key):
    """Load conversation history + booked slots from DB. Survives restarts."""
    row = db.session.get(ConversationSession, session_key)
    if row:
        try:
            history = json.loads(row.history or '[]')
        except Exception:
            history = []
        try:
            booked = set(json.loads(row.booked_slots or '[]'))
        except Exception:
            booked = set()
        return history, booked
    print(f"🆕 New session started: {session_key}")
    return [], set()


def save_session(session_key, history, booked_slots):
    """Persist conversation history + booked slots to DB."""
    try:
        row = db.session.get(ConversationSession, session_key)
        if not row:
            row = ConversationSession(session_key=session_key)
            db.session.add(row)
        row.history = json.dumps(history)
        row.booked_slots = json.dumps(sorted(booked_slots))
        row.updated_at = datetime.utcnow()
        db.session.commit()
    except Exception as e:
        print(f"⚠️ Session save error: {e}")
        db.session.rollback()


def generate_lead_summary(conversation_history, agency_name):
    try:
        conversation_text = "\n".join([
            f"{'Customer' if msg['role'] == 'user' else 'Assistant'}: {msg['content']}"
            for msg in conversation_history
        ])
        analysis_prompt = f"""Analyze this conversation and create a 2-3 sentence business summary for {agency_name}.
Focus on: intent, property type, budget, location, timeline, urgency.
Write the summary in ENGLISH even if the conversation is in another language.
Conversation:
{conversation_text}
Format: "[INTENT] + [REQUIREMENTS] + [TIMELINE]"
Example: "Buyer seeking 3-bed villa in Dubai Marina, budget 2-3M AED, wants to move within 3 months."
Write summary:"""
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": analysis_prompt}],
            temperature=0.3, max_tokens=120
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"❌ Summary error: {e}")
        return "Customer engaged in property conversation."


def extract_seller_property(conversation_history, intent):
    """Pull the property a seller described into structured fields.

    Regex can't do this honestly: a seller's city may be somewhere the
    agency has no listings yet (so the existing DB-driven location matcher
    finds nothing), and free-text amenities ("12KW solar, private gym,
    cinema in the basement") have no pattern to match. One cheap model
    call per QUALIFIED seller lead - not per message - is far more
    reliable than guessing, and it returns null rather than inventing
    anything it wasn't told."""
    try:
        conversation_text = "\n".join(
            f"{'Owner' if m['role'] == 'user' else 'Assistant'}: {m['content']}"
            for m in conversation_history
        )
        prompt = f"""Extract the property this owner wants to {'sell' if intent == 'sell' else 'rent out'}.

Return ONLY valid JSON, no prose, with exactly these keys:
{{"title": str, "location": str, "property_type": str, "bedrooms": int|null,
  "bathrooms": float|null, "features": str, "price_raw": str, "description": str}}

Rules:
- Use null (not a guess) for anything the owner did not state.
- "title": a short listing title you compose from the facts, e.g. "Miami Beach Luxury Villa".
- "location": exactly the area the owner named, e.g. "Miami Beach, FL".
- "property_type": one of villa, house, condo, apartment, townhouse, penthouse, estate, land, other.
- "features": comma-separated amenities the owner mentioned, verbatim in meaning.
- "price_raw": the asking {'price' if intent == 'sell' else 'rent'} exactly as stated, e.g. "5M $" or "3500/month".
- "description": one factual sentence from what the owner said. Never invent selling points.
- Write all values in ENGLISH even if the conversation was in another language.

Conversation:
{conversation_text}

JSON:"""
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=400,
            response_format={"type": "json_object"},
        )
        data = json.loads(response.choices[0].message.content)
        print(f"🏷️ Seller property extracted: {data.get('title')} | {data.get('location')}")
        return data
    except Exception as e:
        print(f"⚠️ Seller property extraction error: {e}")
        return {}


def is_seller_lead_qualified(lead_data, conversation_history, intent):
    """A seller lead is worth creating once we can actually contact them
    AND know what they're offering. Deliberately stricter than the buyer
    rule about the property: a listing with no location or no price is not
    something an agent can act on."""
    if not is_seller_intent(intent):
        return False
    if not (lead_data.get('email') and lead_data.get('name')):
        return False
    user_msgs = [m for m in conversation_history if m['role'] == 'user']
    if len(user_msgs) < 4:
        return False
    # 'budget' is where the asking price lands for a seller - the same
    # money regex catches "5M $" whichever side of the deal they're on.
    return bool(lead_data.get('budget'))


def create_listing_from_seller(agency, lead, conversation_history, intent):
    """Turn a qualified seller conversation into a real (but unpublished)
    listing. It lands as 'pending' so it is invisible to buyers until the
    agency approves it - get_listings_context only ever reads 'available',
    so an unverified price or a duplicate submission can't reach a real
    customer on its own."""
    details = extract_seller_property(conversation_history, intent)
    if not details:
        return None

    price_raw = details.get('price_raw') or lead.budget or ''
    listing = Listing(
        agency_id=agency.id,
        title=(details.get('title') or f"{lead.name}'s property")[:200],
        location=(details.get('location') or '')[:200] or None,
        price_raw=str(price_raw)[:100] or None,
        price=parse_price(price_raw),
        price_numeric=parse_price(price_raw),
        bedrooms=details.get('bedrooms'),
        bathrooms=details.get('bathrooms'),
        property_type=(details.get('property_type') or '')[:50] or None,
        listing_purpose='rent' if intent == 'rent_out' else 'sale',
        features=(details.get('features') or '')[:500] or None,
        description=details.get('description'),
        status='pending',
        source='seller_chat',
        seller_lead_id=lead.id,
    )
    db.session.add(listing)
    db.session.commit()
    print(f"🏠 Pending listing #{listing.id} created from seller lead {lead.id}")
    return listing


def notify_owner_of_seller_lead(agency, lead, listing, intent):
    action = "sell" if intent == 'sell' else "rent out"
    lines = [
        f"Hi {agency.owner_name or agency.name},",
        "",
        f"A property owner just contacted you wanting to {action} their property.",
        "",
        f"👤 Name:    {lead.name or '—'}",
        f"📧 Email:   {lead.email or '—'}",
        f"📱 Contact: {lead.whatsapp_number or lead.phone or 'Not provided'}",
        "",
    ]
    if listing:
        lines += [
            "🏠 Property they described:",
            f"   {listing.title}",
            f"   Location: {listing.location or '—'}",
            f"   Type: {listing.property_type or '—'}",
            f"   Beds/baths: {listing.bedrooms or '—'} / {format_num(listing.bathrooms) if listing.bathrooms else '—'}",
            f"   Asking: {listing.price_raw or '—'}",
            f"   Features: {listing.features or '—'}",
            "",
            "It's saved as a PENDING listing - review and approve it before",
            "it becomes visible to buyers:",
            f"{PUBLIC_BASE_URL}/listings/{agency.id}",
        ]
    else:
        lines.append(f"Log in to review the conversation: {PUBLIC_BASE_URL}/owner-login")
    return send_email_brevo(
        agency.email,
        f"🏠 New Seller Lead: {lead.name or 'Property owner'} | {agency.name}",
        "\n".join(lines))


def extract_name_from_context(conversation_history):
    not_a_name = {
        'yes', 'no', 'ok', 'okay', 'sure', 'fine', 'good', 'great',
        'hello', 'hi', 'hey', 'thanks', 'thank', 'please', 'sorry',
        'email', 'phone', 'whatsapp', 'call', 'text', 'message',
        'looking', 'interested', 'want', 'need', 'like', 'going',
        'villa', 'house', 'apartment', 'property', 'condo', 'flat', 'home',
        'beach', 'miami', 'malibu', 'florida', 'california', 'usa',
        'within', 'about', 'around', 'budget', 'price', 'cost',
        'month', 'week', 'year', 'soon', 'asap', 'later', 'today',
        'just', 'also', 'here', 'there', 'then', 'when', 'where',
        'what', 'how', 'why', 'who', 'which', 'that', 'this', 'with',
        'from', 'have', 'been', 'will', 'would', 'could', 'should',
        'south', 'north', 'east', 'west', 'central', 'downtown',
        'coconut', 'grove', 'hilton', 'santa', 'monica', 'myrtle',
        'asking', 'checking', 'getting', 'making', 'trying'
    }

    # ── METHOD 0: Language-agnostic — reply to AI's very first message ──
    # The AI always asks for the name first. history[1]=assistant question,
    # history[2]=user's name reply. We strip a recognized SELF-INTRODUCTION
    # PREFIX PHRASE ("I am", "Ich bin", "Jestem"...) rather than a bag of
    # individually-strippable words, then take the very next token as the
    # name. This matters because some real first names collide with short
    # function words in other languages (e.g. "Kim" is also the Polish word
    # for "who") - a phrase-prefix match only fires when that exact
    # grammatical construction opens the message, so a name occupying the
    # NAME position is never mistaken for a filler word it happens to
    # resemble in an unrelated language.
    if (len(conversation_history) >= 3
            and conversation_history[1]['role'] == 'assistant'
            and conversation_history[2]['role'] == 'user'):
        candidate_msg = conversation_history[2]['content']
        cleaned = re.sub(r'[.,!?;:¿¡]', ' ', candidate_msg).strip()
        cleaned = re.sub(r'\s+', ' ', cleaned)

        for greet in NAME_GREETING_PREFIXES:
            gm = re.match(r'^' + re.escape(greet) + r'\b\s*', cleaned, re.IGNORECASE)
            if gm:
                cleaned = cleaned[gm.end():].strip()
                break

        for prefix_pattern in NAME_INTRO_PREFIXES:
            pm = re.match(prefix_pattern, cleaned, re.IGNORECASE)
            if pm:
                cleaned = cleaned[pm.end():].strip()
                break

        tokens = cleaned.split()
        if tokens:
            candidate = tokens[0]
            if (re.match(r'^[A-Za-zÀ-ÖØ-öø-ÿążćęłńóśźŻĄĆĘŁŃÓŚŹ]{2,30}$', candidate)
                    and candidate.lower() not in not_a_name):
                print(f"✅ Name (first-turn, lang-agnostic): {candidate.title()}")
                return candidate.title()

    name_question_patterns = [
        "what's your name", "what is your name", "whats your name",
        "your name?", "may i have your name", "can i get your name",
        "could i get your name", "mind sharing your name",
        "first name", "tell me your name", "know your name",
        "who i'm speaking with", "who i am speaking with", "who's this"
    ]
    for i, msg in enumerate(conversation_history):
        if msg['role'] == 'assistant':
            ai_text = msg['content'].lower()
            if any(pattern in ai_text for pattern in name_question_patterns):
                if i + 1 < len(conversation_history):
                    next_msg = conversation_history[i + 1]
                    if next_msg['role'] == 'user':
                        candidate = next_msg['content'].strip()
                        candidate = re.sub(
                            r'^(i\s+am|i\'m|my\s+name\s+is|name\s+is|it\'s|its|call\s+me|this\s+is)\s+',
                            '', candidate, flags=re.IGNORECASE).strip()
                        first_word = candidate.split()[0] if candidate.split() else ''
                        if (first_word and re.match(r'^[a-zA-Z]{2,30}$', first_word)
                                and first_word.lower() not in not_a_name):
                            print(f"✅ Name (context): {first_word.title()}")
                            return first_word.title()

    explicit_pattern = r'(?:i\s+am|i\'m|my\s+name\s+is|name\s+is|call\s+me|this\s+is)\s+([a-zA-Z]{2,30})(?:\s|[.,!?]|$)'
    found_names = []
    for msg in conversation_history:
        if msg['role'] == 'user':
            for match in re.finditer(explicit_pattern, msg['content'], re.IGNORECASE):
                candidate = match.group(1).strip()
                if (re.match(r'^[a-zA-Z]{2,30}$', candidate)
                        and candidate.lower() not in not_a_name):
                    found_names.append(candidate.title())
    if found_names:
        print(f"✅ Name (explicit): {found_names[-1]}")
        return found_names[-1]
    print("⚠️ Name: Not found")
    return None


def extract_lead_data(agency_id, conversation_history):
    full_conversation_user = " ".join([
        msg['content'] for msg in conversation_history if msg['role'] == 'user'
    ])
    lead_data = {
        'name': None, 'email': None, 'phone': None,
        'whatsapp_number': None, 'contact_preference': 'email', 'budget': None,
        'timeline': None, 'timeline_raw': None, 'budget_inferred': False
    }
    email_match = re.search(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", full_conversation_user)
    if email_match:
        lead_data['email'] = email_match.group(0)

    lead_data['name'] = extract_name_from_context(conversation_history)

    for i, msg in enumerate(conversation_history):
        if msg['role'] == 'assistant':
            if is_contact_question(msg['content']):
                if i + 1 < len(conversation_history):
                    next_msg = conversation_history[i + 1]
                    if next_msg['role'] == 'user':
                        user_pref = next_msg['content'].lower()
                        has_email = any(w in user_pref for w in ['email', 'e-mail', 'mail', 'correo'])
                        has_whatsapp = 'whatsapp' in user_pref or 'wa' in user_pref.split()
                        has_phone = any(w in user_pref for w in ['phone', 'call', 'telefon', 'teléfono', 'telefono', 'téléphone', 'telefone'])
                        if has_email and has_whatsapp:
                            lead_data['contact_preference'] = 'email_and_whatsapp'
                        elif has_email and has_phone:
                            lead_data['contact_preference'] = 'email_and_phone'
                        elif has_whatsapp:
                            lead_data['contact_preference'] = 'whatsapp'
                        elif has_phone:
                            lead_data['contact_preference'] = 'phone'
                        elif has_email:
                            lead_data['contact_preference'] = 'email'
                        break

    whatsapp_keywords = ['whatsapp', 'whats app']
    mentions_whatsapp = any(kw in full_conversation_user.lower() for kw in whatsapp_keywords)
    if lead_data['contact_preference'] in ('whatsapp', 'email_and_whatsapp'):
        mentions_whatsapp = True

    phone_patterns = [
        r"\+\d{1,4}[\s\-]?\d{2,4}[\s\-]?\d{3,4}[\s\-]?\d{2,4}",
        r"\+?\d{9,15}", r"\d{3}[\s\-]?\d{3}[\s\-]?\d{3,4}",
    ]
    for pattern in phone_patterns:
        phone_match = re.search(pattern, full_conversation_user)
        if phone_match:
            phone = phone_match.group(0).strip()
            clean = phone.replace('+', '').replace('-', '').replace(' ', '')
            if len(clean) >= 9:
                if mentions_whatsapp:
                    lead_data['whatsapp_number'] = phone
                    print(f"✅ WhatsApp: {phone}")
                else:
                    lead_data['phone'] = phone
                    print(f"✅ Phone: {phone}")
                break

    budget_patterns = [
        r"(\d+(?:\.\d+)?)\s*([MmKk])(?![a-zA-Z])\s*(?:\$|dollars?)?",
        r"[\$]\s*(\d+(?:\.\d+)?)\s*([MmKk](?![a-zA-Z])|million|thousand|mln|mio)?",
        r"(\d+(?:\.\d+)?)\s*(million|thousand|lakh|crore|mln|milionów|milionow|mio|millones|millionen|milioni|milhões|milhoes|miljoen|milyon)\s*(?:\$|dollars?|usd|aed|eur|pln)?",
        r"(?:budget|price|around|afford)\s*[\$]?(\d+(?:\.\d+)?)\s*([MmKk](?![a-zA-Z])|million|thousand|mln)?",
    ]
    million_units = ['mln', 'milionów', 'milionow', 'mio', 'millones', 'millionen',
                      'milioni', 'milhões', 'milhoes', 'miljoen', 'milyon']
    for pattern in budget_patterns:
        budget_match = re.search(pattern, full_conversation_user, re.IGNORECASE)
        if budget_match:
            amount = budget_match.group(1)
            unit = budget_match.group(2) if len(budget_match.groups()) > 1 and budget_match.group(2) else ''
            if unit:
                unit = unit.lower()
                if unit in ['m', 'million']: unit = 'million'
                elif unit in ['k', 'thousand']: unit = 'thousand'
            currency = ''
            low_all = full_conversation_user.lower()
            if '$' in full_conversation_user or 'dollar' in low_all or 'dólar' in low_all or 'dolar' in low_all:
                currency = 'USD'
            elif 'aed' in low_all:
                currency = 'AED'
            if unit in million_units:
                unit = 'million'
            lead_data['budget'] = f"{amount} {unit} {currency}".strip() if unit else f"{amount} {currency}".strip()
            print(f"✅ Budget: {lead_data['budget']}")
            break

    # Fallback: no explicit number was ever stated - use the highest price
    # among listings actually named in the conversation as a reasonable
    # stand-in for their budget ceiling.
    if not lead_data['budget']:
        lead_data['budget'] = infer_budget_from_discussed_listings(agency_id, conversation_history)
        if lead_data['budget']:
            lead_data['budget_inferred'] = True
            print(f"✅ Budget (inferred from discussed listings): {lead_data['budget']}")

    lead_data['timeline'], lead_data['timeline_raw'] = extract_timeline(conversation_history)
    if lead_data['timeline']:
        print(f"✅ Timeline: {timeline_label(lead_data['timeline'])}")
    return lead_data


def extract_appointment_data(agency_id, conversation_history):
    """Extracts viewing intent and ALL requested (date, time, property)
    slots. Date and time are paired following the message flow (day
    mentioned -> time mentioned pairs with that day), so multiple viewings
    in one chat are each captured correctly. Property attribution walks
    BOTH roles' messages (the AI usually names the property just before
    asking for a day, e.g. "Which day for the Boston Luxury Estate?" - the
    customer's reply is just a bare day/time), so each slot gets tagged
    with whichever listing was most recently named by either side."""
    user_msgs = [m['content'] for m in conversation_history if m['role'] == 'user']
    user_text = " ".join(user_msgs).lower()

    data = {'requested': False, 'slots': []}

    data['requested'] = any(kw in user_text for kw in BOOKING_KEYWORDS)

    if not data['requested']:
        for i, msg in enumerate(conversation_history):
            if msg['role'] == 'assistant':
                ai_lower = msg['content'].lower()
                if any(p in ai_lower for p in VIEWING_OFFER_PHRASES):
                    if i + 1 < len(conversation_history):
                        next_msg = conversation_history[i + 1]
                        if next_msg['role'] == 'user':
                            user_reply = re.sub(r'[^\w\sÀ-ÖØ-öø-ÿążćęłńóśźäöüß]', '', next_msg['content'].lower()).strip()
                            if any(user_reply == w or user_reply.startswith(w + ' ') for w in AFFIRMATIVE_WORDS):
                                data['requested'] = True
                                break

    time_patterns = [
        (r'\b10[:.]00\s*(?:am|uhr|h)?\b', '10:00 AM'),
        (r'\b(10\s*am|10\s*o\'?clock)\b', '10:00 AM'),
        (r'\b12[:.]00\s*(?:pm|uhr|h)?\b', '12:00 PM'),
        (r'\b(12\s*pm|noon|12\s*o\'?clock)\b', '12:00 PM'),
        (r'\b2[:.]00\s*pm\b', '2:00 PM'),
        (r'\b14[:.]00\s*(?:uhr|h)?\b', '2:00 PM'),
        (r'\b(2\s*pm|2\s*o\'?clock)\b', '2:00 PM'),
        (r'\b4[:.]00\s*pm\b', '4:00 PM'),
        (r'\b16[:.]00\s*(?:uhr|h)?\b', '4:00 PM'),
        (r'\b(4\s*pm|4\s*o\'?clock)\b', '4:00 PM'),
        (r'\b6[:.]00\s*pm\b', '6:00 PM'),
        (r'\b18[:.]00\s*(?:uhr|h)?\b', '6:00 PM'),
        (r'\b(6\s*pm|6\s*o\'?clock)\b', '6:00 PM'),
        (r'\bmorning\b', '10:00 AM'),
        (r'\b(afternoon|midday)\b', '2:00 PM'),
        (r'\b(evening|late afternoon)\b', '4:00 PM'),
    ]

    def find_all_times(text):
        """ALL times mentioned in a message, in order - needed to pair
        'two times in one message' with two pending days (e.g.
        '4:00 PM and 6:00 PM')."""
        matches = []
        for pattern, label in time_patterns:
            for m in re.finditer(pattern, text):
                matches.append((m.start(), label))
        matches.sort(key=lambda x: x[0])
        result, seen_pos = [], set()
        for pos, label in matches:
            if pos in seen_pos:
                continue
            seen_pos.add(pos)
            result.append(label)
        return result

    def find_days_in_message(text):
        """ALL days mentioned in a single message, in order. Prefers exact
        calendar dates (most specific, e.g. 'August 17') when present;
        falls back to weekday-name mentions (ALL of them, not just the
        last) only when no specific date is given in that message."""
        dates = find_all_dates_in_text(text)
        if dates:
            return dates
        positions = []
        for word, normalized in WEEKDAY_WORDS.items():
            for m in re.finditer(r'\b' + re.escape(word) + r'\b', text):
                positions.append((m.start(), normalized))
        positions.sort(key=lambda x: x[0])
        result, seen_iso = [], set()
        for pos, day_word in positions:
            resolved = resolve_next_date(day_word)
            if resolved and resolved['iso'] not in seen_iso:
                seen_iso.add(resolved['iso'])
                result.append(resolved)
        return result

    # FIFO queue of resolved days awaiting a time. Handles:
    #  - normal 1 day -> 1 time (classic single booking)
    #  - N days mentioned together -> N times given later in one message
    #    (paired in order, e.g. "4:00 PM and 6:00 PM")
    #  - N days mentioned together -> a SINGLE time given later, meaning
    #    that time applies to ALL of them (e.g. customer replies just
    #    "2PM" intending it for both viewings - matches what the AI itself
    #    confirms back to the customer)
    pending_days = []
    pending_property = None
    for msg in conversation_history:
        # Property mentions can come from EITHER side - the AI almost
        # always names the property right before asking for a day.
        titles_here = detect_listing_titles_in_text(agency_id, msg['content'])
        if titles_here:
            pending_property = titles_here[-1]

        if msg['role'] != 'user':
            continue

        text = msg['content'].lower()
        days_here = find_days_in_message(text)
        times_here = find_all_times(text)

        for d in days_here:
            if not any(q['iso'] == d['iso'] for q in pending_days):
                pending_days.append(d)

        if times_here and pending_days:
            if len(times_here) == 1 and len(pending_days) > 1:
                # One time given for multiple pending days - apply to all
                t = times_here[0]
                for d in pending_days:
                    if not any(s['iso'] == d['iso'] and s['time'] == t for s in data['slots']):
                        data['slots'].append({'iso': d['iso'], 'display': d['display'], 'time': t, 'property': pending_property})
                pending_days = []
            else:
                pair_count = min(len(times_here), len(pending_days))
                for i in range(pair_count):
                    d, t = pending_days[i], times_here[i]
                    if not any(s['iso'] == d['iso'] and s['time'] == t for s in data['slots']):
                        data['slots'].append({'iso': d['iso'], 'display': d['display'], 'time': t, 'property': pending_property})
                pending_days = pending_days[pair_count:]
    return data


def contact_step_completed(conversation_history):
    asked_contact_pref = False
    asked_number = False
    gave_number = False
    user_said_email_only = False
    user_declined_number = False

    email_words = ['email', 'e-mail', 'mail', 'correo']
    phone_words = ['phone', 'call', 'telefon', 'teléfono', 'telefono', 'téléphone', 'telefone']

    for i, msg in enumerate(conversation_history):
        if msg['role'] == 'assistant':
            if is_contact_question(msg['content']):
                asked_contact_pref = True
                if i + 1 < len(conversation_history):
                    next_msg = conversation_history[i + 1]
                    if next_msg['role'] == 'user':
                        user_text = next_msg['content'].lower()
                        has_email_word = any(w in user_text for w in email_words)
                        has_wa = 'whatsapp' in user_text
                        has_phone_word = any(w in user_text for w in phone_words)
                        if has_email_word and not has_wa and not has_phone_word:
                            user_said_email_only = True
            if is_number_question(msg['content']):
                asked_number = True
                if i + 1 < len(conversation_history):
                    next_msg = conversation_history[i + 1]
                    if next_msg['role'] == 'user':
                        user_text = next_msg['content'].strip()
                        if re.search(r'\+?\d{9,15}', user_text.replace(' ', '').replace('-', '')):
                            gave_number = True
                        decline_words = ['no', 'nope', 'skip', 'pass', 'later', 'not now', "don't", 'prefer not',
                                          'nein', 'nie', 'non', 'não', 'nao', 'hayır', 'hayir', 'nahi', 'nahin']
                        if any(w in user_text.lower() for w in decline_words):
                            user_declined_number = True

    if user_said_email_only: return True
    if asked_number and (gave_number or user_declined_number): return True
    user_msg_count = len([m for m in conversation_history if m['role'] == 'user'])
    if asked_contact_pref and user_msg_count >= 10: return True
    return False


def detect_objection(user_message):
    user_message_lower = user_message.lower()

    if re.search(r'\d', user_message):
        return None

    objections = {
        'price': ['expensive', 'too much', 'costly', "can't afford", 'cannot afford', 'high price', 'over budget', 'out of my budget'],
        'timing': ['not ready', 'not sure', 'need time', 'thinking about it', 'maybe later', 'unsure'],
        'indecision': ['torn', 'confused', 'cant decide', "can't decide"],
        'trust': ['scam', 'legit', 'is this real', 'can i trust', 'safe', 'reliable']
    }
    for objection_type, keywords in objections.items():
        if any(keyword in user_message_lower for keyword in keywords):
            return objection_type
    return None


def generate_objection_response(objection_type, agency_name):
    responses = {
        'price': "I hear you – budget is key. Even a rough range helps me point you in the right direction. What feels comfortable for you?",
        'timing': "Totally fair! No pressure at all. What's the main thing making you hesitant right now?",
        'indecision': "I get that – it's a big decision! Let's try this: if you had to pick just ONE thing that matters most to you, what would it be?",
        'trust': f"I understand the concern. {agency_name} is a licensed real estate agency. Would you like to know more about us, or would you prefer to just explore properties for now?"
    }
    return responses.get(objection_type, None)


# ─────────────────────────────────────────────────────
# TIMELINE - when they actually intend to move
# ─────────────────────────────────────────────────────
# Moaz's Sept 20 note: the bot was collecting name, budget, contact and
# property details but never asking WHEN. An agent's afternoon is worth
# more on a buyer moving in three weeks than on one browsing for next
# year, and nothing in the old score could tell those two apart.

TIMELINE_LABELS = {
    'immediate':   'Immediately / within a month',
    '1_3_months':  '1-3 months',
    '3_6_months':  '3-6 months',
    '6_12_months': '6-12 months',
    'over_1_year': 'More than a year',
    'browsing':    'Just browsing, no date',
}

# How much each bucket is worth in the quality score. "Just browsing" is
# deliberately worth the same as "no answer": both mean an agent cannot
# plan around this person yet.
TIMELINE_POINTS = {
    'immediate': 3, '1_3_months': 2, '3_6_months': 1,
    '6_12_months': 1, 'over_1_year': 0, 'browsing': 0,
}

# Checked in order - the first hit wins, so the longer and more specific
# phrases must come before the short ones they contain.
TIMELINE_PHRASES = [
    ('browsing', ['just looking', 'just browsing', 'just curious', 'no rush',
                  'no hurry', 'not in a hurry', 'no timeline', 'no specific time',
                  'no particular time', 'just exploring', 'just researching',
                  'someday', 'some day', 'no fixed', 'whenever']),
    ('immediate', ['as soon as possible', 'asap', 'a.s.a.p', 'immediately',
                   'right away', 'straight away', 'right now', 'this week',
                   'next week', 'within a week', 'within days', 'urgently',
                   'very urgent', 'urgent', 'this month', 'within a month',
                   'within the month', 'end of the month', 'ready now',
                   'ready to move now', 'yesterday']),
    ('1_3_months', ['next month', 'couple of months', 'a couple months',
                    'few months', 'a few months', 'next quarter',
                    'within three months', 'within 3 months', 'by the summer']),
    ('3_6_months', ['half a year', 'within six months', 'within 6 months',
                    'end of the year', 'end of this year', 'by year end',
                    'by the end of the year']),
    ('6_12_months', ['within a year', 'within the year', 'in a year',
                     'next year', 'sometime next year']),
    ('over_1_year', ['couple of years', 'a couple years', 'few years',
                     'a few years', 'two years', 'long term', 'long-term',
                     'not for a while', 'not any time soon', 'not anytime soon']),
]

_TIMELINE_NUMBER_WORDS = {
    'a': 1, 'an': 1, 'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
    'six': 6, 'seven': 7, 'eight': 8, 'nine': 9, 'ten': 10, 'eleven': 11,
    'twelve': 12, 'eighteen': 18, 'twenty': 20, 'twenty four': 24,
}


def _months_to_bucket(months):
    if months is None:
        return None
    if months <= 1:
        return 'immediate'
    if months <= 3:
        return '1_3_months'
    if months <= 6:
        return '3_6_months'
    if months <= 12:
        return '6_12_months'
    return 'over_1_year'


def _timeline_from_numbers(text):
    """'in 2 months', 'within 6-8 weeks', 'about a year and a half'.

    A range is bucketed on its FAR end on purpose - '3 to 6 months' is a
    lead who might still be looking in six months, and an agent planning
    their week should be told the honest later date, not the hopeful one."""
    best = None
    pattern = (r'(\d{1,2}|' + '|'.join(sorted(_TIMELINE_NUMBER_WORDS, key=len, reverse=True)) + r')'
               r'(?:\s*(?:-|to|or|and)\s*(\d{1,2}))?'
               r'\s*(day|days|week|weeks|month|months|year|years)\b')
    for m in re.finditer(pattern, text):
        low_raw, high_raw, unit = m.group(1), m.group(2), m.group(3)
        try:
            low = int(low_raw)
        except ValueError:
            low = _TIMELINE_NUMBER_WORDS.get(low_raw)
        if low is None:
            continue
        value = int(high_raw) if high_raw else low
        if unit.startswith('day'):
            months = value / 30.0
        elif unit.startswith('week'):
            months = value / 4.0
        elif unit.startswith('month'):
            months = float(value)
        else:
            months = value * 12.0
        # A conversation can mention several durations ("6 month lease",
        # "moving in 2 months"). The soonest one is the commitment.
        if best is None or months < best:
            best = months
    return _months_to_bucket(best)


def extract_timeline(conversation_history):
    """Returns (bucket, what_they_actually_said).

    The answer to the timeline question is read first and on its own -
    that reply is unambiguous. Only if the bot never asked (older
    conversations, or the customer volunteered it early) does this fall
    back to scanning everything the customer said, where 'this week' might
    belong to something else entirely."""
    def classify(text):
        low = (text or '').lower()
        for bucket, phrases in TIMELINE_PHRASES:
            for phrase in phrases:
                if phrase in low:
                    return bucket
        return _timeline_from_numbers(low)

    for i, msg in enumerate(conversation_history):
        if msg['role'] != 'assistant' or not is_timeline_question(msg['content']):
            continue
        if i + 1 >= len(conversation_history):
            continue
        reply = conversation_history[i + 1]
        if reply['role'] != 'user':
            continue
        bucket = classify(reply['content'])
        if bucket:
            return bucket, reply['content'].strip()[:200]

    user_text = " ".join(m['content'] for m in conversation_history if m['role'] == 'user')
    bucket = classify(user_text)
    if bucket:
        return bucket, None
    return None, None


def timeline_label(bucket):
    return TIMELINE_LABELS.get(bucket or '', 'Not given')


# Every lead table shows the timeline, so make it a template global
# rather than threading it through a dozen render_template calls.
app.jinja_env.globals['timeline_label'] = timeline_label


def score_lead_quality(lead_data, conversation_history, has_booking=False,
                       lead_type='buyer', seller_property=None):
    """Score a lead 1-5 and say WHY, in words an agent can argue with.

    The old version gave a point for a name (the bot asks for it first,
    so everyone had one), a point for a budget, a point for a phone, and
    a bonus if the word "month" or "week" appeared anywhere the customer
    typed - which fired on "I signed a 12 month lease" as readily as on
    "I need to move next month". Timeline, the thing an agent actually
    plans around, was never asked and so never really counted.

    Now each signal is weighted by how much it predicts a deal, and the
    reasons are stored alongside the score so the dashboard can show the
    working instead of an unexplained number of stars."""
    reasons = []
    points = 0

    # ── Reachability. An email is the minimum; a number is what an agent
    #    actually uses on the day they want to close something.
    if lead_data.get('email'):
        points += 1
        reasons.append(('+1', 'Email address given'))
    else:
        reasons.append(('0', 'No email address'))
    if lead_data.get('whatsapp_number') or lead_data.get('phone'):
        points += 2
        reasons.append(('+2', 'Phone or WhatsApp number given'))
    else:
        reasons.append(('0', 'No phone or WhatsApp number'))

    # ── Money. Said out loud beats inferred from what they browsed.
    money_word = 'Asking price' if lead_type == 'seller' else 'Budget'
    if lead_data.get('budget'):
        if lead_data.get('budget_inferred'):
            points += 1
            reasons.append(('+1', f'{money_word} inferred from the properties discussed'))
        else:
            points += 2
            reasons.append(('+2', f'{money_word} stated: {lead_data["budget"]}'))
    else:
        reasons.append(('0', f'No {money_word.lower()} given'))

    # ── Timeline. The signal that was missing entirely.
    bucket = lead_data.get('timeline')
    if bucket:
        earned = TIMELINE_POINTS.get(bucket, 0)
        points += earned
        reasons.append((f'+{earned}', f'Timeline: {timeline_label(bucket)}'))
    else:
        reasons.append(('0', 'Timeline: not given'))

    # ── Commitment. A buyer who booked a viewing has put their own time
    #    on the line; a seller's equivalent is a property we could list.
    if lead_type == 'seller':
        prop = seller_property or {}
        if prop.get('location') and prop.get('price_raw'):
            points += 2
            reasons.append(('+2', 'Property described in full (location and price)'))
        elif prop.get('location') or prop.get('price_raw'):
            points += 1
            reasons.append(('+1', 'Property partly described'))
        else:
            reasons.append(('0', 'Property details incomplete'))
    elif has_booking:
        points += 2
        reasons.append(('+2', 'Booked a viewing'))
    else:
        reasons.append(('0', 'No viewing booked'))

    # ── Engagement. Someone still answering after eight turns is invested.
    user_msgs = len([m for m in conversation_history if m['role'] == 'user'])
    if user_msgs >= 8:
        points += 1
        reasons.append(('+1', f'Engaged conversation ({user_msgs} messages)'))
    else:
        reasons.append(('0', f'Short conversation ({user_msgs} messages)'))

    # 11 points available. The thresholds are set so that a 5 has to be
    # earned on several fronts at once - contactable AND funded AND soon
    # AND committed - rather than by filling in a form.
    if points >= 9:
        score = 5
    elif points >= 7:
        score = 4
    elif points >= 5:
        score = 3
    elif points >= 3:
        score = 2
    else:
        score = 1

    print(f"📊 Quality {score}/5 ({points}/11 pts): "
          + ", ".join(f"{pts} {why}" for pts, why in reasons))
    return score, reasons


def analyze_lead_quality(lead_data, conversation_history):
    """Back-compatible wrapper - returns just the star rating."""
    return score_lead_quality(lead_data, conversation_history)[0]


def is_lead_qualified(lead_data, conversation_history, has_booking=False):
    has_email = bool(lead_data.get('email'))
    has_name = bool(lead_data.get('name'))
    has_budget = bool(lead_data.get('budget'))
    message_count = len([msg for msg in conversation_history if msg['role'] == 'user'])
    contact_done = contact_step_completed(conversation_history)
    is_qualified = (has_email and has_name and has_budget and message_count >= 7 and contact_done)
    # Alternate path: a booked viewing with name+email is inherently qualified
    if not is_qualified and has_booking and has_email and has_name:
        is_qualified = True
        print("✅ QUALIFIED via booking path")
    if is_qualified:
        print(f"✅ QUALIFIED: Email={has_email}, Name={has_name}, Budget={has_budget}, Msgs={message_count}, ContactDone={contact_done}")
    else:
        print(f"⚠️ Not yet: Email={has_email}, Name={has_name}, Budget={has_budget}, Msgs={message_count}/7, ContactDone={contact_done}")
    return is_qualified


# -------------------------
# DATABASE MODELS
# -------------------------
class Agency(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    prompt = db.Column(db.Text)
    assistant_name = db.Column(db.String(100), default="AI Assistant")
    owner_name = db.Column(db.String(100))
    email = db.Column(db.String(150), nullable=False)
    whatsapp = db.Column(db.String(50))
    password_hash = db.Column(db.String(200))
    subscription_type = db.Column(db.String(50))
    status = db.Column(db.String(50), default="Active")
    webhook_url = db.Column(db.String(500))
    max_viewings_per_slot = db.Column(db.Integer, default=2)
    # ── Tier & Paddle billing (Step 4A) ──
    tier = db.Column(db.String(20), default='solo')
    parent_id = db.Column(db.Integer, nullable=True)          # branch → HQ agency id
    paddle_customer_id = db.Column(db.String(100), nullable=True)
    paddle_subscription_id = db.Column(db.String(100), nullable=True)
    subscription_status = db.Column(db.String(20), default='active')
    trial_ends_at = db.Column(db.DateTime, nullable=True)
    billing_email = db.Column(db.String(150), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    reset_token = db.Column(db.String(100), nullable=True)
    reset_token_expires = db.Column(db.DateTime, nullable=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Lead(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(100))
    email = db.Column(db.String(100))
    phone = db.Column(db.String(50))
    whatsapp_number = db.Column(db.String(50))
    contact_preference = db.Column(db.String(20), default='email')
    budget = db.Column(db.String(50))
    message = db.Column(db.Text)
    intent_score = db.Column(db.Integer, default=1)
    lead_status = db.Column(db.String(20), default='new')
    notes = db.Column(db.Text, default='[]')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(pytz.timezone('Asia/Karachi')))
    follow_up_1_sent = db.Column(db.Integer, default=0)
    follow_up_7_sent = db.Column(db.Integer, default=0)
    agent_id = db.Column(db.Integer, nullable=True)   # assigned agent (Tier 2/3)
    # 'buyer' (someone looking for a property) or 'seller' (an owner
    # listing one). Two completely different jobs for an agent, so the
    # dashboard keeps them visibly apart.
    lead_type = db.Column(db.String(20), default='buyer')
    # When they intend to act. Bucket key from TIMELINE_LABELS, plus the
    # sentence they actually said so an agent reads it in their words.
    timeline = db.Column(db.String(40), nullable=True)
    timeline_raw = db.Column(db.String(200), nullable=True)
    # JSON list of [points, reason] - the working behind intent_score, so
    # "why is this a 3?" has an answer on the lead card.
    quality_reasons = db.Column(db.Text, nullable=True)

class ActivityEvent(db.Model):
    """One line in the agency's shared feed.

    Moaz's Sept 20 note: "whenever an agent or agency owner updates a
    status, all relevant parties are notified... there should also be a
    mechanism to track these updates directly on the dashboards."

    Email alone is a notification you can miss; a feed alone is one you
    have to remember to check. Every status change, outcome and note
    writes a row here AND emails the other side, and each side carries
    its own seen flag so the owner reading the feed doesn't mark it read
    for the agent."""
    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer, nullable=False, index=True)
    # The agent this event concerns - not necessarily the one who acted.
    # An owner closing an agent's lead is an event *for* that agent.
    agent_id = db.Column(db.Integer, nullable=True, index=True)
    actor_type = db.Column(db.String(20))     # owner | agent | customer | system
    actor_name = db.Column(db.String(120))
    action = db.Column(db.String(40))         # lead_status | appointment_stage | note | ...
    summary = db.Column(db.String(400))       # the sentence shown in the feed
    subject_type = db.Column(db.String(20))   # lead | appointment | listing
    subject_id = db.Column(db.Integer, nullable=True)
    subject_name = db.Column(db.String(150))  # the customer, for the feed line
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(pytz.timezone('Asia/Karachi')))
    seen_by_owner = db.Column(db.Integer, default=0)
    seen_by_agent = db.Column(db.Integer, default=0)


class Appointment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer, nullable=False)
    lead_id = db.Column(db.Integer, nullable=True)
    agent_id = db.Column(db.Integer, nullable=True)   # whose calendar (Tier 2/3)
    customer_name = db.Column(db.String(100))
    customer_email = db.Column(db.String(150))
    appointment_date = db.Column(db.String(100))       # Display: "Monday, July 13, 2026"
    appointment_date_iso = db.Column(db.String(20))    # Query: "2026-07-13"
    appointment_time = db.Column(db.String(50))
    property_interest = db.Column(db.String(200))
    status = db.Column(db.String(20), default='pending')
    notes = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(pytz.timezone('Asia/Karachi')))
    # ── Post-appointment loop (Item 6) ──
    # outcome: None (no answer yet) / 'wants_to_buy' / 'wants_other_options' / 'not_interested'
    outcome = db.Column(db.String(30), nullable=True)
    outcome_source = db.Column(db.String(20), nullable=True)   # 'customer' / 'owner' / 'agent'
    outcome_at = db.Column(db.DateTime, nullable=True)
    checkin_token = db.Column(db.String(100), nullable=True)   # customer-facing feedback link
    checkin_sent_at = db.Column(db.DateTime, nullable=True)


class Listing(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(200), nullable=False)
    location = db.Column(db.String(200))
    price_raw = db.Column(db.String(100))
    price = db.Column(db.Float, nullable=True)
    price_numeric = db.Column(db.Float, nullable=True)
    bedrooms = db.Column(db.Integer, nullable=True)
    bathrooms = db.Column(db.Float, nullable=True)   # supports half-baths e.g. 4.5
    property_type = db.Column(db.String(50))
    listing_purpose = db.Column(db.String(10), default='sale')  # 'sale' or 'rent'
    features = db.Column(db.String(500))
    description = db.Column(db.Text)
    # 'available' (live, offered to buyers), 'pending' (submitted by a
    # seller, awaiting the agency's approval), 'rejected', or 'sold'.
    # Only 'available' is ever shown to a customer.
    status = db.Column(db.String(20), default='available')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(pytz.timezone('Asia/Karachi')))
    # Where this listing came from: 'agency' (uploaded/added by staff) or
    # 'seller_chat' (a property owner described it to the AI).
    source = db.Column(db.String(20), default='agency')
    seller_lead_id = db.Column(db.Integer, nullable=True)


class ConversationSession(db.Model):
    session_key = db.Column(db.String(120), primary_key=True)
    history = db.Column(db.Text, default='[]')
    booked_slots = db.Column(db.Text, default='[]')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class Agent(db.Model):
    """Sub-accounts for Tier 2 (agency) and Tier 3 (corporation branches)."""
    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer, nullable=False)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(150), nullable=False)
    password_hash = db.Column(db.String(200))
    status = db.Column(db.String(20), default='active')   # active / disabled
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    reset_token = db.Column(db.String(100), nullable=True)
    reset_token_expires = db.Column(db.DateTime, nullable=True)
    # The area(s) this agent covers, e.g. "Miami" or "Miami, Orlando".
    # Leads and viewings are matched against it before falling back to
    # round-robin, so a local agent gets the local property.
    location = db.Column(db.String(200), nullable=True)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)
    
# -------------------------
# ROUTES
# -------------------------

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/signup")
def signup():
    return render_template("signup.html", show_tier_3=SHOW_TIER_3)

@app.route("/signup/solo")
def signup_solo():
    return render_template("signup_solo.html")

@app.route("/signup/agency")
def signup_agency():
    return render_template("signup_agency.html")

@app.route("/owner-login", methods=["GET", "POST"])
@limiter.limit(LOGIN_LIMIT, methods=["POST"])
def owner_login():
    if request.method == "GET":
        return render_template("owner_login.html")
    agency_id = request.form.get("agency_id", "").strip()
    password = request.form.get("password", "").strip()
    if not agency_id or not password:
        return redirect("/owner-login?error=Missing+credentials")
    try:
        agency = db.session.get(Agency, int(agency_id))
    except:
        return redirect("/owner-login?error=Invalid+Agency+ID")
    if not agency:
        return redirect("/owner-login?error=Agency+not+found")
    if agency.password_hash and agency.check_password(password):
        session['agency_id'] = str(agency_id)
        return redirect(f"/admin?agency_id={agency_id}")
    else:
        return redirect("/owner-login?error=Invalid+password")

@app.route("/owner-logout")
def owner_logout():
    session.pop('agency_id', None)
    return redirect("/owner-login")

@app.route("/admin")
def admin():
    agency_id = request.args.get("agency_id")
    if not agency_id:
        return redirect("/owner-login?error=Please+login+first")
    try:
        agency_id_int = int(agency_id)
    except ValueError:
        return redirect("/owner-login?error=Invalid+Agency+ID")
    if not _owner_owns_agency(agency_id_int):
        return redirect("/owner-login?error=Please+login+first")
    try:
        leads = Lead.query.filter_by(
            agency_id=int(agency_id)
        ).order_by(Lead.intent_score.desc(), Lead.created_at.desc()).all()
        agency = db.session.get(Agency, int(agency_id))
        if not agency:
            return redirect("/owner-login?error=Agency+not+found")
        seller_count = sum(1 for l in leads if l.lead_type == 'seller')
        pending_listings = Listing.query.filter_by(
            agency_id=int(agency_id), status='pending').count()
        return render_template("admin.html", leads=leads, agency=agency,
                               now=datetime.utcnow(), seller_count=seller_count,
                               buyer_count=len(leads) - seller_count,
                               pending_listings=pending_listings,
                               activity=recent_activity(agency_id_int),
                               activity_unseen=unseen_activity_count(agency_id_int),
                               activity_icon=activity_icon)
    except Exception as e:
        print(f"❌ ADMIN ERROR: {e}")
        return redirect("/owner-login?error=Something+went+wrong")

@app.route("/super-admin-login", methods=["GET", "POST"])
@limiter.limit(SUPER_ADMIN_LOGIN_LIMIT, methods=["POST"])
def super_admin_login():
    if request.method == "GET":
        return render_template("super_admin_login.html")
    password = request.form.get("password", "").strip()
    if SUPER_ADMIN_PASSWORD and password == SUPER_ADMIN_PASSWORD:
        session['super_admin'] = True
        return redirect("/owner")
    return redirect("/super-admin-login?error=Invalid+password")

@app.route("/super-admin-logout")
def super_admin_logout():
    session.pop('super_admin', None)
    return redirect("/super-admin-login")

@app.route("/owner")
def owner():
    if not session.get('super_admin'):
        return redirect("/super-admin-login?error=Please+login+first")
    return render_template("owner.html")

@app.route("/ping")
def ping():
    return jsonify({"status": "ok", "message": "pong"})

@app.route("/create-agency", methods=["POST", "OPTIONS"])
@limiter.limit(SIGNUP_LIMIT, methods=["POST"],
               exempt_when=lambda: bool(session.get('super_admin')))
def create_agency():
    if request.method == "OPTIONS":
        return "", 200
    data = request.json
    if not data.get("name") or not data.get("email"):
        return jsonify({"error": "Name and email required"}), 400

    tier = data.get("tier", "solo")
    if tier not in TIER_LIMITS:
        tier = "solo"

    # If the caller (a future signup form) already collected a real password,
    # use it. Otherwise generate a random one-time temporary password - never
    # a shared hardcoded default like the old "admin123".
    supplied_password = (data.get("password") or "").strip()
    temp_password = supplied_password or secrets.token_urlsafe(9)

    agency = Agency(
        name=data.get("name"),
        prompt=data.get("prompt", "You are a luxury real estate assistant."),
        assistant_name=data.get("assistant_name", "AI Assistant"),
        owner_name=data.get("owner_name"),
        email=data.get("email"),
        whatsapp=data.get("whatsapp"),
        subscription_type=data.get("subscription_type", "Basic"),
        status="Active",
        tier=tier,
        subscription_status="trialing",
        trial_ends_at=datetime.utcnow() + timedelta(days=14),
        billing_email=data.get("billing_email") or data.get("email")
    )
    agency.set_password(temp_password)
    db.session.add(agency)
    db.session.commit()
    print(f"✅ Agency created: ID {agency.id} | Tier: {tier} | Trial ends: {agency.trial_ends_at.date()}")
    return jsonify({
        "agency_id": agency.id,
        "tier": tier,
        "trial_ends": agency.trial_ends_at.strftime('%Y-%m-%d'),
        # Only handed back when we generated it ourselves - if the caller
        # supplied their own password, it already knows it.
        "temp_password": None if supplied_password else temp_password,
        "message": "Agency created"
    })

@app.route("/change-owner-password/<int:agency_id>", methods=["POST"])
def change_owner_password(agency_id):
    """Lets an agency owner (or the super admin) rotate an agency's login
    password. Used right now to get agencies off the old admin123 default."""
    if session.get('agency_id') != str(agency_id) and not session.get('super_admin'):
        return jsonify({"error": "Unauthorized"}), 401
    agency = db.session.get(Agency, agency_id)
    if not agency:
        return jsonify({"error": "Agency not found"}), 404
    data = request.get_json(force=True, silent=True) or {}
    new_password = (data.get("new_password") or "").strip()
    if len(new_password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    agency.set_password(new_password)
    db.session.commit()
    return jsonify({"message": "Password updated"})

@app.route("/paddle-webhook", methods=["POST"])
def paddle_webhook():
    """Stub: logs Paddle events. Signature verification + event handling
    will be added when the Paddle account goes live (Step 7)."""
    try:
        payload = request.get_json(force=True, silent=True) or {}
        event_type = payload.get("event_type", "unknown")
        print(f"💳 Paddle webhook received: {event_type}")
        # Future: subscription.created → set tier/status
        #         subscription.updated → change tier
        #         subscription.cancelled → subscription_status='cancelled'
        return jsonify({"status": "received"}), 200
    except Exception as e:
        print(f"⚠️ Paddle webhook error: {e}")
        return jsonify({"status": "error"}), 200

@app.route("/agencies")
def get_agencies():
    if not session.get('super_admin'):
        return jsonify({"error": "Unauthorized"}), 401
    agencies = Agency.query.order_by(Agency.created_at.desc()).all()
    now = datetime.utcnow()
    result = []
    for a in agencies:
        trial_days_left = None
        if a.trial_ends_at:
            trial_days_left = max(0, (a.trial_ends_at - now).days + 1) if a.trial_ends_at > now else 0
        result.append({
            "id": a.id, "name": a.name,
            "assistant_name": a.assistant_name or "AI Assistant",
            "owner_name": a.owner_name or "—",
            "email": a.email, "status": a.status,
            "tier": a.tier or "solo",
            "subscription_status": a.subscription_status or "active",
            "trial_ends_at": a.trial_ends_at.isoformat() if a.trial_ends_at else None,
            "trial_days_left": trial_days_left,
            "created_at": a.created_at.isoformat(),
            "lead_count": Lead.query.filter_by(agency_id=a.id).count(),
            "appointment_count": Appointment.query.filter_by(agency_id=a.id).count(),
            "agent_count": Agent.query.filter_by(agency_id=a.id).count(),
        })
    return jsonify(result)


@app.route("/platform-stats")
def platform_stats():
    """Basic, at-a-glance counts for the Super Admin panel's overview
    cards - deliberately lean per Moaz's instruction (agency list + trial/
    subscription status + basic counts), not a full analytics build-out."""
    if not session.get('super_admin'):
        return jsonify({"error": "Unauthorized"}), 401
    now = datetime.utcnow()
    active_trials = Agency.query.filter(
        Agency.subscription_status == 'trialing',
        Agency.trial_ends_at.isnot(None),
        Agency.trial_ends_at >= now,
    ).count()
    expired_trials = Agency.query.filter(
        Agency.subscription_status == 'trialing',
        Agency.trial_ends_at.isnot(None),
        Agency.trial_ends_at < now,
    ).count()
    # Only count rows that still belong to a live agency. Agencies deleted
    # before the cascade cleanup existed left orphaned agents/leads/
    # appointments behind, and counting those made the panel report more
    # agents than actually exist anywhere in the product.
    live_ids = [a.id for a in Agency.query.with_entities(Agency.id).all()]

    def live_count(model):
        if not live_ids:
            return 0
        return model.query.filter(model.agency_id.in_(live_ids)).count()

    # "Paying" means a real subscription exists - not merely the model's
    # default status string. Agencies created before the 'trialing' default
    # landed still carry subscription_status='active' and were being counted
    # as paying customers when nobody has paid yet.
    paying = Agency.query.filter(
        Agency.subscription_status == 'active',
        Agency.paddle_subscription_id.isnot(None),
        Agency.paddle_subscription_id != '',
    ).count()

    active_agents = (Agent.query.filter(Agent.agency_id.in_(live_ids),
                                        Agent.status == 'active').count()
                     if live_ids else 0)

    return jsonify({
        "total_agencies": Agency.query.count(),
        "active_trials": active_trials,
        "expired_trials": expired_trials,
        "paying_agencies": paying,
        "by_tier": {
            tier: Agency.query.filter_by(tier=tier).count()
            for tier in ('solo', 'agency', 'corporation')
        },
        "total_leads": live_count(Lead),
        "total_appointments": live_count(Appointment),
        "total_agents": live_count(Agent),
        "active_agents": active_agents,
    })

@app.route("/delete-agency/<int:agency_id>", methods=["DELETE"])
def delete_agency(agency_id):
    if not session.get('super_admin'):
        return jsonify({"error": "Unauthorized"}), 401
    agency = db.session.get(Agency, agency_id)
    if not agency:
        return jsonify({"error": "Agency not found"}), 404
    Lead.query.filter_by(agency_id=agency_id).delete()
    Appointment.query.filter_by(agency_id=agency_id).delete()
    Listing.query.filter_by(agency_id=agency_id).delete()
    Agent.query.filter_by(agency_id=agency_id).delete()
    ConversationSession.query.filter(
        ConversationSession.session_key.like(f"{agency_id}_%")
    ).delete(synchronize_session=False)
    db.session.delete(agency)
    db.session.commit()
    return jsonify({"message": "Agency deleted"})

@app.route("/agency/<int:agency_id>")
def agency_info(agency_id):
    agency = db.session.get(Agency, agency_id)
    if not agency:
        return jsonify({"error": "Invalid agency ID"}), 404
    return jsonify({"name": agency.name, "assistant": agency.assistant_name or "AI Assistant"})


# ─────────────────────────────────────────────────────
# PHASE 2B ROUTES
# ─────────────────────────────────────────────────────

def _owner_owns_agency(agency_id):
    return session.get('agency_id') == str(agency_id) or session.get('super_admin')


def _agent_in_agency(agency_id):
    """True if the logged-in agent (if any) belongs to this agency - used
    for routes both the owner dashboard and the agent dashboard call."""
    agent_id = session.get('agent_id')
    if not agent_id:
        return False
    agent = db.session.get(Agent, agent_id)
    return bool(agent and agent.agency_id == agency_id)



LEAD_STATUS_LABELS = {
    'new': 'New', 'contacted': 'Contacted', 'meeting': 'Meeting booked',
    'closed': 'Closed', 'lost': 'Lost',
}

@app.route("/update-lead-status/<int:lead_id>", methods=["POST"])
def update_lead_status(lead_id):
    try:
        lead = db.session.get(Lead, lead_id)
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        if not _owner_owns_agency(lead.agency_id):
            return jsonify({"error": "Unauthorized"}), 401
        data = request.get_json(force=True)
        new_status = data.get("status", "new")
        if new_status not in ['new', 'contacted', 'meeting', 'closed', 'lost']:
            return jsonify({"error": "Invalid status"}), 400
        previous = lead.lead_status or 'new'
        lead.lead_status = new_status
        db.session.commit()
        actor_type, actor_name, _ = acting_identity()
        record_activity(
            lead.agency_id, 'lead_status',
            f"{actor_name} moved {lead.name or 'a lead'} from "
            f"{LEAD_STATUS_LABELS.get(previous, previous)} to "
            f"{LEAD_STATUS_LABELS.get(new_status, new_status)}",
            actor_type=actor_type, actor_name=actor_name,
            subject_type='lead', subject_id=lead.id, subject_name=lead.name,
            agent_id=lead.agent_id)
        return jsonify({"success": True, "status": new_status})
    except Exception as e:
        return jsonify({"error": "Failed to update status"}), 500


@app.route("/add-lead-note/<int:lead_id>", methods=["POST"])
def add_lead_note(lead_id):
    try:
        lead = db.session.get(Lead, lead_id)
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        if not _owner_owns_agency(lead.agency_id):
            return jsonify({"error": "Unauthorized"}), 401
        data = request.get_json(force=True)
        note_text = data.get("note", "").strip()
        if not note_text:
            return jsonify({"error": "Note cannot be empty"}), 400
        try:
            notes = json.loads(lead.notes or '[]')
        except:
            notes = []
        new_note = {
            "id": len(notes) + 1,
            "text": note_text,
            "author": "Owner",
            "timestamp": datetime.now(pytz.timezone('Asia/Karachi')).strftime('%B %d, %Y at %I:%M %p')
        }
        notes.append(new_note)
        lead.notes = json.dumps(notes)
        db.session.commit()
        actor_type, actor_name, _ = acting_identity()
        record_activity(
            lead.agency_id, 'lead_note',
            f"{actor_name} added a note on {lead.name or 'a lead'}: "
            f"{note_text[:120]}",
            actor_type=actor_type, actor_name=actor_name,
            subject_type='lead', subject_id=lead.id, subject_name=lead.name,
            agent_id=lead.agent_id)
        return jsonify({"success": True, "note": new_note, "total_notes": len(notes)})
    except Exception as e:
        return jsonify({"error": "Failed to add note"}), 500


@app.route("/delete-lead-note/<int:lead_id>/<int:note_id>", methods=["DELETE"])
def delete_lead_note(lead_id, note_id):
    try:
        lead = db.session.get(Lead, lead_id)
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        if not _owner_owns_agency(lead.agency_id):
            return jsonify({"error": "Unauthorized"}), 401
        try:
            notes = json.loads(lead.notes or '[]')
        except:
            notes = []
        notes = [n for n in notes if n.get('id') != note_id]
        lead.notes = json.dumps(notes)
        db.session.commit()
        return jsonify({"success": True, "total_notes": len(notes)})
    except Exception as e:
        return jsonify({"error": "Failed to delete note"}), 500


def _quality_reasons(lead):
    """The stored working behind a lead's star rating. Leads captured
    before the score was made explainable simply have none - the card
    says so rather than inventing a justification after the fact."""
    try:
        return [{"points": pts, "reason": why}
                for pts, why in json.loads(lead.quality_reasons or '[]')]
    except Exception:
        return []


@app.route("/get-lead-detail/<int:lead_id>")
def get_lead_detail(lead_id):
    try:
        lead = db.session.get(Lead, lead_id)
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        # Called from both the owner dashboard and any agent's dashboard
        # (agents can see leads beyond their own via cross-agent visibility).
        if not (_owner_owns_agency(lead.agency_id) or _agent_in_agency(lead.agency_id)):
            return jsonify({"error": "Unauthorized"}), 401
        try:
            notes = json.loads(lead.notes or '[]')
        except:
            notes = []
        clean_num = clean_whatsapp_number(lead.whatsapp_number)
        wa_link = f"https://wa.me/{clean_num}" if clean_num else None

        agent_name = None
        if lead.agent_id:
            assigned_agent = db.session.get(Agent, lead.agent_id)
            if assigned_agent:
                agent_name = assigned_agent.name

        agency_agents = Agent.query.filter_by(agency_id=lead.agency_id).all()
        agent_names_map = {a.id: a.name for a in agency_agents}
        related = get_related_appointments(lead.agency_id, lead.email)
        related_appointments = [{
            "id": r.id,
            "agent_name": agent_names_map.get(r.agent_id, "Unassigned"),
            "date": r.appointment_date or "—",
            "time": r.appointment_time or "—",
            "property": r.property_interest or "—",
            "status": r.status,
            "notes": r.notes or ""
        } for r in related]

        return jsonify({
            "id": lead.id, "name": lead.name or "—",
            "email": lead.email or "—", "phone": lead.phone or None,
            "whatsapp_number": lead.whatsapp_number or None,
            "whatsapp_link": wa_link,
            "contact_preference": lead.contact_preference or "email",
            "budget": lead.budget or "—", "message": lead.message or "—",
            "intent_score": lead.intent_score or 1,
            "lead_type": lead.lead_type or "buyer",
            "timeline": timeline_label(lead.timeline),
            "timeline_raw": lead.timeline_raw or None,
            "quality_reasons": _quality_reasons(lead),
            "lead_status": lead.lead_status or "new", "notes": notes,
            "agent_name": agent_name,
            "related_appointments": related_appointments,
            "created_at": lead.created_at.strftime('%B %d, %Y at %I:%M %p') if lead.created_at else "—"
        })
    except Exception as e:
        return jsonify({"error": "Failed to get lead"}), 500


@app.route("/mark-activity-seen/<int:agency_id>", methods=["POST"])
def mark_activity_seen(agency_id):
    """Called when the owner opens the updates panel. Marking read is the
    owner's flag alone - the agent's unread count is untouched, so one
    person reading the feed never hides an update from the other."""
    if not _owner_owns_agency(agency_id):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        ActivityEvent.query.filter_by(agency_id=agency_id, seen_by_owner=0)\
            .update({ActivityEvent.seen_by_owner: 1}, synchronize_session=False)
        db.session.commit()
        return jsonify({"success": True})
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Failed"}), 500


@app.route("/agent-mark-activity-seen/<int:agent_id>", methods=["POST"])
def agent_mark_activity_seen(agent_id):
    if session.get('agent_id') != agent_id:
        return jsonify({"error": "Unauthorized"}), 401
    agent = db.session.get(Agent, agent_id)
    if not agent:
        return jsonify({"error": "Agent not found"}), 404
    try:
        ActivityEvent.query.filter_by(agency_id=agent.agency_id,
                                      agent_id=agent_id, seen_by_agent=0)\
            .update({ActivityEvent.seen_by_agent: 1}, synchronize_session=False)
        db.session.commit()
        return jsonify({"success": True})
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Failed"}), 500


@app.route("/bulk-delete-leads", methods=["POST"])
def bulk_delete_leads():
    try:
        if not session.get('agency_id') and not session.get('super_admin'):
            return jsonify({"error": "Unauthorized"}), 401
        data = request.get_json(force=True)
        lead_ids = data.get("lead_ids", [])
        if not lead_ids:
            return jsonify({"error": "No leads selected"}), 400
        deleted = 0
        for lead_id in lead_ids:
            lead = db.session.get(Lead, int(lead_id))
            # Only delete leads that actually belong to the caller's own
            # agency - a lead ID from a different agency is silently skipped.
            if lead and _owner_owns_agency(lead.agency_id):
                db.session.delete(lead)
                deleted += 1
        db.session.commit()
        return jsonify({"success": True, "deleted": deleted})
    except Exception as e:
        return jsonify({"error": "Failed to bulk delete"}), 500


# ─────────────────────────────────────────────────────
# PHASE 2C ROUTES - APPOINTMENTS
# ─────────────────────────────────────────────────────

@app.route("/appointments/<int:agency_id>")
def appointments(agency_id):
    if not _owner_owns_agency(agency_id):
        return redirect("/owner-login?error=Please+login+first")
    agency = db.session.get(Agency, agency_id)
    if not agency:
        return redirect("/owner-login?error=Agency+not+found")
    appts = Appointment.query.filter_by(
        agency_id=agency_id
    ).order_by(Appointment.created_at.desc()).all()
    agents = Agent.query.filter_by(agency_id=agency_id).order_by(Agent.name.asc()).all()
    agent_names = {a.id: a.name for a in agents}
    # Grouped by agent so the owner reads a per-person workload instead of
    # one undifferentiated pile. Unassigned bookings get their own group at
    # the end rather than being hidden.
    grouped = []
    for a in agents:
        mine = [ap for ap in appts if ap.agent_id == a.id]
        if mine:
            grouped.append({"agent": a, "appointments": mine})
    unassigned = [ap for ap in appts if not ap.agent_id]
    if unassigned:
        grouped.append({"agent": None, "appointments": unassigned})
    return render_template("appointments.html", agency=agency, appointments=appts,
                           agents=agents, agent_names=agent_names, grouped=grouped,
                           stage_of=appointment_stage,
                           stage_labels=APPOINTMENT_STAGE_LABELS)

@app.route("/reassign-appointment/<int:appt_id>", methods=["POST"])
def reassign_appointment(appt_id):
    try:
        appt = db.session.get(Appointment, appt_id)
        if not appt:
            return jsonify({"error": "Appointment not found"}), 404
        if not _owner_owns_agency(appt.agency_id):
            return jsonify({"error": "Unauthorized"}), 401
        data = request.get_json(force=True)
        new_agent_id = data.get("agent_id")
        if not new_agent_id:
            appt.agent_id = None
            db.session.commit()
            return jsonify({"success": True, "agent_id": None})
        agent = db.session.get(Agent, int(new_agent_id))
        if not agent or agent.agency_id != appt.agency_id:
            return jsonify({"error": "Invalid agent"}), 400
        if appt.appointment_date_iso and appt.appointment_time:
            if agent_busy_at(agent.id, appt.appointment_date_iso, appt.appointment_time) and appt.agent_id != agent.id:
                return jsonify({"error": f"{agent.name} already has a booking at that time"}), 409
        previous_agent_id = appt.agent_id
        appt.agent_id = agent.id
        db.session.commit()
        print(f"✅ Appointment {appt_id} reassigned → agent {agent.name}")
        actor_type, actor_name, _ = acting_identity()
        summary = (f"{actor_name} reassigned {appt.customer_name or 'a client'}'s viewing "
                   f"({appt.appointment_date or 'no date'}) to {agent.name}")
        record_activity(
            appt.agency_id, 'appointment_reassigned', summary,
            actor_type=actor_type, actor_name=actor_name,
            subject_type='appointment', subject_id=appt.id,
            subject_name=appt.customer_name, agent_id=agent.id)
        # The agent who just lost it needs to know as much as the one who
        # gained it - otherwise they keep preparing for a viewing that is
        # no longer theirs.
        if previous_agent_id and previous_agent_id != agent.id:
            record_activity(
                appt.agency_id, 'appointment_reassigned',
                f"{actor_name} moved {appt.customer_name or 'a client'}'s viewing "
                f"({appt.appointment_date or 'no date'}) from you to {agent.name}",
                actor_type=actor_type, actor_name=actor_name,
                subject_type='appointment', subject_id=appt.id,
                subject_name=appt.customer_name, agent_id=previous_agent_id)
        return jsonify({"success": True, "agent_id": agent.id})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Failed to reassign"}), 500

@app.route("/update-slot-capacity/<int:agency_id>", methods=["POST"])
def update_slot_capacity(agency_id):
    """Agency sets how many customers can book the same slot"""
    if not _owner_owns_agency(agency_id):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        agency = db.session.get(Agency, agency_id)
        if not agency:
            return jsonify({"error": "Agency not found"}), 404
        data = request.get_json(force=True)
        capacity = int(data.get("capacity", 2))
        if capacity < 1 or capacity > 20:
            return jsonify({"error": "Capacity must be between 1 and 20"}), 400
        agency.max_viewings_per_slot = capacity
        db.session.commit()
        print(f"✅ Slot capacity for agency {agency_id} → {capacity}")
        return jsonify({"success": True, "capacity": capacity})
    except Exception as e:
        return jsonify({"error": "Failed to update capacity"}), 500


@app.route("/book-appointment", methods=["POST"])
def book_appointment():
    """Manual booking with real date + agent-aware capacity check"""
    try:
        data = request.get_json(force=True)
        agency_id = data.get("agency_id")
        agency = db.session.get(Agency, int(agency_id))
        if not agency:
            return jsonify({"error": "Agency not found"}), 404

        date_iso = data.get("appointment_date_iso", "").strip()
        time_label = data.get("appointment_time", "").strip()

        display_date = data.get("appointment_date", "")
        if date_iso:
            try:
                d = datetime.strptime(date_iso, '%Y-%m-%d').date()
                if d.weekday() == 6:
                    return jsonify({"error": "Sundays are closed - please pick another day"}), 400
                display_date = d.strftime('%A, %B %d, %Y')
            except ValueError:
                return jsonify({"error": "Invalid date format"}), 400

        max_slot = get_slot_capacity(agency)
        if date_iso and time_label:
            booked = slot_booked_count(int(agency_id), date_iso, time_label)
            if booked >= max_slot:
                return jsonify({"error": f"This slot is full ({booked}/{max_slot} booked). Please choose another time."}), 409

        # Agent assignment (Tier 2/3)
        agent_id = None
        if (agency.tier or 'solo') != 'solo':
            requested_agent = data.get("agent_id")
            if requested_agent:
                agent = db.session.get(Agent, int(requested_agent))
                if not agent or agent.agency_id != int(agency_id):
                    return jsonify({"error": "Invalid agent"}), 400
                if date_iso and time_label and agent_busy_at(agent.id, date_iso, time_label):
                    return jsonify({"error": f"{agent.name} already has a booking at that time. Pick another agent or slot."}), 409
                agent_id = agent.id
            else:
                wanted = (data.get("property_interest") or "").strip()
                property_location = None
                if wanted:
                    listing_row = Listing.query.filter_by(
                        agency_id=int(agency_id), title=wanted).first()
                    if listing_row:
                        property_location = listing_row.location
                chosen = pick_agent_for_slot(
                    agency, date_iso, time_label, None, property_location)
                agent_id = chosen.id if chosen else None

        appt = Appointment(
            agency_id=int(agency_id),
            lead_id=data.get("lead_id"),
            agent_id=agent_id,
            customer_name=data.get("customer_name", ""),
            customer_email=data.get("customer_email", ""),
            appointment_date=display_date,
            appointment_date_iso=date_iso,
            appointment_time=time_label,
            property_interest=data.get("property_interest", ""),
            status="pending",
            notes=data.get("notes", "")
        )
        db.session.add(appt)
        db.session.commit()
        print(f"✅ Appointment booked: ID {appt.id} for {appt.customer_name} on {display_date} (agent: {agent_id})")
        send_appointment_confirmation(agency, appt)
        return jsonify({
            "success": True, "appointment_id": appt.id,
            "message": f"Appointment booked for {display_date} at {time_label}"
        })
    except Exception as e:
        print(f"❌ Book appointment error: {e}")
        db.session.rollback()
        return jsonify({"error": "Failed to book appointment"}), 500


@app.route("/update-appointment-status/<int:appt_id>", methods=["POST"])
def update_appointment_status(appt_id):
    try:
        appt = db.session.get(Appointment, appt_id)
        if not appt:
            return jsonify({"error": "Appointment not found"}), 404
        if not _owner_owns_agency(appt.agency_id):
            return jsonify({"error": "Unauthorized"}), 401
        data = request.get_json(force=True)
        # Accepts the unified stage list now - scheduling states AND
        # post-viewing results come through this one endpoint.
        new_status = data.get("stage") or data.get("status") or "pending"
        if not apply_appointment_stage(appt, new_status, "owner"):
            return jsonify({"error": "Invalid status"}), 400
        actor_type, actor_name, _ = acting_identity()
        record_activity(
            appt.agency_id, 'appointment_stage',
            f"{actor_name} set {appt.customer_name or 'a client'}'s viewing "
            f"({appt.appointment_date or 'no date'}) to "
            f"{APPOINTMENT_STAGE_LABELS.get(new_status, new_status)}",
            actor_type=actor_type, actor_name=actor_name,
            subject_type='appointment', subject_id=appt.id,
            subject_name=appt.customer_name, agent_id=appt.agent_id)
        return jsonify({"success": True, "status": new_status,
                        "stage": appointment_stage(appt)})
    except Exception as e:
        return jsonify({"error": "Failed to update"}), 500


@app.route("/delete-appointment/<int:appt_id>", methods=["DELETE"])
def delete_appointment(appt_id):
    try:
        appt = db.session.get(Appointment, appt_id)
        if not appt:
            return jsonify({"error": "Appointment not found"}), 404
        if not _owner_owns_agency(appt.agency_id):
            return jsonify({"error": "Unauthorized"}), 401
        db.session.delete(appt)
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": "Failed to delete"}), 500


@app.route("/get-appointments-count/<int:agency_id>")
def get_appointments_count(agency_id):
    if not _owner_owns_agency(agency_id):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        total = Appointment.query.filter_by(agency_id=agency_id).count()
        pending = Appointment.query.filter_by(agency_id=agency_id, status='pending').count()
        confirmed = Appointment.query.filter_by(agency_id=agency_id, status='confirmed').count()
        return jsonify({"total": total, "pending": pending, "confirmed": confirmed})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─────────────────────────────────────────────────────
# STEP 4B - AGENT MANAGEMENT (Tier 2/3)
# ─────────────────────────────────────────────────────

@app.route("/agents/<int:agency_id>")
def agents_page(agency_id):
    if not _owner_owns_agency(agency_id):
        return redirect("/owner-login?error=Please+login+first")
    agency = db.session.get(Agency, agency_id)
    if not agency:
        return redirect("/owner-login?error=Agency+not+found")
    if (agency.tier or 'solo') == 'solo':
        return redirect(f"/admin?agency_id={agency_id}")
    agents = Agent.query.filter_by(agency_id=agency_id).order_by(Agent.created_at.asc()).all()
    lead_counts = {a.id: Lead.query.filter_by(agency_id=agency_id, agent_id=a.id).count() for a in agents}
    appt_counts = {a.id: Appointment.query.filter(
        Appointment.agency_id == agency_id,
        Appointment.agent_id == a.id,
        Appointment.status != 'cancelled').count() for a in agents}
    limits = get_tier_limits(agency)
    return render_template("agents.html", agency=agency, agents=agents,
                           lead_counts=lead_counts, appt_counts=appt_counts,
                           limits=limits)


@app.route("/agent-detail/<int:agent_id>")
def agent_detail(agent_id):
    """Everything assigned to one agent, for the owner: their leads and
    their viewings in one place. Clicking an agent used to show nothing."""
    agent = db.session.get(Agent, agent_id)
    if not agent:
        return redirect("/owner-login?error=Agent+not+found")
    if not _owner_owns_agency(agent.agency_id):
        return redirect("/owner-login?error=Please+login+first")
    agency = db.session.get(Agency, agent.agency_id)
    leads = Lead.query.filter_by(agency_id=agent.agency_id, agent_id=agent_id)\
        .order_by(Lead.intent_score.desc(), Lead.created_at.desc()).all()
    appts = Appointment.query.filter_by(agency_id=agent.agency_id, agent_id=agent_id)\
        .order_by(Appointment.created_at.desc()).all()
    return render_template("agent_detail.html", agency=agency, agent=agent,
                           leads=leads, appointments=appts,
                           stage_of=appointment_stage,
                           stage_labels=APPOINTMENT_STAGE_LABELS)


@app.route("/add-agent/<int:agency_id>", methods=["POST"])
def add_agent(agency_id):
    if not _owner_owns_agency(agency_id):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        agency = db.session.get(Agency, agency_id)
        if not agency:
            return jsonify({"error": "Agency not found"}), 404
        limits = get_tier_limits(agency)
        current = Agent.query.filter_by(agency_id=agency_id).count()
        if current >= limits['agents']:
            return jsonify({"error": f"Agent limit reached ({limits['agents']} for {limits['label']} plan). Upgrade to add more."}), 403
        data = request.get_json(force=True)
        name = data.get("name", "").strip()
        email = data.get("email", "").strip().lower()
        # No more shared hardcoded "agent123" default - generate a random
        # one-time temp password unless the owner supplied one.
        password = data.get("password", "").strip() or secrets.token_urlsafe(9)
        if not name or not email:
            return jsonify({"error": "Name and email required"}), 400
        if Agent.query.filter_by(agency_id=agency_id, email=email).first():
            return jsonify({"error": "An agent with this email already exists"}), 400
        agent = Agent(agency_id=agency_id, name=name, email=email, status='active',
                       location=(data.get("location") or "").strip() or None)
        agent.set_password(password)
        db.session.add(agent)
        db.session.commit()
        print(f"✅ Agent added: {name} (ID {agent.id}) for agency {agency_id}")
        return jsonify({"success": True, "agent_id": agent.id, "default_password": password})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Failed to add agent"}), 500


@app.route("/toggle-agent/<int:agent_id>", methods=["POST"])
def toggle_agent(agent_id):
    try:
        agent = db.session.get(Agent, agent_id)
        if not agent:
            return jsonify({"error": "Agent not found"}), 404
        if not _owner_owns_agency(agent.agency_id):
            return jsonify({"error": "Unauthorized"}), 401
        agent.status = 'disabled' if agent.status == 'active' else 'active'
        db.session.commit()
        return jsonify({"success": True, "status": agent.status})
    except Exception:
        return jsonify({"error": "Failed to update"}), 500


@app.route("/delete-agent/<int:agent_id>", methods=["DELETE"])
def delete_agent(agent_id):
    try:
        agent = db.session.get(Agent, agent_id)
        if not agent:
            return jsonify({"error": "Agent not found"}), 404
        if not _owner_owns_agency(agent.agency_id):
            return jsonify({"error": "Unauthorized"}), 401
        # Unassign their leads (leads stay with the agency)
        Lead.query.filter_by(agent_id=agent_id).update({"agent_id": None})
        Appointment.query.filter_by(agent_id=agent_id).update({"agent_id": None})
        db.session.delete(agent)
        db.session.commit()
        return jsonify({"success": True})
    except Exception:
        db.session.rollback()
        return jsonify({"error": "Failed to delete"}), 500


@app.route("/agent-login", methods=["GET", "POST"])
@limiter.limit(LOGIN_LIMIT, methods=["POST"])
def agent_login():
    if request.method == "GET":
        return render_template("agent_login.html")
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "").strip()
    agent = Agent.query.filter_by(email=email, status='active').first()
    if agent and agent.check_password(password):
        session['agent_id'] = agent.id
        return redirect(f"/agent-dashboard/{agent.id}")
    return redirect("/agent-login?error=Invalid+credentials")


@app.route("/agent-logout")
def agent_logout():
    session.pop('agent_id', None)
    return redirect("/agent-login")


@app.route("/agent-dashboard/<int:agent_id>")
def agent_dashboard(agent_id):
    if session.get('agent_id') != agent_id:
        return redirect("/agent-login?error=Please+login+first")
    agent = db.session.get(Agent, agent_id)
    if not agent:
        return redirect("/agent-login?error=Agent+not+found")
    agency = db.session.get(Agency, agent.agency_id)
    my_leads = Lead.query.filter_by(agency_id=agent.agency_id, agent_id=agent_id)\
        .order_by(Lead.intent_score.desc(), Lead.created_at.desc()).all()
    my_appts = Appointment.query.filter_by(agency_id=agent.agency_id, agent_id=agent_id)\
        .order_by(Appointment.created_at.desc()).all()
    all_agents = Agent.query.filter_by(agency_id=agent.agency_id).all()
    agent_names = {a.id: a.name for a in all_agents}
    # Cross-agent visibility: for each of my leads, show ALL appointments tied
    # to that customer's email agency-wide, so I see what other agents did too.
    related_by_lead = {lead.id: get_related_appointments(agent.agency_id, lead.email) for lead in my_leads}

    # Parse each lead's notes server-side so the template can render them
    # directly (with author attribution) without a separate AJAX call.
    leads_notes = {}
    for lead in my_leads:
        try:
            leads_notes[lead.id] = json.loads(lead.notes or '[]')
        except Exception:
            leads_notes[lead.id] = []

    # "I'm handling a viewing for someone else's lead" awareness: for each
    # of MY appointments, check if the customer is also a lead owned by a
    # DIFFERENT agent, so that context - AND the owner's/lead-owner's notes -
    # surfaces right on my appointment card, even though that lead never
    # appears on my own dashboard.
    all_agency_leads = Lead.query.filter_by(agency_id=agent.agency_id).all()
    leads_by_email = {l.email.strip().lower(): l for l in all_agency_leads if l.email}
    appt_lead_owner = {}
    appt_owner_lead_notes = {}
    for appt in my_appts:
        if appt.customer_email:
            owning_lead = leads_by_email.get(appt.customer_email.strip().lower())
            if owning_lead and owning_lead.agent_id and owning_lead.agent_id != agent.id:
                owner_agent = db.session.get(Agent, owning_lead.agent_id)
                if owner_agent:
                    appt_lead_owner[appt.id] = owner_agent.name
                try:
                    appt_owner_lead_notes[appt.id] = json.loads(owning_lead.notes or '[]')
                except Exception:
                    appt_owner_lead_notes[appt.id] = []

    return render_template("agent_dashboard.html", agent=agent, agency=agency,
                           leads=my_leads, appointments=my_appts,
                           agent_names=agent_names, related_by_lead=related_by_lead,
                           leads_notes=leads_notes, appt_lead_owner=appt_lead_owner,
                           appt_owner_lead_notes=appt_owner_lead_notes,
                           stage_of=appointment_stage,
                           stage_labels=APPOINTMENT_STAGE_LABELS,
                           time_slots=TIME_SLOTS,
                           activity=recent_activity(agent.agency_id, agent_id=agent.id),
                           activity_unseen=unseen_activity_count(agent.agency_id, agent_id=agent.id),
                           activity_icon=activity_icon)


@app.route("/change-agent-password/<int:agent_id>", methods=["POST"])
def change_agent_password(agent_id):
    """Lets an agent (or the agency owner, or the super admin) rotate an
    agent's own login password - the agent-side equivalent of
    /change-owner-password."""
    agent = db.session.get(Agent, agent_id)
    if not agent:
        return jsonify({"error": "Agent not found"}), 404
    is_self = session.get('agent_id') == agent_id
    is_owner = session.get('agency_id') == str(agent.agency_id)
    is_super_admin = session.get('super_admin')
    if not (is_self or is_owner or is_super_admin):
        return jsonify({"error": "Unauthorized"}), 401
    data = request.get_json(force=True, silent=True) or {}
    new_password = (data.get("new_password") or "").strip()
    if len(new_password) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    agent.set_password(new_password)
    db.session.commit()
    return jsonify({"message": "Password updated"})


# ─────────────────────────────────────────────────────
# FORGOT / RESET PASSWORD - shared by Agency owners and Agents.
# Same design in both directions: never reveal whether an email matched
# (prevents account enumeration), token is single-use with a 1-hour
# expiry, and the reset landing page needs the raw token from the emailed
# link (never stored anywhere but hashed... actually stored raw here since
# it's single-use + time-boxed + never displayed back to the user).
# ─────────────────────────────────────────────────────

RESET_TOKEN_TTL_HOURS = 1
_GENERIC_RESET_MESSAGE = (
    "If an account exists with that email, we've sent a password reset link to it."
)


@app.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit(PASSWORD_RESET_LIMIT, methods=["POST"])
def forgot_password():
    if request.method == "GET":
        return render_template("forgot_password.html")

    email = (request.form.get("email") or "").strip().lower()
    if not email:
        return render_template("forgot_password.html", message=_GENERIC_RESET_MESSAGE)

    base_url = request.host_url.rstrip("/")

    agency = Agency.query.filter(db.func.lower(Agency.email) == email).first()
    if agency:
        token = secrets.token_urlsafe(32)
        agency.reset_token = token
        agency.reset_token_expires = datetime.utcnow() + timedelta(hours=RESET_TOKEN_TTL_HOURS)
        db.session.commit()
        reset_link = f"{base_url}/reset-password/{token}"
        send_email_brevo(
            agency.email,
            "Reset your Luxury Leads AI password",
            f"Hi {agency.owner_name or agency.name},\n\n"
            f"We received a request to reset your Luxury Leads AI login password.\n\n"
            f"Reset it here (valid for {RESET_TOKEN_TTL_HOURS} hour): {reset_link}\n\n"
            f"If you didn't request this, you can safely ignore this email."
        )

    agent = Agent.query.filter(db.func.lower(Agent.email) == email, Agent.status == 'active').first()
    if agent:
        token = secrets.token_urlsafe(32)
        agent.reset_token = token
        agent.reset_token_expires = datetime.utcnow() + timedelta(hours=RESET_TOKEN_TTL_HOURS)
        db.session.commit()
        reset_link = f"{base_url}/reset-password/{token}"
        send_email_brevo(
            agent.email,
            "Reset your Luxury Leads AI password",
            f"Hi {agent.name},\n\n"
            f"We received a request to reset your Luxury Leads AI login password.\n\n"
            f"Reset it here (valid for {RESET_TOKEN_TTL_HOURS} hour): {reset_link}\n\n"
            f"If you didn't request this, you can safely ignore this email."
        )

    # Same message whether or not anything matched - never leak which
    # emails exist in the system.
    return render_template("forgot_password.html", message=_GENERIC_RESET_MESSAGE)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    now = datetime.utcnow()
    agency = Agency.query.filter_by(reset_token=token).first()
    agent = None if agency else Agent.query.filter_by(reset_token=token).first()
    account = agency or agent

    token_valid = bool(
        account and account.reset_token_expires and account.reset_token_expires > now
    )

    if request.method == "GET":
        if not token_valid:
            return render_template("reset_password.html", token=token, invalid=True)
        return render_template("reset_password.html", token=token, invalid=False)

    if not token_valid:
        return render_template("reset_password.html", token=token, invalid=True)

    new_password = (request.form.get("new_password") or "").strip()
    confirm_password = (request.form.get("confirm_password") or "").strip()
    if len(new_password) < 6:
        return render_template("reset_password.html", token=token, invalid=False,
                                error="Password must be at least 6 characters")
    if new_password != confirm_password:
        return render_template("reset_password.html", token=token, invalid=False,
                                error="Passwords do not match")

    account.set_password(new_password)
    # Single-use: clear the token immediately so the same link can't be
    # replayed, whether it's an Agency or an Agent account.
    account.reset_token = None
    account.reset_token_expires = None
    db.session.commit()

    login_url = "/owner-login" if agency else "/agent-login"
    return render_template("reset_password.html", token=token, invalid=False,
                            success=True, login_url=login_url)


# ─────────────────────────────────────────────────────
# PROFILE MANAGEMENT (Item 5) - self-service editing of contact/business
# info. Separate from the password-change routes above on purpose: those
# are security-sensitive (session invalidation isn't needed here since
# nothing here touches password_hash), these are everyday detail edits.
# ─────────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@app.route("/agency-profile/<int:agency_id>")
def agency_profile(agency_id):
    if not _owner_owns_agency(agency_id):
        return redirect("/owner-login?error=Please+login+first")
    agency = db.session.get(Agency, agency_id)
    if not agency:
        return redirect("/owner-login?error=Agency+not+found")
    return render_template("agency_profile.html", agency=agency)


@app.route("/update-agency-profile/<int:agency_id>", methods=["POST"])
def update_agency_profile(agency_id):
    if not _owner_owns_agency(agency_id):
        return jsonify({"error": "Unauthorized"}), 401
    agency = db.session.get(Agency, agency_id)
    if not agency:
        return jsonify({"error": "Agency not found"}), 404

    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    if not name:
        return jsonify({"error": "Business name is required"}), 400
    if not email or not _EMAIL_RE.match(email):
        return jsonify({"error": "A valid email address is required"}), 400

    max_viewings_raw = data.get("max_viewings_per_slot", agency.max_viewings_per_slot)
    try:
        max_viewings = int(max_viewings_raw)
        if max_viewings < 1:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "Max viewings per slot must be a positive number"}), 400

    agency.name = name
    agency.email = email
    agency.owner_name = (data.get("owner_name") or "").strip() or None
    agency.whatsapp = (data.get("whatsapp") or "").strip() or None
    agency.assistant_name = (data.get("assistant_name") or "").strip() or "AI Assistant"
    agency.max_viewings_per_slot = max_viewings
    db.session.commit()
    return jsonify({"success": True, "message": "Profile updated"})


@app.route("/agent-profile/<int:agent_id>")
def agent_profile(agent_id):
    agent = db.session.get(Agent, agent_id)
    if not agent:
        return redirect("/agent-login?error=Agent+not+found")
    is_self = session.get('agent_id') == agent_id
    is_owner = session.get('agency_id') == str(agent.agency_id)
    is_super_admin = session.get('super_admin')
    if not (is_self or is_owner or is_super_admin):
        return redirect("/agent-login?error=Please+login+first")
    agency = db.session.get(Agency, agent.agency_id)
    return render_template("agent_profile.html", agent=agent, agency=agency)


@app.route("/update-agent-profile/<int:agent_id>", methods=["POST"])
def update_agent_profile(agent_id):
    agent = db.session.get(Agent, agent_id)
    if not agent:
        return jsonify({"error": "Agent not found"}), 404
    is_self = session.get('agent_id') == agent_id
    is_owner = session.get('agency_id') == str(agent.agency_id)
    is_super_admin = session.get('super_admin')
    if not (is_self or is_owner or is_super_admin):
        return jsonify({"error": "Unauthorized"}), 401

    # Agents manage their own PASSWORD only (see /change-agent-password).
    # Their name, login email and coverage area belong to the agency owner -
    # an agent quietly changing the email their leads are routed to is not
    # something the owner should find out about afterwards.
    if is_self and not (is_owner or is_super_admin):
        return jsonify({
            "error": "Your name, email and location are managed by your agency owner. "
                     "You can change your own password from your dashboard."
        }), 403

    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    location = (data.get("location") or "").strip()
    if not name:
        return jsonify({"error": "Name is required"}), 400
    if not email or not _EMAIL_RE.match(email):
        return jsonify({"error": "A valid email address is required"}), 400

    # Same-agency uniqueness, matching /add-agent's existing rule. A
    # different agency using this same email is a separate, pre-existing
    # issue with /agent-login's global-by-email lookup - not something to
    # silently paper over here.
    conflict = Agent.query.filter(
        Agent.agency_id == agent.agency_id,
        Agent.id != agent.id,
        db.func.lower(Agent.email) == email,
    ).first()
    if conflict:
        return jsonify({"error": "Another agent in your agency already uses this email"}), 400

    agent.name = name
    agent.email = email
    agent.location = location or None
    db.session.commit()
    return jsonify({"success": True, "message": "Profile updated"})


# ─────────────────────────────────────────────────────
# STEP 4C.1 - AGENT CLOSE/NOTE CAPABILITIES
# ─────────────────────────────────────────────────────

@app.route("/agent-update-lead-status/<int:lead_id>", methods=["POST"])
def agent_update_lead_status(lead_id):
    try:
        # Who's acting comes from the session, never from the request body -
        # a client-supplied agent_id could otherwise be swapped for any agent.
        agent_id = session.get('agent_id')
        if not agent_id:
            return jsonify({"error": "Unauthorized"}), 401
        data = request.get_json(force=True)
        lead = db.session.get(Lead, lead_id)
        if not lead or lead.agent_id != int(agent_id):
            return jsonify({"error": "Not authorized for this lead"}), 403
        new_status = data.get("status", "new")
        if new_status not in ['new', 'contacted', 'meeting', 'closed', 'lost']:
            return jsonify({"error": "Invalid status"}), 400
        previous = lead.lead_status or 'new'
        lead.lead_status = new_status
        db.session.commit()
        actor_type, actor_name, acting_agent_id = acting_identity()
        record_activity(
            lead.agency_id, 'lead_status',
            f"{actor_name} moved {lead.name or 'a lead'} from "
            f"{LEAD_STATUS_LABELS.get(previous, previous)} to "
            f"{LEAD_STATUS_LABELS.get(new_status, new_status)}",
            actor_type=actor_type, actor_name=actor_name,
            subject_type='lead', subject_id=lead.id, subject_name=lead.name,
            agent_id=lead.agent_id or acting_agent_id)
        return jsonify({"success": True, "status": new_status})
    except Exception:
        return jsonify({"error": "Failed to update"}), 500


@app.route("/agent-add-lead-note/<int:lead_id>", methods=["POST"])
def agent_add_lead_note(lead_id):
    try:
        agent_id = session.get('agent_id')
        if not agent_id:
            return jsonify({"error": "Unauthorized"}), 401
        data = request.get_json(force=True)
        lead = db.session.get(Lead, lead_id)
        if not lead or lead.agent_id != int(agent_id):
            return jsonify({"error": "Not authorized"}), 403
        note_text = data.get("note", "").strip()
        if not note_text:
            return jsonify({"error": "Note cannot be empty"}), 400
        acting_agent = db.session.get(Agent, int(agent_id))
        try:
            notes = json.loads(lead.notes or '[]')
        except:
            notes = []
        notes.append({
            "id": len(notes) + 1,
            "text": note_text,
            "author": acting_agent.name if acting_agent else "Agent",
            "timestamp": datetime.now(pytz.timezone('Asia/Karachi')).strftime('%B %d, %Y at %I:%M %p')
        })
        lead.notes = json.dumps(notes)
        db.session.commit()
        actor_type, actor_name, acting_agent_id = acting_identity()
        record_activity(
            lead.agency_id, 'lead_note',
            f"{actor_name} added a note on {lead.name or 'a lead'}: {note_text[:120]}",
            actor_type=actor_type, actor_name=actor_name,
            subject_type='lead', subject_id=lead.id, subject_name=lead.name,
            agent_id=lead.agent_id or acting_agent_id)
        return jsonify({"success": True})
    except Exception:
        return jsonify({"error": "Failed"}), 500


@app.route("/agent-update-appointment-status/<int:appt_id>", methods=["POST"])
def agent_update_appointment_status(appt_id):
    try:
        agent_id = session.get('agent_id')
        if not agent_id:
            return jsonify({"error": "Unauthorized"}), 401
        data = request.get_json(force=True)
        appt = db.session.get(Appointment, appt_id)
        if not appt or appt.agent_id != int(agent_id):
            return jsonify({"error": "Not authorized for this appointment"}), 403
        new_status = data.get("stage") or data.get("status") or "pending"
        if not apply_appointment_stage(appt, new_status, "agent"):
            return jsonify({"error": "Invalid status"}), 400
        acting_agent = db.session.get(Agent, int(agent_id))
        notify_other_agents_of_update(
            appt, acting_agent,
            f"set an appointment to '{APPOINTMENT_STAGE_LABELS.get(new_status, new_status)}'")
        record_activity(
            appt.agency_id, 'appointment_stage',
            f"{acting_agent.name if acting_agent else 'An agent'} set "
            f"{appt.customer_name or 'a client'}'s viewing "
            f"({appt.appointment_date or 'no date'}) to "
            f"{APPOINTMENT_STAGE_LABELS.get(new_status, new_status)}",
            actor_type='agent',
            actor_name=acting_agent.name if acting_agent else 'An agent',
            subject_type='appointment', subject_id=appt.id,
            subject_name=appt.customer_name, agent_id=appt.agent_id)
        return jsonify({"success": True, "status": new_status,
                        "stage": appointment_stage(appt)})
    except Exception:
        return jsonify({"error": "Failed to update"}), 500


@app.route("/agent-add-appointment-note/<int:appt_id>", methods=["POST"])
def agent_add_appointment_note(appt_id):
    try:
        agent_id = session.get('agent_id')
        if not agent_id:
            return jsonify({"error": "Unauthorized"}), 401
        data = request.get_json(force=True)
        appt = db.session.get(Appointment, appt_id)
        if not appt or appt.agent_id != int(agent_id):
            return jsonify({"error": "Not authorized"}), 403
        note_text = data.get("note", "").strip()
        if not note_text:
            return jsonify({"error": "Note cannot be empty"}), 400
        appt.notes = (appt.notes + "\n" if appt.notes else "") + note_text
        db.session.commit()
        acting_agent = db.session.get(Agent, int(agent_id))
        notify_other_agents_of_update(appt, acting_agent, "added a note to an appointment")
        record_activity(
            appt.agency_id, 'appointment_note',
            f"{acting_agent.name if acting_agent else 'An agent'} added a note on "
            f"{appt.customer_name or 'a client'}'s viewing: {note_text[:120]}",
            actor_type='agent',
            actor_name=acting_agent.name if acting_agent else 'An agent',
            subject_type='appointment', subject_id=appt.id,
            subject_name=appt.customer_name, agent_id=appt.agent_id)
        return jsonify({"success": True})
    except Exception:
        return jsonify({"error": "Failed"}), 500


@app.route("/set-appointment-outcome/<int:appt_id>", methods=["POST"])
def set_appointment_outcome(appt_id):
    """Owner-side manual entry - for viewings the customer discussed by
    phone or in person instead of clicking the emailed check-in link."""
    try:
        appt = db.session.get(Appointment, appt_id)
        if not appt:
            return jsonify({"error": "Appointment not found"}), 404
        if not _owner_owns_agency(appt.agency_id):
            return jsonify({"error": "Unauthorized"}), 401
        data = request.get_json(force=True, silent=True) or {}
        outcome = data.get("outcome", "")
        if not apply_appointment_outcome(appt, outcome, "owner"):
            return jsonify({"error": "Invalid outcome"}), 400
        actor_type, actor_name, _ = acting_identity()
        record_activity(
            appt.agency_id, 'appointment_outcome',
            f"{actor_name} recorded the outcome of {appt.customer_name or 'a client'}'s "
            f"viewing: {APPOINTMENT_STAGE_LABELS.get(outcome, outcome)}",
            actor_type=actor_type, actor_name=actor_name,
            subject_type='appointment', subject_id=appt.id,
            subject_name=appt.customer_name, agent_id=appt.agent_id)
        return jsonify({"success": True, "outcome": outcome})
    except Exception:
        return jsonify({"error": "Failed to update"}), 500


@app.route("/agent-set-appointment-outcome/<int:appt_id>", methods=["POST"])
def agent_set_appointment_outcome(appt_id):
    """Agent-side equivalent of /set-appointment-outcome - same guard
    pattern as the other agent-* appointment routes above (session
    agent_id must match the appointment's assigned agent)."""
    try:
        agent_id = session.get('agent_id')
        if not agent_id:
            return jsonify({"error": "Unauthorized"}), 401
        appt = db.session.get(Appointment, appt_id)
        if not appt or appt.agent_id != int(agent_id):
            return jsonify({"error": "Not authorized for this appointment"}), 403
        data = request.get_json(force=True, silent=True) or {}
        outcome = data.get("outcome", "")
        if not apply_appointment_outcome(appt, outcome, "agent"):
            return jsonify({"error": "Invalid outcome"}), 400
        acting_agent = db.session.get(Agent, int(agent_id))
        notify_other_agents_of_update(appt, acting_agent, f"set an appointment outcome to '{outcome}'")
        record_activity(
            appt.agency_id, 'appointment_outcome',
            f"{acting_agent.name if acting_agent else 'An agent'} recorded the outcome of "
            f"{appt.customer_name or 'a client'}'s viewing: "
            f"{APPOINTMENT_STAGE_LABELS.get(outcome, outcome)}",
            actor_type='agent',
            actor_name=acting_agent.name if acting_agent else 'An agent',
            subject_type='appointment', subject_id=appt.id,
            subject_name=appt.customer_name, agent_id=appt.agent_id)
        return jsonify({"success": True, "outcome": outcome})
    except Exception:
        return jsonify({"error": "Failed to update"}), 500


@app.route("/book-followup-viewing/<int:appt_id>", methods=["POST"])
def book_followup_viewing(appt_id):
    """'Customer wasn't sold on that one but wants to see another' - books
    the next viewing for the same customer straight from the appointment
    card, keeping them with the same agent, and marks the original as
    'wants other options' so the loop stays honest about what happened."""
    appt = db.session.get(Appointment, appt_id)
    if not appt:
        return jsonify({"error": "Appointment not found"}), 404
    is_owner = _owner_owns_agency(appt.agency_id)
    is_assigned_agent = appt.agent_id and session.get('agent_id') == appt.agent_id
    if not (is_owner or is_assigned_agent):
        return jsonify({"error": "Unauthorized"}), 401

    agency = db.session.get(Agency, appt.agency_id)
    if not agency:
        return jsonify({"error": "Agency not found"}), 404

    data = request.get_json(force=True, silent=True) or {}
    date_iso = (data.get("appointment_date_iso") or "").strip()
    time_label = (data.get("appointment_time") or "").strip()
    property_interest = (data.get("property_interest") or "").strip()
    if not date_iso or not time_label:
        return jsonify({"error": "Pick a date and a time for the new viewing"}), 400
    if time_label not in TIME_SLOTS:
        return jsonify({"error": "That time slot isn't offered"}), 400
    try:
        d = datetime.strptime(date_iso, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({"error": "Invalid date"}), 400
    if d.weekday() == 6:
        return jsonify({"error": "Sundays are closed - please pick another day"}), 400
    if not is_slot_within_booking_window(d, datetime.now(PK_TZ).date()):
        return jsonify({"error": "That date is outside the booking window"}), 400

    max_slot = get_slot_capacity(agency)
    if slot_booked_count(appt.agency_id, date_iso, time_label) >= max_slot:
        return jsonify({"error": "That slot is already full - pick another time"}), 409

    # Keep the customer with the agent who already knows them, as long as
    # they're free; otherwise fall back to the normal location-aware pick.
    property_location = None
    if property_interest:
        listing_row = Listing.query.filter_by(
            agency_id=appt.agency_id, title=property_interest).first()
        if listing_row:
            property_location = listing_row.location
    chosen = pick_agent_for_slot(agency, date_iso, time_label,
                                  appt.agent_id, property_location)

    new_appt = Appointment(
        agency_id=appt.agency_id,
        lead_id=appt.lead_id,
        agent_id=chosen.id if chosen else None,
        customer_name=appt.customer_name,
        customer_email=appt.customer_email,
        appointment_date=d.strftime('%A, %B %d, %Y'),
        appointment_date_iso=date_iso,
        appointment_time=time_label,
        property_interest=property_interest or 'Follow-up viewing',
        status='pending',
        notes=f"Follow-up viewing after {appt.property_interest or 'an earlier viewing'}."
    )
    db.session.add(new_appt)
    db.session.commit()

    # The original viewing is now definitively 'they wanted something else'.
    if appt.outcome not in APPOINTMENT_OUTCOMES:
        apply_appointment_stage(appt, 'wants_other_options',
                                 'agent' if is_assigned_agent else 'owner')

    send_appointment_confirmation(agency, new_appt)
    if chosen:
        notify_agent(chosen,
                     f"📅 Follow-up Viewing Booked - {new_appt.customer_name}",
                     f"Hi {chosen.name},\n\nA follow-up viewing was booked:\n\n"
                     f"Customer: {new_appt.customer_name}\nEmail: {new_appt.customer_email}\n"
                     f"Property: {new_appt.property_interest}\n"
                     f"Date: {new_appt.appointment_date}\nTime: {new_appt.appointment_time}\n\n"
                     f"Login: {PUBLIC_BASE_URL}/agent-login")
    actor_type, actor_name, _ = acting_identity()
    record_activity(
        appt.agency_id, 'appointment_new',
        f"{actor_name} booked a follow-up viewing for {new_appt.customer_name or 'a client'} "
        f"on {new_appt.appointment_date} at {time_label}"
        + (f" with {chosen.name}" if chosen else ""),
        actor_type=actor_type, actor_name=actor_name,
        subject_type='appointment', subject_id=new_appt.id,
        subject_name=new_appt.customer_name,
        agent_id=chosen.id if chosen else None)
    return jsonify({"success": True, "appointment_id": new_appt.id,
                     "message": f"Follow-up viewing booked for {new_appt.appointment_date} at {time_label}"})


@app.route("/appointment-feedback/<token>")
def appointment_feedback(token):
    """Public landing page behind the check-in email's links - no login,
    since the customer isn't a platform user. A ?choice= query param (buy /
    other / no) records the outcome on first visit; revisiting the same
    link (or a manually-recorded outcome beating them to it) just shows
    whatever outcome is already on file instead of overwriting it."""
    appt = Appointment.query.filter_by(checkin_token=token).first()
    if not appt:
        return render_template("appointment_feedback.html", invalid=True)

    choice = request.args.get("choice")
    choice_map = {"buy": "wants_to_buy", "other": "wants_other_options", "no": "not_interested"}
    if choice and not appt.outcome:
        outcome = choice_map.get(choice)
        if not outcome or not apply_appointment_outcome(appt, outcome, "customer"):
            return render_template("appointment_feedback.html", invalid=True)
        # The customer answering for themselves is the most important
        # update of all, and until now nobody was told it had happened.
        record_activity(
            appt.agency_id, 'customer_feedback',
            f"{appt.customer_name or 'A client'} answered the check-in email after their "
            f"viewing: {APPOINTMENT_STAGE_LABELS.get(outcome, outcome)}",
            actor_type='customer', actor_name=appt.customer_name or 'Client',
            subject_type='appointment', subject_id=appt.id,
            subject_name=appt.customer_name, agent_id=appt.agent_id,
            # 'wants to buy' already sends its own richer email from
            # apply_appointment_outcome - the feed row is enough here. The
            # other two answers used to go completely unannounced, which
            # is how an agent found out a viewing went nowhere by
            # noticing the customer had stopped replying.
            notify=(outcome != 'wants_to_buy'))

    agency = db.session.get(Agency, appt.agency_id)
    return render_template("appointment_feedback.html", invalid=False,
                            appt=appt, outcome=appt.outcome, agency=agency)


# ─────────────────────────────────────────────────────
# PHASE 2D ROUTES - PROPERTY LISTINGS
# ─────────────────────────────────────────────────────

def parse_price(price_str):
    if not price_str:
        return None
    try:
        clean = re.sub(r'[\$,\s]', '', str(price_str))
        if clean.lower().endswith('m'):
            return float(clean[:-1]) * 1_000_000
        elif clean.lower().endswith('k'):
            return float(clean[:-1]) * 1_000
        return float(clean)
    except:
        return None


def infer_listing_purpose(title, description, explicit=None):
    """Determines sale vs rent: explicit value wins, else guessed from text."""
    if explicit in ('sale', 'rent'):
        return explicit
    text = f"{title or ''} {description or ''}".lower()
    if 'for rent' in text or 'rental' in text or '/mo' in text or 'per month' in text:
        return 'rent'
    return 'sale'


@app.route("/listings/<int:agency_id>")
def listings(agency_id):
    if not _owner_owns_agency(agency_id):
        return redirect("/owner-login?error=Please+login+first")
    agency = db.session.get(Agency, agency_id)
    if not agency:
        return redirect("/owner-login?error=Agency+not+found")
    all_listings = Listing.query.filter_by(
        agency_id=agency_id
    ).order_by(Listing.status.asc(), Listing.price_numeric.asc()).all()
    # Properties submitted by owners through the chat wait here for a
    # decision - they are not shown to buyers until approved, so they get
    # their own section at the top rather than being lost in the list.
    pending = [l for l in all_listings if l.status == 'pending']
    live = [l for l in all_listings if l.status != 'pending']
    seller_names = {}
    for l in pending:
        if l.seller_lead_id:
            seller = db.session.get(Lead, l.seller_lead_id)
            if seller:
                seller_names[l.id] = f"{seller.name or 'Owner'} · {seller.email or ''}".strip(" ·")
    return render_template("listings.html", agency=agency, listings=live,
                           pending_listings=pending, seller_names=seller_names)


@app.route("/add-listing/<int:agency_id>", methods=["POST"])
def add_listing(agency_id):
    if not _owner_owns_agency(agency_id):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        agency = db.session.get(Agency, agency_id)
        if not agency:
            return jsonify({"error": "Agency not found"}), 404
        data = request.get_json(force=True)
        price_numeric = parse_price(data.get("price", ""))
        listing = Listing(
            agency_id=agency_id,
            title=data.get("title", "").strip(),
            location=data.get("location", "").strip(),
            price_raw=data.get("price", "").strip(),
            price=price_numeric,
            price_numeric=price_numeric,
            bedrooms=int(data["bedrooms"]) if data.get("bedrooms") else None,
            bathrooms=float(data["bathrooms"]) if data.get("bathrooms") else None,
            property_type=data.get("property_type", "").strip(),
            listing_purpose=infer_listing_purpose(data.get("title", ""), data.get("description", ""), data.get("purpose")),
            features=data.get("features", "").strip(),
            description=data.get("description", "").strip(),
            status="available"
        )
        db.session.add(listing)
        db.session.commit()
        print(f"✅ Listing added: {listing.title} (ID {listing.id})")
        return jsonify({"success": True, "listing_id": listing.id, "title": listing.title})
    except Exception as e:
        print(f"❌ Add listing error: {e}")
        db.session.rollback()
        return jsonify({"error": "Failed to add listing"}), 500


@app.route("/upload-listings/<int:agency_id>", methods=["POST"])
def upload_listings(agency_id):
    if not _owner_owns_agency(agency_id):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        agency = db.session.get(Agency, agency_id)
        if not agency:
            return jsonify({"error": "Agency not found"}), 404
        if 'file' not in request.files:
            return jsonify({"error": "No file uploaded"}), 400
        file = request.files['file']
        if not file.filename.endswith('.csv'):
            return jsonify({"error": "Only CSV files are supported"}), 400
        content = file.read().decode('utf-8-sig')
        reader = csv.DictReader(StringIO(content))
        added = 0
        errors = []
        for i, row in enumerate(reader, 1):
            try:
                row = {k.lower().strip(): v.strip() for k, v in row.items() if k}
                title = row.get('title', '').strip()
                if not title:
                    errors.append(f"Row {i}: Missing title, skipped")
                    continue
                price_str = row.get('price', '')
                price_numeric = parse_price(price_str)
                beds = None
                baths = None
                try:
                    if row.get('bedrooms'):
                        beds = int(float(row['bedrooms']))
                except:
                    pass
                try:
                    if row.get('bathrooms'):
                        baths = float(row['bathrooms'])
                except:
                    pass
                listing = Listing(
                    agency_id=agency_id,
                    title=title,
                    location=row.get('location', ''),
                    price_raw=price_str,
                    price=price_numeric,
                    price_numeric=price_numeric,
                    bedrooms=beds,
                    bathrooms=baths,
                    property_type=row.get('type', row.get('property_type', '')),
                    listing_purpose=infer_listing_purpose(title, row.get('description', ''), row.get('purpose')),
                    features=row.get('features', ''),
                    description=row.get('description', ''),
                    status='available'
                )
                db.session.add(listing)
                added += 1
            except Exception as row_err:
                errors.append(f"Row {i}: {str(row_err)}")
                continue
        db.session.commit()
        print(f"✅ CSV upload: {added} listings added for agency {agency_id}")
        return jsonify({
            "success": True, "added": added,
            "errors": errors,
            "message": f"{added} listings imported successfully"
        })
    except Exception as e:
        print(f"❌ CSV upload error: {e}")
        db.session.rollback()
        return jsonify({"error": f"Upload failed: {str(e)}"}), 500


@app.route("/toggle-listing-status/<int:listing_id>", methods=["POST"])
def toggle_listing_status(listing_id):
    try:
        listing = db.session.get(Listing, listing_id)
        if not listing:
            return jsonify({"error": "Listing not found"}), 404
        if not _owner_owns_agency(listing.agency_id):
            return jsonify({"error": "Unauthorized"}), 401
        data = request.get_json(force=True)
        new_status = data.get("status", "available")
        if new_status not in ['available', 'sold', 'pending', 'rejected']:
            return jsonify({"error": "Invalid status"}), 400
        listing.status = new_status
        db.session.commit()
        return jsonify({"success": True, "status": new_status})
    except Exception as e:
        return jsonify({"error": "Failed to update"}), 500


@app.route("/review-seller-listing/<int:listing_id>", methods=["POST"])
def review_seller_listing(listing_id):
    """Approve or reject a property an owner submitted through the chat.
    Nothing a seller types reaches a real buyer until this runs - approving
    is what flips it from 'pending' to 'available', which is the only
    status the AI ever reads from."""
    listing = db.session.get(Listing, listing_id)
    if not listing:
        return jsonify({"error": "Listing not found"}), 404
    if not _owner_owns_agency(listing.agency_id):
        return jsonify({"error": "Unauthorized"}), 401

    data = request.get_json(force=True, silent=True) or {}
    decision = (data.get("decision") or "").strip().lower()
    if decision not in ('approve', 'reject'):
        return jsonify({"error": "Decision must be approve or reject"}), 400

    listing.status = 'available' if decision == 'approve' else 'rejected'
    db.session.commit()
    print(f"🏠 Listing #{listing.id} {decision}d by owner")

    actor_type, actor_name, _ = acting_identity()
    seller_lead = db.session.get(Lead, listing.seller_lead_id) if listing.seller_lead_id else None
    record_activity(
        listing.agency_id,
        'listing_approved' if decision == 'approve' else 'listing_rejected',
        f"{actor_name} {'approved' if decision == 'approve' else 'rejected'} the property "
        f"\"{listing.title}\"{f' submitted by {seller_lead.name}' if seller_lead and seller_lead.name else ''}",
        actor_type=actor_type, actor_name=actor_name,
        subject_type='listing', subject_id=listing.id,
        subject_name=listing.title,
        agent_id=seller_lead.agent_id if seller_lead else None)

    # Tell the owner who submitted it that it's now live.
    if decision == 'approve' and listing.seller_lead_id:
        seller = db.session.get(Lead, listing.seller_lead_id)
        agency = db.session.get(Agency, listing.agency_id)
        if seller and seller.email and agency:
            send_email_brevo(
                seller.email,
                f"Your property is now listed with {agency.name}",
                f"Hi {seller.name or 'there'},\n\n"
                f"Good news - your property \"{listing.title}\" is now live with "
                f"{agency.name}, and we'll start matching it to buyers straight away.\n\n"
                f"Location: {listing.location or '—'}\n"
                f"Asking: {listing.price_raw or '—'}\n\n"
                f"If anything above needs correcting, just reply to this email.\n\n"
                f"{agency.name}")
    return jsonify({"success": True, "status": listing.status})


@app.route("/delete-listing/<int:listing_id>", methods=["DELETE"])
def delete_listing(listing_id):
    try:
        listing = db.session.get(Listing, listing_id)
        if not listing:
            return jsonify({"error": "Listing not found"}), 404
        if not _owner_owns_agency(listing.agency_id):
            return jsonify({"error": "Unauthorized"}), 401
        db.session.delete(listing)
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": "Failed to delete"}), 500


@app.route("/delete-all-listings/<int:agency_id>", methods=["DELETE"])
def delete_all_listings(agency_id):
    if not _owner_owns_agency(agency_id):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        count = Listing.query.filter_by(agency_id=agency_id).delete()
        db.session.commit()
        return jsonify({"success": True, "deleted": count})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Failed to delete listings"}), 500


@app.route("/get-listings/<int:agency_id>")
def get_listings_api(agency_id):
    if not _owner_owns_agency(agency_id):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        status_filter = request.args.get('status', 'all')
        query = Listing.query.filter_by(agency_id=agency_id)
        if status_filter != 'all':
            query = query.filter_by(status=status_filter)
        all_listings = query.order_by(Listing.price_numeric.asc()).all()
        return jsonify([{
            "id": l.id, "title": l.title, "location": l.location,
            "price": l.price_raw, "price_numeric": l.price_numeric,
            "bedrooms": l.bedrooms, "bathrooms": l.bathrooms,
            "type": l.property_type, "features": l.features,
            "description": l.description, "status": l.status
        } for l in all_listings])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─────────────────────────────────────────────────────

@app.route("/chat", methods=["POST", "OPTIONS"])
@limiter.limit(CHAT_LIMIT, methods=["POST"])
def chat():
    if request.method == "OPTIONS":
        return "", 200
    clean_expired_sessions()
    try:
        data = request.get_json(force=True)
        user_message = data.get("message", "").strip()
        agency_id = int(data.get("agency_id"))
        widget_session_id = data.get("session_id")
        if widget_session_id:
            session_key = f"{agency_id}_{widget_session_id}"
        else:
            visitor_ip = request.remote_addr or "unknown"
            user_agent = request.headers.get('User-Agent', '')
            session_hash = hashlib.md5(f"{visitor_ip}{user_agent}".encode()).hexdigest()[:12]
            session_key = f"{agency_id}_{session_hash}"
        if not user_message:
            return jsonify({"error": "Message required"}), 400
        agency = db.session.get(Agency, agency_id)
        if not agency:
            return jsonify({"error": "Invalid agency ID"}), 400

        history, booked_slots = load_session(session_key)
        history.append({"role": "user", "content": user_message})

        max_slot = get_slot_capacity(agency)
        chat_intent = detect_chat_intent(history)
        seller_mode = is_seller_intent(chat_intent)

        # A seller is not shopping. Showing them the buyer inventory (and
        # the viewing calendar) is how the AI ended up asking an owner for
        # their "budget" and offering to find them properties.
        listings_context = "" if seller_mode else get_listings_context(agency_id, history)
        availability_context = ("" if seller_mode
                                else get_availability_context(agency_id, max_slot, booked_slots))

        seller_context = ""
        if seller_mode:
            what = "sell" if chat_intent == 'sell' else "rent out"
            money = "asking price" if chat_intent == 'sell' else "monthly rent they want"
            seller_context = f"""

⚠️ THIS VISITOR IS A PROPERTY OWNER WHO WANTS TO {what.upper()} THEIR PROPERTY.
They are NOT shopping. Ignore the buyer sequence above entirely - every rule in it about
budgets, recommending listings, and booking viewings is wrong for this conversation.

NEVER do any of these with an owner:
- Ask what their "budget" is. They are receiving money, not spending it.
- Ask if they want to buy or rent something else.
- Offer them properties from our listings, or offer to find them options.
- Offer a viewing, a date, or a time slot.
- Promise to "find them a buyer soon" with a timeline you cannot keep.

INFORMATION TO COLLECT FROM THIS OWNER (strictly one at a time, in this order,
skipping anything already in ALREADY CAPTURED):
1. Where the property is located ("Whereabouts is the property?")
2. What type of property it is ("What kind of property is it - villa, house, apartment?")
3. How many bedrooms ("How many bedrooms does it have?")
4. How many bathrooms
5. Standout features and amenities ("Anything that makes it stand out - pool, garden, solar, parking?")
6. The {money} ("What {'price are you hoping for' if chat_intent == 'sell' else 'monthly rent are you asking'}?")
7. Timeline ("How soon are you hoping to {'sell' if chat_intent == 'sell' else 'have it rented out'}?") - ask this every time, in its own message. "No particular rush" is a complete answer; accept it and move on. Never press for a firmer date and never imply urgency they didn't express.
8. Email address
9. Contact preference: "Best way to reach you - WhatsApp, phone, or email?"

If they volunteer several of these at once, accept all of it and move to the next MISSING one.
Acknowledge what they describe warmly and specifically ("A pool and a home cinema - that will
appeal to the right buyer") without valuing the property or promising a price.

ONCE YOU HAVE ALL OF THE ABOVE, close the conversation like this and then STOP asking questions:
tell them their property details have been passed to the {agency.name} team, that an agent will
review and get in touch about next steps, and thank them. Do not invent timelines, valuations,
commission rates, or contract terms - an agent handles all of that."""

        # What we already know, stated plainly for the model. Relying on it to
        # re-read 20 messages and notice it already has an email is how
        # customers ended up being asked for the same email and the same
        # contact preference twice in one conversation.
        known = extract_lead_data(agency_id, history)
        known_bits = []
        if known.get('name'):
            known_bits.append(f"Name: {known['name']}")
        if known.get('email'):
            known_bits.append(f"Email: {known['email']}")
        if known.get('whatsapp_number'):
            known_bits.append(f"WhatsApp: {known['whatsapp_number']}")
        if known.get('phone'):
            known_bits.append(f"Phone: {known['phone']}")
        if known.get('budget'):
            known_bits.append(f"Budget: {known['budget']}")
        if known.get('timeline'):
            known_bits.append(f"Timeline: {timeline_label(known['timeline'])} (ALREADY ANSWERED)")
        if contact_step_completed(history):
            known_bits.append(
                f"Contact preference: {(known.get('contact_preference') or 'email').replace('_', ' ')} (ALREADY ANSWERED)")
        already_known_context = ""
        if known_bits:
            already_known_context = (
                "\n\nALREADY CAPTURED FROM THIS CUSTOMER - treat every item here as done:\n- "
                + "\n- ".join(known_bits)
                + "\nNever ask for any of the above again, not even to confirm it, and not in a recap. "
                "If you need to reference one, state it back as a fact ('I'll email you at "
                + (known.get('email') or 'the address you gave') + "'). "
                "Move straight to the next item that is genuinely missing."
            )

        system_prompt = f"""You are {agency.assistant_name}, a real estate consultant at {agency.name}.
{listings_context}
{already_known_context}

GOLDEN RULE - ONE QUESTION PER MESSAGE:
- Never ask two questions in one response. Ever.
- WRONG: "Interested in learning more? When are you hoping to move in?"
- WRONG: "Would you like to see it in person? Which day works best - Monday, Tuesday, Wednesday...?" (this is TWO questions: confirm interest, THEN ask the day - always separate messages)
- RIGHT: "Interested in learning more about it?"
- Wait for their answer before asking the next thing.

CONVERSATION START - GET NAME FIRST, ALWAYS, NO EXCEPTIONS:
- The client's FIRST message is asked who you're speaking with - no matter what that first message contains, even a greeting with small talk or a question back to you ("Hi, how are you?", "Hola, ¿cómo estás?").
- Do NOT answer small talk or reciprocate a question in the first message. Skip straight to asking their name.
- Example: Client says "Hi" → You say "Hello! May I know who I'm speaking with?"
- Example: Client says "Hi, how are you?" → You STILL say "Hello! May I know who I'm speaking with?" - do not answer "how are you" first.
- Client gives name → welcome them by name, say who you are and that you help with BUYING, SELLING and RENTING, then ask which one they're here for. Example: "Nice to meet you, [Name]! Welcome to {agency.name}. I'm {agency.assistant_name} and I can help you buy, sell or rent a property - which brings you in today?"
- That one question is mandatory: never assume someone is a buyer. An owner who wants to LIST a property needs completely different questions from someone looking for one.
- Use their name naturally throughout the conversation.
- NEVER ask for the name again once given.

PACE - LET THE CLIENT LEAD:
- The client came to ask questions. Answer them patiently and helpfully.
- Do not rush to collect information. Help them think and decide first.
- Only after they seem satisfied with a property choice, collect: email, then contact preference.
- Never interrogate. One relaxed question at a time.

PROPERTY RECOMMENDATIONS:
- The list below has ALREADY been filtered and ranked by the customer's stated budget, property type, bedroom/bathroom count, and buy/rent preference. Only recommend properties from THIS list - never invent or approximate one that isn't shown.
- If the customer hasn't given a location yet, that's fine - go ahead and offer from the list, since it already reflects their budget and type across all locations.
- Mention matches by name with price and key features in 1-2 sentences, then ask ONE question: "Would you like to know more?"
- If the list is empty: "We don't have anything matching that combination right now, but I can keep an eye out and get back to you with options." Even with no match, STILL continue the normal information flow afterward - ask for budget if you don't have it yet, then email, then contact preference - so we can follow up once something becomes available. Do not end the conversation early just because nothing matched right now.
- If the customer broadens or changes their criteria, treat the next matching list as the new source of truth.

VIEWING FLOW - ONE PROPERTY AT A TIME, ONE STEP AT A TIME:
- If client selects a property FROM THE LISTINGS and shows interest, offer a viewing in its OWN message: "Would you like to see it in person?" and STOP - wait for their answer. Do NOT list any days in this same message.
{availability_context}
- STRICT SEQUENCE for EACH property being booked:
  1. Only after they confirm they want a viewing, ask which DAY works, and list ONLY the day names with their dates from the availability above (e.g. "Monday Aug 17, Tuesday Aug 18, Wednesday Aug 19, Thursday Aug 20, Friday Aug 21, Saturday Aug 22"). Do NOT list any time slots yet - that comes after they pick a day.
  2. Once they pick a day, THEN list the open time slots for THAT DAY ONLY (e.g. "10:00 AM, 12:00 PM, 2:00 PM, 4:00 PM, 6:00 PM").
  3. Once they pick a time, confirm that ONE property's booking by name: "You're booked for [Property Name] on [full date] at [Time]."
- NEVER list multiple days' worth of time slots in a single message. NEVER dump every day and every time slot together - this is overwhelming and error-prone. One day list, then later one time list, per property.
- If the customer wants to view MORE THAN ONE property, handle them ONE AT A TIME, start to finish (day, then time, then confirm) before starting the next property's day/time from step 1. Never mix days or times for two different properties in the same message.
- After the LAST property's time is confirmed, ask for email if you don't have it yet. Then a short recap of all bookings together, each naming its property.
- If client wants to view a property NOT in the listings: say "Unfortunately we don't currently have a property matching your requirements. I'll find suitable options and get back to you to plan a viewing." Do NOT offer any dates or time slots in this case. Just collect their email and contact preference so the agency can follow up.

INFORMATION TO COLLECT FROM A BUYER OR RENTER (strictly one at a time, in this order):
1. Name (at the very start)
2. Property type ("What kind of property are you looking for?")
3. Buying or renting? ("Are you looking to buy or rent?")
4. Location ONLY ("Any particular area in mind?") - do NOT mention budget yet
5. Budget ONLY (after location is answered: "And what budget are you working with?")
6. Timeline ONLY ("How soon are you looking to move?") - ask this every time, in its own message, right after the budget. It is never optional and never combined with another question. If they have booked a viewing and you never asked it, ask it once straight after the booking is confirmed.
7. Email (after they're satisfied or a viewing is planned)
8. Contact preference: "Best way to reach you - WhatsApp, phone, or email?"
9. If WhatsApp/phone chosen: ask for the number. If they decline or say email only, that's fine.

WHY THE TIMELINE MATTERS - accept any answer, never push:
- "Just looking for now" is a complete, acceptable answer. Thank them and move on to the next item.
- Never ask a second time, never rephrase it to get a firmer date, and never imply urgency they didn't express.
- If they give a vague answer ("sometime this year"), that is enough - do not press for a month.
{seller_context}

NEVER combine location and budget in one question.
WRONG: "Could you share the location and your budget?"
RIGHT: "Any particular area in mind?" → wait → "And what's your budget?"

If customer volunteers multiple details in one message (e.g. "Miami, 10K per month"), accept ALL of it gracefully - acknowledge and move to the NEXT missing item. Never re-ask something they already told you.

Skip any numbered item above that appears in ALREADY CAPTURED - that list is authoritative. If the customer has to tell you something twice, you have failed. This applies to the closing recap too: recap the bookings, do not re-collect the email or the contact preference.

FORMATTING RULES:
- Never use markdown: no **, no *, no _, no #, no bullets, no numbered lists
- Plain conversational text only
- Short responses: 1-2 sentences
- Use contractions: "it's", "that's", "you're"

TONE:
- Warm, natural, like a knowledgeable friend
- Vary your acknowledgements - don't repeat "Perfect", "Great", "Awesome" more than once each
- Acknowledge what they said before responding

HANDLING HESITATION:
- Price concern: "I hear you - even a rough range helps. What feels comfortable?"
- Not ready: "No pressure at all. What's holding you back right now?"
- Indecisive: "If you had to pick just one thing that matters most, what would it be?"

LANGUAGE:
- Detect the visitor's language and respond in that same language throughout
- When mentioning viewing time slots in another language, keep the exact time format like 10:00 AM, 2:00 PM so the customer can reply with it

Respond naturally in plain text only:"""

        objection = detect_objection(user_message)
        objection_context = ""
        if objection:
            suggested_response = generate_objection_response(objection, agency.name)
            if suggested_response:
                objection_context = f"\n\nNOTE: User expressed a '{objection}' concern. Respond with empathy: '{suggested_response}'"

        messages = [{"role": "system", "content": system_prompt + objection_context}] + history[-20:]
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.7,
            max_tokens=350,
            presence_penalty=0.8,
            frequency_penalty=0.5
        )
        ai_reply = response.choices[0].message.content.strip()
        history.append({"role": "assistant", "content": ai_reply})

        lead_data = extract_lead_data(agency_id, history)

        # ─── Auto-appointment: books ALL requested slots (multi-property support) ───
        appt_data = extract_appointment_data(agency_id, history)

        if (appt_data['requested']
                and appt_data['slots']
                and lead_data.get('email')
                and lead_data.get('name')):
            today_pk = datetime.now(PK_TZ).date()
            for slot in appt_data['slots']:
                slot_id = f"{slot['iso']}|{slot['time']}"
                if slot_id in booked_slots:
                    continue
                slot_date = datetime.strptime(slot['iso'], '%Y-%m-%d').date()
                if not is_slot_within_booking_window(slot_date, today_pk):
                    print(f"⚠️ Date out of sane range: {slot['display']} - not auto-booking")
                    booked_slots.add(slot_id)
                    continue
                booked = slot_booked_count(agency_id, slot['iso'], slot['time'])
                if booked >= max_slot:
                    print(f"⚠️ Slot full ({booked}/{max_slot}): {slot['display']} at {slot['time']} - not booking")
                    booked_slots.add(slot_id)
                    continue
                existing_appt = Appointment.query.filter_by(
                    agency_id=agency_id,
                    customer_email=lead_data['email'],
                    appointment_date_iso=slot['iso'],
                    appointment_time=slot['time']
                ).first()
                if existing_appt:
                    booked_slots.add(slot_id)
                    continue
                try:
                    canonical_name, existing_lead_id = resolve_lead_identity(
                        agency_id, lead_data['email'], lead_data.get('name'))
                    preferred_id = None
                    if existing_lead_id:
                        lead_row = db.session.get(Lead, existing_lead_id)
                        if lead_row:
                            preferred_id = lead_row.agent_id
                    # Where the property actually is, so a local agent is
                    # preferred for the viewing.
                    slot_property = slot.get('property')
                    property_location = None
                    if slot_property:
                        listing_row = Listing.query.filter_by(
                            agency_id=agency_id, title=slot_property).first()
                        if listing_row:
                            property_location = listing_row.location
                    chosen_agent = pick_agent_for_slot(
                        agency, slot['iso'], slot['time'], preferred_id, property_location)
                    if chosen_agent:
                        print(f"👥 Appointment → agent {chosen_agent.name} (ID {chosen_agent.id})")
                    new_appt = Appointment(
                        agency_id=agency_id,
                        agent_id=chosen_agent.id if chosen_agent else None,
                        lead_id=existing_lead_id,
                        customer_name=canonical_name,
                        customer_email=lead_data['email'],
                        appointment_date=slot['display'],
                        appointment_date_iso=slot['iso'],
                        appointment_time=slot['time'],
                        property_interest=slot.get('property') or ((lead_data.get('budget') or '') + ' property viewing'),
                        status='pending'
                    )
                    db.session.add(new_appt)
                    db.session.commit()
                    booked_slots.add(slot_id)
                    print(f"✅ Appointment auto-booked: {new_appt.customer_name} | {slot['display']} at {slot['time']} ({booked + 1}/{max_slot})")
                    # notify=False: the confirmation and the agent's own
                    # assignment email fire right below. The feed row is
                    # what the dashboards read, not a second inbox copy.
                    record_activity(
                        agency_id, 'appointment_new',
                        f"{new_appt.customer_name or 'A visitor'} booked a viewing of "
                        f"{new_appt.property_interest or 'a property'} on "
                        f"{new_appt.appointment_date} at {new_appt.appointment_time}",
                        actor_type='customer', actor_name=new_appt.customer_name or 'Website visitor',
                        subject_type='appointment', subject_id=new_appt.id,
                        subject_name=new_appt.customer_name,
                        agent_id=chosen_agent.id if chosen_agent else None,
                        notify=False)
                    send_appointment_confirmation(agency, new_appt)
                    if chosen_agent:
                        notify_agent(chosen_agent,
                            f"📅 New Viewing Assigned - {new_appt.customer_name}",
                            f"Hi {chosen_agent.name},\n\nA viewing was booked and assigned to you:\n\nCustomer: {new_appt.customer_name}\nEmail: {new_appt.customer_email}\nDate: {new_appt.appointment_date}\nTime: {new_appt.appointment_time}\n\nLogin: {PUBLIC_BASE_URL}/agent-login")
                except Exception as appt_err:
                    print(f"⚠️ Auto-appointment error: {appt_err}")
                    db.session.rollback()

        # ─── Seller lead: a property owner, not a shopper ───
        if seller_mode and is_seller_lead_qualified(lead_data, history, chat_intent):
            try:
                canonical_name, existing_lead_id = resolve_lead_identity(
                    agency_id, lead_data['email'], lead_data.get('name'))
                existing_seller = db.session.get(Lead, existing_lead_id) if existing_lead_id else None
                if existing_seller:
                    print(f"⚠️ Seller lead already recorded: {lead_data['email']}")
                else:
                    ai_summary = generate_lead_summary(history, agency.name)
                    # Sellers are routed by where their PROPERTY is.
                    seller_property = extract_seller_property(history, chat_intent)
                    assigned = assign_next_agent(agency, seller_property.get('location'))
                    seller_score, seller_reasons = score_lead_quality(
                        lead_data, history, lead_type='seller',
                        seller_property=seller_property)
                    seller_lead = Lead(
                        agency_id=agency_id,
                        agent_id=assigned.id if assigned else None,
                        name=canonical_name,
                        email=lead_data['email'],
                        phone=lead_data.get('phone'),
                        whatsapp_number=lead_data.get('whatsapp_number'),
                        contact_preference=lead_data.get('contact_preference', 'email'),
                        budget=lead_data['budget'],          # their asking price
                        message=ai_summary,
                        intent_score=seller_score,
                        quality_reasons=json.dumps(seller_reasons),
                        timeline=lead_data.get('timeline'),
                        timeline_raw=lead_data.get('timeline_raw'),
                        lead_status='new',
                        lead_type='seller',
                        notes='[]',
                    )
                    db.session.add(seller_lead)
                    db.session.commit()

                    listing = None
                    if seller_property:
                        price_raw = seller_property.get('price_raw') or seller_lead.budget or ''
                        listing = Listing(
                            agency_id=agency_id,
                            title=(seller_property.get('title') or f"{canonical_name}'s property")[:200],
                            location=(seller_property.get('location') or '')[:200] or None,
                            price_raw=str(price_raw)[:100] or None,
                            price=parse_price(price_raw),
                            price_numeric=parse_price(price_raw),
                            bedrooms=seller_property.get('bedrooms'),
                            bathrooms=seller_property.get('bathrooms'),
                            property_type=(seller_property.get('property_type') or '')[:50] or None,
                            listing_purpose='rent' if chat_intent == 'rent_out' else 'sale',
                            features=(seller_property.get('features') or '')[:500] or None,
                            description=seller_property.get('description'),
                            status='pending',        # invisible to buyers until approved
                            source='seller_chat',
                            seller_lead_id=seller_lead.id,
                        )
                        db.session.add(listing)
                        db.session.commit()
                        print(f"🏠 Pending listing #{listing.id} from seller lead {seller_lead.id}")

                    record_activity(
                        agency_id, 'seller_lead',
                        f"New {seller_score}-star seller lead: {seller_lead.name or 'unnamed'} wants to "
                        f"{'sell' if chat_intent == 'sell' else 'rent out'} "
                        f"{listing.title if listing else 'a property'}"
                        + (f" — awaiting your approval" if listing else ""),
                        actor_type='system', actor_name='Chatbot',
                        subject_type='lead', subject_id=seller_lead.id,
                        subject_name=seller_lead.name,
                        agent_id=seller_lead.agent_id, notify=False)
                    notify_owner_of_seller_lead(agency, seller_lead, listing, chat_intent)
                    if assigned:
                        notify_agent(assigned,
                                     f"🏠 New Seller Lead Assigned - {seller_lead.name}",
                                     f"Hi {assigned.name},\n\nA property owner wants to "
                                     f"{'sell' if chat_intent == 'sell' else 'rent out'} their property "
                                     f"and has been assigned to you:\n\n"
                                     f"Name: {seller_lead.name}\nEmail: {seller_lead.email}\n"
                                     f"Property: {listing.title if listing else '—'}\n"
                                     f"Location: {listing.location if listing else '—'}\n"
                                     f"Asking: {listing.price_raw if listing else seller_lead.budget or '—'}\n\n"
                                     f"Login: {PUBLIC_BASE_URL}/agent-login")
                    print(f"✅ SELLER LEAD #{seller_lead.id} captured: {seller_lead.name}")
            except Exception as seller_err:
                print(f"⚠️ Seller lead error: {seller_err}")
                db.session.rollback()

        elif is_lead_qualified(lead_data, history, has_booking=bool(booked_slots)):
            try:
                canonical_name, existing_lead_id = resolve_lead_identity(
                    agency_id, lead_data['email'], lead_data.get('name'))
                existing_lead = db.session.get(Lead, existing_lead_id) if existing_lead_id else None
                if existing_lead:
                    updated = False
                    if not existing_lead.whatsapp_number and lead_data.get('whatsapp_number'):
                        existing_lead.whatsapp_number = lead_data['whatsapp_number']
                        existing_lead.contact_preference = lead_data['contact_preference']
                        updated = True
                    if not existing_lead.phone and lead_data.get('phone'):
                        existing_lead.phone = lead_data['phone']
                        existing_lead.contact_preference = lead_data['contact_preference']
                        updated = True
                    # A returning customer who finally names a date is a
                    # different lead to the agent than the one who didn't.
                    if not existing_lead.timeline and lead_data.get('timeline'):
                        existing_lead.timeline = lead_data['timeline']
                        existing_lead.timeline_raw = lead_data.get('timeline_raw')
                        updated = True
                    if updated:
                        rescore, rereasons = score_lead_quality(
                            lead_data, history, has_booking=bool(booked_slots),
                            lead_type=existing_lead.lead_type or 'buyer')
                        existing_lead.intent_score = rescore
                        existing_lead.quality_reasons = json.dumps(rereasons)
                    if updated:
                        db.session.commit()
                        print(f"✅ Lead {existing_lead.id} silently updated")
                    else:
                        print(f"⚠️ Duplicate: {lead_data['email']}")
                else:
                    ai_summary = generate_lead_summary(history, agency.name)
                    quality_score, quality_reasons = score_lead_quality(
                        lead_data, history, has_booking=bool(booked_slots))
                    # Route to an agent who covers the area this customer
                    # actually asked about, before falling back to round-robin.
                    wanted_cities = detect_location(agency_id, history)
                    assigned = assign_next_agent(
                        agency, ", ".join(wanted_cities) if wanted_cities else None)
                    lead = Lead(
                        agency_id=agency_id,
                        agent_id=assigned.id if assigned else None,
                        name=canonical_name,
                        email=lead_data['email'],
                        phone=lead_data.get('phone'),
                        whatsapp_number=lead_data.get('whatsapp_number'),
                        contact_preference=lead_data.get('contact_preference', 'email'),
                        budget=lead_data['budget'],
                        message=ai_summary,
                        intent_score=quality_score,
                        quality_reasons=json.dumps(quality_reasons),
                        timeline=lead_data.get('timeline'),
                        timeline_raw=lead_data.get('timeline_raw'),
                        lead_status='new',
                        notes='[]'
                    )
                    db.session.add(lead)
                    db.session.commit()
                    print(f"✅ Lead saved: ID {lead.id} | Score: {quality_score}/5")
                    record_activity(
                        agency_id, 'lead_new',
                        f"New {quality_score}-star buyer lead: {lead.name or 'unnamed'}"
                        + (f", timeline {timeline_label(lead.timeline)}" if lead.timeline else "")
                        + (f", assigned to {db.session.get(Agent, lead.agent_id).name}" if lead.agent_id and db.session.get(Agent, lead.agent_id) else ""),
                        actor_type='system', actor_name='Chatbot',
                        subject_type='lead', subject_id=lead.id, subject_name=lead.name,
                        agent_id=lead.agent_id, notify=False)
                    send_lead_email(agency, lead)
                    send_crm_webhook(agency, lead)
                    if lead.agent_id:
                        assigned_agent = db.session.get(Agent, lead.agent_id)
                        notify_agent(assigned_agent,
                            f"🎯 New Lead Assigned - {lead.name}",
                            f"Hi {assigned_agent.name},\n\nA new lead was assigned to you:\n\nName: {lead.name}\nEmail: {lead.email}\nBudget: {lead.budget}\n\nLogin: {PUBLIC_BASE_URL}/agent-login")
            except Exception as save_err:
                print(f"❌ Lead save error: {save_err}")
                db.session.rollback()

        save_session(session_key, history, booked_slots)
        return jsonify({"reply": ai_reply})
    except Exception as e:
        print(f"❌ CHAT ERROR: {e}")
        return jsonify({"error": "Connection issue"}), 500


@app.route("/delete-lead/<int:lead_id>", methods=["DELETE"])
def delete_lead(lead_id):
    try:
        lead = db.session.get(Lead, lead_id)
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        if not _owner_owns_agency(lead.agency_id):
            return jsonify({"error": "Unauthorized"}), 401
        db.session.delete(lead)
        db.session.commit()
        return jsonify({"message": "Lead deleted"})
    except Exception as e:
        return jsonify({"error": "Failed to delete"}), 500


@app.route("/clear-all-leads/<int:agency_id>", methods=["DELETE"])
def clear_all_leads(agency_id):
    if not _owner_owns_agency(agency_id):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        ConversationSession.query.filter(
            ConversationSession.session_key.like(f"{agency_id}_%")
        ).delete(synchronize_session=False)
        deleted_count = Lead.query.filter_by(agency_id=agency_id).delete()
        db.session.commit()
        return jsonify({"message": f"{deleted_count} leads deleted"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Failed to clear"}), 500


@app.route("/export/<int:agency_id>")
def export_leads(agency_id):
    if not _owner_owns_agency(agency_id):
        return redirect("/owner-login?error=Please+login+first")
    try:
        leads = Lead.query.filter_by(
            agency_id=agency_id
        ).order_by(Lead.intent_score.desc(), Lead.created_at.desc()).all()
        wb = Workbook()
        ws = wb.active
        ws.title = "Leads"
        headers = ["Sr #", "Quality", "Type", "Status", "Name", "Email", "Contact",
                   "Preference", "Budget", "Timeline", "Customer Insights", "Date"]
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for i, lead in enumerate(leads, start=1):
            quality_stars = "⭐" * (lead.intent_score or 1)
            contact = lead.whatsapp_number if lead.whatsapp_number else (lead.phone if lead.phone else "—")
            preference = lead.contact_preference.replace('_', ' ').title() if lead.contact_preference else "Email"
            status = (lead.lead_status or 'new').title()
            ws.append([
                i, quality_stars, (lead.lead_type or 'buyer').title(), status,
                lead.name or "—", lead.email or "—",
                contact, preference, lead.budget or "—",
                timeline_label(lead.timeline), lead.message or "—",
                lead.created_at.strftime('%Y-%m-%d') if lead.created_at else "—"
            ])
        for column in ws.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    if cell.value and len(str(cell.value)) > max_length:
                        max_length = len(str(cell.value))
                except:
                    pass
            ws.column_dimensions[column_letter].width = min(max_length + 2, 50)
        buffer = BytesIO()
        wb.save(buffer)
        buffer.seek(0)
        return Response(buffer,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=leads_agency_{agency_id}.xlsx"})
    except Exception as e:
        return jsonify({"error": "Export failed"}), 500


@app.route("/terms")
def terms():
    return render_template("terms.html")

@app.route("/privacy-policy")
def privacy():
    return render_template("privacy.html")

@app.route("/refund-policy")
def refund():
    return render_template("refund.html")

@app.route("/pricing")
def pricing():
    return render_template("pricing.html", show_tier_3=SHOW_TIER_3)


@app.route("/analytics/<int:agency_id>")
def analytics(agency_id):
    if not _owner_owns_agency(agency_id):
        return redirect("/owner-login?error=Please+login+first")
    agency = db.session.get(Agency, agency_id)
    if not agency:
        return redirect("/owner-login?error=Agency+not+found")
    leads = Lead.query.filter_by(agency_id=agency_id).all()
    now = datetime.utcnow()
    total = len(leads)
    hot = sum(1 for l in leads if l.intent_score == 5)
    high = sum(1 for l in leads if (l.intent_score or 1) >= 4)
    avg_score = round(sum(l.intent_score or 1 for l in leads) / total, 1) if total else 0.0
    quality_dist = {i: sum(1 for l in leads if (l.intent_score or 1) == i) for i in range(1, 6)}
    thirty_days_ago = now - timedelta(days=30)
    daily_counts = defaultdict(int)
    for lead in leads:
        if lead.created_at:
            try:
                dt = lead.created_at
                if hasattr(dt, 'tzinfo') and dt.tzinfo is not None:
                    dt = dt.replace(tzinfo=None)
                if dt >= thirty_days_ago:
                    daily_counts[dt.strftime('%Y-%m-%d')] += 1
            except Exception:
                pass
    date_labels, date_values = [], []
    for i in range(29, -1, -1):
        day = now - timedelta(days=i)
        date_labels.append(day.strftime('%b %d'))
        date_values.append(daily_counts.get(day.strftime('%Y-%m-%d'), 0))
    this_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month_end = this_month_start - timedelta(seconds=1)
    last_month_start = last_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    def naive(dt):
        if dt and hasattr(dt, 'tzinfo') and dt.tzinfo is not None:
            return dt.replace(tzinfo=None)
        return dt
    this_month = sum(1 for l in leads if naive(l.created_at) and naive(l.created_at) >= this_month_start)
    last_month = sum(1 for l in leads if naive(l.created_at) and last_month_start <= naive(l.created_at) <= last_month_end)
    return render_template("analytics.html",
        agency=agency, agency_id=agency_id, total=total, hot=hot, high=high,
        avg_score=avg_score, quality_dist=quality_dist, date_labels=date_labels,
        date_values=date_values, this_month=this_month, last_month=last_month)


@app.route("/update-agency-webhook/<int:agency_id>", methods=["POST"])
def update_agency_webhook(agency_id):
    if not _owner_owns_agency(agency_id):
        return redirect("/owner-login?error=Please+login+first")
    agency = db.session.get(Agency, agency_id)
    if not agency:
        return jsonify({"error": "Agency not found"}), 404
    webhook_url = request.form.get("webhook_url", "").strip()
    agency.webhook_url = webhook_url if webhook_url else None
    db.session.commit()
    return redirect(f"/analytics/{agency_id}")


@app.route("/send-followups", methods=["GET", "POST"])
def send_followups():
    results = process_pending_followups()
    results["appointment_checkins"] = process_appointment_checkins()
    return jsonify({"status": "ok", "results": results})


# -------------------------
# DATABASE INIT
# -------------------------
with app.app_context():
    db.create_all()
    print("✅ Database ready")
    try:
        from sqlalchemy import text, inspect
        inspector = inspect(db.engine)
        lead_cols = [col['name'] for col in inspector.get_columns('lead')]
        agency_cols = [col['name'] for col in inspector.get_columns('agency')]
        appt_cols = [col['name'] for col in inspector.get_columns('appointment')]

        if 'intent_score' not in lead_cols:
            db.session.execute(text("ALTER TABLE lead ADD COLUMN intent_score INTEGER DEFAULT 1;"))
            db.session.commit()
        if 'whatsapp_number' not in lead_cols:
            db.session.execute(text("ALTER TABLE lead ADD COLUMN whatsapp_number VARCHAR(50);"))
            db.session.commit()
        if 'contact_preference' not in lead_cols:
            db.session.execute(text("ALTER TABLE lead ADD COLUMN contact_preference VARCHAR(20) DEFAULT 'email';"))
            db.session.commit()
        if 'follow_up_1_sent' not in lead_cols:
            db.session.execute(text("ALTER TABLE lead ADD COLUMN follow_up_1_sent INTEGER DEFAULT 0;"))
            db.session.commit()
        if 'follow_up_7_sent' not in lead_cols:
            db.session.execute(text("ALTER TABLE lead ADD COLUMN follow_up_7_sent INTEGER DEFAULT 0;"))
            db.session.commit()
        if 'webhook_url' not in agency_cols:
            db.session.execute(text("ALTER TABLE agency ADD COLUMN webhook_url VARCHAR(500);"))
            db.session.commit()
        if 'lead_status' not in lead_cols:
            db.session.execute(text("ALTER TABLE lead ADD COLUMN lead_status VARCHAR(20) DEFAULT 'new';"))
            db.session.commit()
        if 'notes' not in lead_cols:
            db.session.execute(text("ALTER TABLE lead ADD COLUMN notes TEXT DEFAULT '[]';"))
            db.session.commit()

        if 'max_viewings_per_slot' not in agency_cols:
            db.session.execute(text("ALTER TABLE agency ADD COLUMN max_viewings_per_slot INTEGER DEFAULT 2;"))
            db.session.commit()
            print("✅ Migration: max_viewings_per_slot added")
        if 'appointment_date_iso' not in appt_cols:
            db.session.execute(text("ALTER TABLE appointment ADD COLUMN appointment_date_iso VARCHAR(20);"))
            db.session.commit()
            print("✅ Migration: appointment_date_iso added")

            # STEP 4A MIGRATIONS - Tier + Paddle fields
        for col, ddl in [
            ('tier', "ALTER TABLE agency ADD COLUMN tier VARCHAR(20) DEFAULT 'solo';"),
            ('parent_id', "ALTER TABLE agency ADD COLUMN parent_id INTEGER;"),
            ('paddle_customer_id', "ALTER TABLE agency ADD COLUMN paddle_customer_id VARCHAR(100);"),
            ('paddle_subscription_id', "ALTER TABLE agency ADD COLUMN paddle_subscription_id VARCHAR(100);"),
            ('subscription_status', "ALTER TABLE agency ADD COLUMN subscription_status VARCHAR(20) DEFAULT 'active';"),
            ('trial_ends_at', "ALTER TABLE agency ADD COLUMN trial_ends_at TIMESTAMP;"),
            ('billing_email', "ALTER TABLE agency ADD COLUMN billing_email VARCHAR(150);"),
        ]:
            if col not in agency_cols:
                db.session.execute(text(ddl))
                db.session.commit()
                print(f"✅ Migration: agency.{col} added")

        print("✅ All migrations complete")
    except Exception as e:
        print(f"⚠️ Migration error: {e}")
        db.session.rollback()

            # ── STEP 4B MIGRATIONS (self-contained) ──
    try:
        from sqlalchemy import text as _text, inspect as _inspect
        _insp = _inspect(db.engine)
        _lead_cols = [c['name'] for c in _insp.get_columns('lead')]
        _appt_cols = [c['name'] for c in _insp.get_columns('appointment')]

        if 'agent_id' not in _lead_cols:
            db.session.execute(_text("ALTER TABLE lead ADD COLUMN agent_id INTEGER;"))
            db.session.commit()
            print("✅ Migration: lead.agent_id added")
        else:
            print("✔ lead.agent_id already exists")

        if 'agent_id' not in _appt_cols:
            db.session.execute(_text("ALTER TABLE appointment ADD COLUMN agent_id INTEGER;"))
            db.session.commit()
            print("✅ Migration: appointment.agent_id added")
        else:
            print("✔ appointment.agent_id already exists")
    except Exception as e:
        print(f"⚠️ 4B migration error: {e}")
        db.session.rollback()

    # ── STEP 4C.1 MIGRATIONS (self-contained) ──
    try:
        from sqlalchemy import text as _text2, inspect as _inspect2
        _insp2 = _inspect2(db.engine)
        _listing_cols = [c['name'] for c in _insp2.get_columns('listing')]
        if 'listing_purpose' not in _listing_cols:
            db.session.execute(_text2("ALTER TABLE listing ADD COLUMN listing_purpose VARCHAR(10) DEFAULT 'sale';"))
            db.session.commit()
            print("✅ Migration: listing.listing_purpose added")
        else:
            print("✔ listing.listing_purpose already exists")
    except Exception as e:
        print(f"⚠️ 4C.1 migration error: {e}")
        db.session.rollback()

    # ── STEP 4C.2 MIGRATIONS (self-contained) ──
    # Upgrades bathrooms from INTEGER to FLOAT so half-baths (e.g. 4.5)
    # survive instead of being silently truncated to 4.
    try:
        from sqlalchemy import text as _text3
        db.session.execute(_text3("ALTER TABLE listing ALTER COLUMN bathrooms TYPE FLOAT USING bathrooms::float;"))
        db.session.commit()
        print("✅ Migration: listing.bathrooms upgraded to FLOAT")
    except Exception as e:
        db.session.rollback()
        print(f"✔ listing.bathrooms FLOAT migration skipped (already applied or n/a): {e}")

    # ── FORGOT/RESET PASSWORD MIGRATIONS (self-contained) ──
    try:
        from sqlalchemy import text as _text4, inspect as _inspect4
        _insp4 = _inspect4(db.engine)
        _agency_cols4 = [c['name'] for c in _insp4.get_columns('agency')]
        _agent_cols4 = [c['name'] for c in _insp4.get_columns('agent')]
        for table, cols in (('agency', _agency_cols4), ('agent', _agent_cols4)):
            if 'reset_token' not in cols:
                db.session.execute(_text4(f"ALTER TABLE {table} ADD COLUMN reset_token VARCHAR(100);"))
                db.session.commit()
                print(f"✅ Migration: {table}.reset_token added")
            if 'reset_token_expires' not in cols:
                db.session.execute(_text4(f"ALTER TABLE {table} ADD COLUMN reset_token_expires TIMESTAMP;"))
                db.session.commit()
                print(f"✅ Migration: {table}.reset_token_expires added")
    except Exception as e:
        db.session.rollback()
        print(f"⚠️ reset-token migration error: {e}")

    # ── POST-APPOINTMENT LOOP MIGRATIONS (self-contained) ──
    try:
        from sqlalchemy import text as _text5, inspect as _inspect5
        _insp5 = _inspect5(db.engine)
        _appt_cols5 = [c['name'] for c in _insp5.get_columns('appointment')]
        _appt_new_cols5 = {
            'outcome': 'VARCHAR(30)',
            'outcome_source': 'VARCHAR(20)',
            'outcome_at': 'TIMESTAMP',
            'checkin_token': 'VARCHAR(100)',
            'checkin_sent_at': 'TIMESTAMP',
        }
        for col_name, col_type in _appt_new_cols5.items():
            if col_name not in _appt_cols5:
                db.session.execute(_text5(f"ALTER TABLE appointment ADD COLUMN {col_name} {col_type};"))
                db.session.commit()
                print(f"✅ Migration: appointment.{col_name} added")
    except Exception as e:
        db.session.rollback()
        print(f"⚠️ post-appointment-loop migration error: {e}")

    # ── AGENT LOCATION MIGRATION (self-contained) ──
    try:
        from sqlalchemy import text as _text6, inspect as _inspect6
        _insp6 = _inspect6(db.engine)
        _agent_cols6 = [c['name'] for c in _insp6.get_columns('agent')]
        if 'location' not in _agent_cols6:
            db.session.execute(_text6("ALTER TABLE agent ADD COLUMN location VARCHAR(200);"))
            db.session.commit()
            print("✅ Migration: agent.location added")
    except Exception as e:
        db.session.rollback()
        print(f"⚠️ agent-location migration error: {e}")

    # ── SELLER-SIDE MIGRATIONS (self-contained) ──
    try:
        from sqlalchemy import text as _text7, inspect as _inspect7
        _insp7 = _inspect7(db.engine)
        _lead_cols7 = [c['name'] for c in _insp7.get_columns('lead')]
        _listing_cols7 = [c['name'] for c in _insp7.get_columns('listing')]
        if 'lead_type' not in _lead_cols7:
            db.session.execute(_text7(
                "ALTER TABLE lead ADD COLUMN lead_type VARCHAR(20) DEFAULT 'buyer';"))
            db.session.commit()
            print("✅ Migration: lead.lead_type added")
        if 'source' not in _listing_cols7:
            db.session.execute(_text7(
                "ALTER TABLE listing ADD COLUMN source VARCHAR(20) DEFAULT 'agency';"))
            db.session.commit()
            print("✅ Migration: listing.source added")
        if 'seller_lead_id' not in _listing_cols7:
            db.session.execute(_text7(
                "ALTER TABLE listing ADD COLUMN seller_lead_id INTEGER;"))
            db.session.commit()
            print("✅ Migration: listing.seller_lead_id added")
    except Exception as e:
        db.session.rollback()
        print(f"⚠️ seller-side migration error: {e}")

    # ── LEAD TIMELINE + SCORE BREAKDOWN MIGRATION (self-contained) ──
    try:
        from sqlalchemy import text as _text8, inspect as _inspect8
        _insp8 = _inspect8(db.engine)
        _lead_cols8 = [c['name'] for c in _insp8.get_columns('lead')]
        for _col, _ddl in (('timeline', 'VARCHAR(40)'),
                           ('timeline_raw', 'VARCHAR(200)'),
                           ('quality_reasons', 'TEXT')):
            if _col not in _lead_cols8:
                db.session.execute(_text8(f"ALTER TABLE lead ADD COLUMN {_col} {_ddl};"))
                db.session.commit()
                print(f"✅ Migration: lead.{_col} added")
    except Exception as e:
        db.session.rollback()
        print(f"⚠️ lead-timeline migration error: {e}")

    # ── ACTIVITY FEED MIGRATION (self-contained) ──
    # create_all() above already makes the table on a fresh database; this
    # is only here so an existing deployment picks it up on restart.
    try:
        from sqlalchemy import inspect as _inspect9
        if 'activity_event' not in _inspect9(db.engine).get_table_names():
            ActivityEvent.__table__.create(db.engine)
            print("✅ Migration: activity_event table created")
    except Exception as e:
        db.session.rollback()
        print(f"⚠️ activity-feed migration error: {e}")

# -------------------------
# RUN
# -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
