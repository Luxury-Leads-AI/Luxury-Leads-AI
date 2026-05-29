from flask import Flask, request, jsonify, render_template, Response, redirect, session
from flask_sqlalchemy import SQLAlchemy
from openai import OpenAI
from dotenv import load_dotenv
from pathlib import Path
from flask_cors import CORS
from datetime import datetime, timedelta
from openpyxl import Workbook
from openpyxl.styles import Font
from io import BytesIO
import os
import re
import smtplib
from email.mime.text import MIMEText
from werkzeug.security import generate_password_hash, check_password_hash
import hashlib
import pytz
from collections import defaultdict

# SendGrid email imports
try:
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail
    SENDGRID_AVAILABLE = True
except ImportError:
    SENDGRID_AVAILABLE = False
    print("⚠️ SendGrid not installed - email will use Gmail SMTP only")

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
# DATABASE URL FIX
# -------------------------
database_url = os.getenv('DATABASE_URL', 'sqlite:///luxury_leads.db')
if database_url.startswith('postgresql://'):
    database_url = database_url.replace('postgresql://', 'postgresql+psycopg://', 1)

app.config['SQLALCHEMY_DATABASE_URI'] = database_url
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'change-this-in-production')

db = SQLAlchemy(app)

# -------------------------
# OPENAI CLIENT
# -------------------------
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise ValueError("OPENAI_API_KEY is not set.")

client = OpenAI(api_key=api_key)

# -------------------------
# MEMORY STORE - PER VISITOR WITH EXPIRATION
# -------------------------
conversation_memory = {}
session_timestamps = {}

# -------------------------
# EMAIL CONFIG
# -------------------------
SMTP_EMAIL = os.getenv("SMTP_EMAIL")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

def send_lead_email(agency, lead):
    """Send email notification - SendGrid primary, Gmail fallback"""
    subject = f"🎯 New Qualified Lead for {agency.name}"
    
    # Determine contact method
    contact_info = ""
    if lead.whatsapp_number:
        contact_info = f"💬 WhatsApp: {lead.whatsapp_number}"
    elif lead.phone:
        contact_info = f"📱 Phone:    {lead.phone}"
    else:
        contact_info = "📱 Phone:    Not provided"

    body = f"""
New QUALIFIED Lead Received from {agency.name}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
👤 Name:    {lead.name or 'Not provided'}
📧 Email:   {lead.email or 'Not provided'}
{contact_info}
💰 Budget:  {lead.budget or 'Not provided'}
📞 Prefers: {lead.contact_preference.title() if lead.contact_preference else 'Email'}

📝 CUSTOMER INSIGHTS:
{lead.message or 'No summary available'}

🌟 Lead Quality: {"⭐" * (lead.intent_score or 1)} ({lead.intent_score}/5)

📅 Date: {lead.created_at.strftime('%Y-%m-%d %H:%M:%S')}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Login to view all leads:
https://luxury-leads-ai.onrender.com/owner-login

Agency ID: {agency.id}
Default Password: admin123
"""
    
    SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
    
    if SENDGRID_API_KEY and SENDGRID_AVAILABLE:
        try:
            print(f"📧 Sending via SendGrid to: {agency.email}")
            message = Mail(
                from_email=SMTP_EMAIL,
                to_emails=agency.email,
                subject=subject,
                plain_text_content=body
            )
            sg = SendGridAPIClient(SENDGRID_API_KEY)
            response = sg.send(message)
            print(f"✅ EMAIL SENT via SendGrid (Status: {response.status_code})")
            return True
        except Exception as e:
            print(f"⚠️ SendGrid failed: {e}")
            return False
    else:
        print("⚠️ SendGrid not configured - email will NOT be sent on Render")
        return False


def send_crm_webhook(agency, lead):
    """POST lead data to agency's configured CRM webhook URL"""
    if not agency.webhook_url:
        return
    try:
        payload = {
            "event": "lead_qualified",
            "agency_id": agency.id,
            "agency_name": agency.name,
            "lead": {
                "id": lead.id,
                "name": lead.name,
                "email": lead.email,
                "phone": lead.phone,
                "whatsapp_number": lead.whatsapp_number,
                "contact_preference": lead.contact_preference,
                "budget": lead.budget,
                "summary": lead.message,
                "intent_score": lead.intent_score,
                "created_at": lead.created_at.isoformat() if lead.created_at else None
            }
        }
        import httpx
        response = httpx.post(agency.webhook_url, json=payload, timeout=5)
        print(f"✅ Webhook sent to {agency.webhook_url} (Status: {response.status_code})")
    except Exception as e:
        print(f"⚠️ Webhook failed: {e}")


def send_followup_email(agency, lead, day):
    """Send a Day 1 or Day 7 follow-up reminder email to the agency"""
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
Hot leads respond best within the first 48 hours!

Login to view: https://luxury-leads-ai.onrender.com/owner-login
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

Login to view: https://luxury-leads-ai.onrender.com/owner-login
"""
    else:
        return False

    SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
    if SENDGRID_API_KEY and SENDGRID_AVAILABLE:
        try:
            message = Mail(
                from_email=SMTP_EMAIL,
                to_emails=agency.email,
                subject=subject,
                plain_text_content=body
            )
            sg = SendGridAPIClient(SENDGRID_API_KEY)
            response = sg.send(message)
            print(f"✅ Follow-up Day {day} sent to {agency.email} (Status: {response.status_code})")
            return True
        except Exception as e:
            print(f"⚠️ Follow-up Day {day} email failed: {e}")
            return False
    return False


def process_pending_followups():
    """Finds and sends all pending Day 1 and Day 7 follow-up emails"""
    try:
        now = datetime.utcnow()
        day1_count = 0
        day7_count = 0

        day1_cutoff = now - timedelta(hours=24)
        day1_leads = Lead.query.filter(
            Lead.follow_up_1_sent == 0,
            Lead.created_at <= day1_cutoff
        ).all()
        for lead in day1_leads:
            agency = db.session.get(Agency, lead.agency_id)
            if agency and send_followup_email(agency, lead, 1):
                lead.follow_up_1_sent = 1
                day1_count += 1
        db.session.commit()

        day7_cutoff = now - timedelta(days=7)
        day7_leads = Lead.query.filter(
            Lead.follow_up_7_sent == 0,
            Lead.created_at <= day7_cutoff
        ).all()
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


def clean_expired_sessions():
    """Remove conversation sessions older than 30 minutes"""
    try:
        current_time = datetime.utcnow()
        expired_keys = []
        
        for key in list(session_timestamps.keys()):
            last_activity = session_timestamps[key]
            time_diff = (current_time - last_activity).total_seconds()
            
            if time_diff > 1800:
                expired_keys.append(key)
        
        for key in expired_keys:
            if key in conversation_memory:
                del conversation_memory[key]
                print(f"🧹 Expired session cleared: {key}")
            if key in session_timestamps:
                del session_timestamps[key]
                
    except Exception as e:
        print(f"⚠️ Session cleanup error: {e}")


def generate_lead_summary(conversation_history, agency_name):
    """AI summary"""
    try:
        conversation_text = "\n".join([
            f"{'Customer' if msg['role'] == 'user' else 'Assistant'}: {msg['content']}"
            for msg in conversation_history
        ])

        analysis_prompt = f"""Analyze this conversation and create a 2-3 sentence business summary for {agency_name}.

Focus on: intent, property type, budget, location, timeline, urgency.

Conversation:
{conversation_text}

Format: "[INTENT] + [REQUIREMENTS] + [TIMELINE]"

Example: "Buyer seeking 3-bed villa in Dubai Marina, budget 2-3M AED, wants to move within 3 months."

Write summary:"""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": analysis_prompt}],
            temperature=0.3,
            max_tokens=120
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"❌ Summary error: {e}")
        return "Customer engaged in property conversation."


def extract_lead_data(conversation_history):
    """PRODUCTION: Ultra-strict extraction + WhatsApp support"""
    full_conversation = " ".join([msg['content'] for msg in conversation_history if msg['role'] == 'user'])
    
    lead_data = {
        'name': None, 
        'email': None, 
        'phone': None, 
        'whatsapp_number': None,
        'contact_preference': 'email',
        'budget': None
    }
    
    # EMAIL
    email_match = re.search(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", full_conversation)
    if email_match:
        lead_data['email'] = email_match.group(0)
    
    # NAME - ULTRA-STRICT BLOCKLIST
    name_blocklist = {
        'can', 'could', 'will', 'would', 'should', 'may', 'might', 'must',
        'looking', 'interested', 'want', 'need', 'like', 'going', 'trying',
        'searching', 'seeking', 'finding', 'buying', 'renting', 'moving',
        'not', 'sure', 'idea', 'just', 'also', 'here', 'there', 'then',
        'know', 'think', 'feel', 'seem', 'show', 'come', 'give', 'take',
        'okay', 'note', 'info', 'area', 'more', 'some', 'very', 'even',
        'asking', 'saying', 'telling', 'checking', 'getting', 'making',
        'coconut', 'grove', 'hilton', 'santa', 'monica', 'myrtle', 'island',
        'south', 'north', 'east', 'west', 'central', 'downtown', 'uptown',
        'am', 'is', 'are', 'was', 'were', 'have', 'has', 'been', 'being',
        'villa', 'house', 'apartment', 'property', 'condo', 'flat', 'home',
        'bedroom', 'bathroom', 'kitchen', 'garage', 'living', 'drawing',
        'beach', 'side', 'miami', 'york', 'washington', 'angeles', 'newyork',
        'malibu', 'florida', 'california', 'usa', 'location', 'cape', 'keys',
        'buy', 'rent', 'purchase', 'move', 'find', 'search', 'prefer', 'suggest',
        'perfect', 'great', 'nice', 'good', 'suitable', 'new', 'old', 'popular',
        'for', 'to', 'in', 'at', 'on', 'with', 'from', 'by', 'an', 'a', 'the',
        'what', 'where', 'when', 'why', 'how', 'which', 'who'
    }

    # Pattern 1: Explicit name introductions — prefer latest match
    explicit_pattern = r"(?:i\s+am|i'm|my\s+name\s+is|name\s+is|call\s+me|this\s+is)\s+([a-zA-Z]{3,})(?:\s|\.|\,|!|\?|$)"

    name_matches = list(re.finditer(explicit_pattern, full_conversation, re.IGNORECASE))
    for match in reversed(name_matches):
        potential_name = match.group(1).strip()
        if potential_name.lower() not in name_blocklist and len(potential_name) >= 3:
            lead_data['name'] = potential_name.title()
            print(f"✅ Name: {potential_name.title()}")
            break

    # Pattern 2: Standalone capitalized words (fallback)
    if not lead_data['name']:
        standalone_matches = re.findall(r"\b([A-Z][a-z]{3,})\b", full_conversation)
        for match in standalone_matches:
            if match.lower() not in name_blocklist and len(match) >= 4:
                lead_data['name'] = match.title()
                print(f"✅ Name: {match.title()} (standalone)")
                break
    
    # WHATSAPP vs PHONE DETECTION
    whatsapp_keywords = ['whatsapp', 'wa', 'whats app']
    mentions_whatsapp = any(kw in full_conversation.lower() for kw in whatsapp_keywords)
    
    phone_patterns = [
        r"\+\d{1,4}[\s\-]?\d{2,4}[\s\-]?\d{3,4}[\s\-]?\d{2,4}",
        r"\+?\d{9,15}",
        r"\d{3}[\s\-]?\d{3}[\s\-]?\d{3,4}",
    ]
    
    for pattern in phone_patterns:
        phone_match = re.search(pattern, full_conversation)
        if phone_match:
            phone = phone_match.group(0).strip()
            clean = phone.replace('+', '').replace('-', '').replace(' ', '')
            if len(clean) >= 9:
                if mentions_whatsapp:
                    lead_data['whatsapp_number'] = phone
                    lead_data['contact_preference'] = 'whatsapp'
                    print(f"✅ WhatsApp: {phone}")
                else:
                    lead_data['phone'] = phone
                    lead_data['contact_preference'] = 'phone'
                    print(f"✅ Phone: {phone}")
                break
    
    # BUDGET - All formats
    budget_patterns = [
        r"(\d+(?:\.\d+)?)\s*([MmKk])\s*(?:\$|dollars?)?",
        r"[\$]\s*(\d+(?:\.\d+)?)\s*([MmKk]|million|thousand)?",
        r"(\d+(?:\.\d+)?)\s*(million|thousand|lakh|crore)\s*(?:\$|dollars?|usd|aed)?",
        r"(?:budget|price|around|afford)\s*[\$]?(\d+(?:\.\d+)?)\s*([MmKk]|million|thousand)?",
    ]
    
    for pattern in budget_patterns:
        budget_match = re.search(pattern, full_conversation, re.IGNORECASE)
        if budget_match:
            amount = budget_match.group(1)
            unit = budget_match.group(2) if len(budget_match.groups()) > 1 and budget_match.group(2) else ''
            
            if unit:
                unit = unit.lower()
                if unit in ['m', 'million']:
                    unit = 'million'
                elif unit in ['k', 'thousand']:
                    unit = 'thousand'
            
            currency = ''
            if '$' in full_conversation or 'dollar' in full_conversation.lower():
                currency = 'USD'
            elif 'aed' in full_conversation.lower():
                currency = 'AED'
            
            lead_data['budget'] = f"{amount} {unit} {currency}".strip() if unit else f"{amount} {currency}".strip()
            print(f"✅ Budget: {lead_data['budget']}")
            break
    
    return lead_data


def detect_objection(user_message):
    """Detect if user message contains an objection"""
    user_message_lower = user_message.lower()
    
    objections = {
        'price': ['expensive', 'too much', 'costly', 'afford', 'budget', 'high price', 'over budget'],
        'timing': ['not ready', 'not sure', 'need time', 'thinking', 'maybe later', 'unsure'],
        'indecision': ['torn', 'confused', 'cant decide', "can't decide", 'both', 'either'],
        'trust': ['scam', 'legit', 'real', 'trust', 'safe', 'reliable']
    }
    
    for objection_type, keywords in objections.items():
        if any(keyword in user_message_lower for keyword in keywords):
            return objection_type
    
    return None


def generate_objection_response(objection_type, agency_name):
    """Generate contextual response to objection"""
    responses = {
        'price': "I hear you – budget is key. Even a rough range helps me point you in the right direction. What feels comfortable for you?",
        'timing': "Totally fair! No pressure at all. What's the main thing making you hesitant right now?",
        'indecision': "I get that – it's a big decision! Let's try this: if you had to pick just ONE thing that matters most to you, what would it be?",
        'trust': f"I understand the concern. {agency_name} is a licensed real estate agency. Would you like to know more about us, or would you prefer to just explore properties for now?"
    }
    
    return responses.get(objection_type, None)


def analyze_lead_quality(lead_data, conversation_history):
    """Timeline-aware scoring"""
    score = 1
    
    has_name = bool(lead_data.get('name'))
    has_phone = bool(lead_data.get('phone') or lead_data.get('whatsapp_number'))
    has_budget = bool(lead_data.get('budget'))
    
    if has_name:
        score += 1
    if has_budget:
        score += 1
    if has_phone:
        score += 1
    
    full_text = " ".join([msg['content'].lower() for msg in conversation_history if msg['role'] == 'user'])
    urgency = ['asap', 'urgent', 'soon', 'quickly', 'this week', 'this month', 'within', 'month', 'week']
    
    has_urgency = any(kw in full_text for kw in urgency)
    if has_urgency:
        score = min(score + 1, 5)
    
    print(f"📊 Quality: Name={has_name}, Phone={has_phone}, Budget={has_budget}, Timeline={has_urgency} → {score}/5")
    return min(score, 5)


def is_lead_qualified(lead_data, conversation_history):
    """PRODUCTION: Email + Name + Budget + 7+ messages"""
    has_email = bool(lead_data.get('email'))
    has_name = bool(lead_data.get('name'))
    has_budget = bool(lead_data.get('budget'))
    
    message_count = len([msg for msg in conversation_history if msg['role'] == 'user'])
    
    is_qualified = (
        has_email and 
        has_name and 
        has_budget and
        message_count >= 7
    )
    
    if is_qualified:
        print(f"✅ QUALIFIED: Email={has_email}, Name={has_name}, Budget={has_budget}, Msgs={message_count}")
    else:
        print(f"⚠️ Not yet: Email={has_email}, Name={has_name}, Budget={has_budget}, Msgs={message_count}/7")
    
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

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
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(pytz.timezone('Asia/Karachi')))
    follow_up_1_sent = db.Column(db.Integer, default=0)
    follow_up_7_sent = db.Column(db.Integer, default=0)


# -------------------------
# ROUTES
# -------------------------

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/signup")
def signup():
    return render_template("signup.html")

@app.route("/owner-login", methods=["GET", "POST"])
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

    if password == "admin123":
        session['agency_id'] = str(agency_id)
        return redirect(f"/admin?agency_id={agency_id}")
    else:
        return redirect("/owner-login?error=Invalid+password")

@app.route("/admin")
def admin():
    agency_id = request.args.get("agency_id")

    if not agency_id:
        return redirect("/owner-login?error=Please+login+first")

    try:
        leads = Lead.query.filter_by(
            agency_id=int(agency_id)
        ).order_by(Lead.intent_score.desc(), Lead.created_at.desc()).all()

        agency = db.session.get(Agency, int(agency_id))
        if not agency:
            return redirect("/owner-login?error=Agency+not+found")

        return render_template("admin.html", leads=leads, agency=agency, now=datetime.utcnow())

    except Exception as e:
        print(f"❌ ADMIN ERROR: {e}")
        return redirect("/owner-login?error=Something+went+wrong")

@app.route("/owner")
def owner():
    return render_template("owner.html")

@app.route("/ping")
def ping():
    return jsonify({"status": "ok", "message": "pong"})

@app.route("/create-agency", methods=["POST", "OPTIONS"])
def create_agency():
    if request.method == "OPTIONS":
        return "", 200

    data = request.json

    if not data.get("name") or not data.get("email"):
        return jsonify({"error": "Name and email required"}), 400

    agency = Agency(
        name=data.get("name"),
        prompt=data.get("prompt", "You are a luxury real estate assistant."),
        assistant_name=data.get("assistant_name", "AI Assistant"),
        owner_name=data.get("owner_name"),
        email=data.get("email"),
        whatsapp=data.get("whatsapp"),
        subscription_type=data.get("subscription_type", "Basic"),
        status="Active"
    )

    agency.set_password("admin123")
    db.session.add(agency)
    db.session.commit()

    return jsonify({"agency_id": agency.id, "message": "Agency created"})

@app.route("/agencies")
def get_agencies():
    agencies = Agency.query.all()
    return jsonify([{
        "id": a.id,
        "name": a.name,
        "assistant_name": a.assistant_name or "AI Assistant",
        "owner_name": a.owner_name or "—",
        "email": a.email,
        "status": a.status,
        "created_at": a.created_at.isoformat()
    } for a in agencies])

@app.route("/delete-agency/<int:agency_id>", methods=["DELETE"])
def delete_agency(agency_id):
    agency = db.session.get(Agency, agency_id)
    if not agency:
        return jsonify({"error": "Agency not found"}), 404

    Lead.query.filter_by(agency_id=agency_id).delete()
    db.session.delete(agency)
    db.session.commit()
    return jsonify({"message": "Agency deleted"})

@app.route("/agency/<int:agency_id>")
def agency_info(agency_id):
    agency = db.session.get(Agency, agency_id)
    if not agency:
        return jsonify({"error": "Invalid agency ID"}), 404

    return jsonify({
        "name": agency.name,
        "assistant": agency.assistant_name or "AI Assistant"
    })

@app.route("/chat", methods=["POST", "OPTIONS"])
def chat():
    """PRODUCTION CHAT WITH SESSION EXPIRATION + OBJECTION HANDLING"""
    if request.method == "OPTIONS":
        return "", 200

    clean_expired_sessions()

    try:
        data = request.get_json(force=True)
        user_message = data.get("message", "").strip()
        agency_id = int(data.get("agency_id"))
        
        visitor_ip = request.remote_addr or "unknown"
        user_agent = request.headers.get('User-Agent', '')
        session_hash = hashlib.md5(f"{visitor_ip}{user_agent}".encode()).hexdigest()[:12]
        session_key = f"{agency_id}_{session_hash}"

        if not user_message:
            return jsonify({"error": "Message required"}), 400

        agency = db.session.get(Agency, agency_id)
        if not agency:
            return jsonify({"error": "Invalid agency ID"}), 400

        system_prompt = f"""You are {agency.assistant_name}, a real estate consultant at {agency.name}.

CRITICAL FORMATTING RULES - MUST FOLLOW:
- Never use markdown: no **, no *, no _, no #, no bullet points with -, no numbered lists with 1. 2. 3.
- Never use bold or italic formatting of any kind
- Write in plain conversational text only
- If listing options, separate with commas or "or" not bullet points
- Example WRONG: "Here are some options: **Miami**, **Malibu**"
- Example RIGHT: "Some popular spots are Miami, Malibu, and Santa Monica."

GREETING RULES:
- Never start with the same greeting twice in a conversation
- Vary your opening: "Hi there!", "Hey!", "Hello!", "Welcome!", "Hey, good to see you!"
- After the first message, never use opening greetings again
- Do not say "Perfect", "Great", "Nice", "Awesome" more than once per conversation
- Do not say "Sounds good" repeatedly

PERSONALITY:
- Warm and natural, like a knowledgeable friend
- Short responses: 1-2 sentences only
- Ask ONE question at a time
- Use casual language and contractions: "it's", "that's", "you're", "we'll"
- Show genuine interest without being over-enthusiastic
- Acknowledge what they said before asking next question

CONVERSATION FLOW - COLLECT IN ORDER:
1. Property type (villa, condo, apartment, etc.)
2. Location preference
3. Budget (ask gently: "What budget range are you working with?")
4. Timeline ("When are you hoping to make a move?")
5. Name ("What's your name?")
6. Email
7. Contact preference: "Best way to reach you - WhatsApp, phone, or email?"
8. If WhatsApp/phone: ask for the number

HANDLING HESITATION:
- Price concern: "I hear you - even a rough range helps. What feels comfortable?"
- Not ready: "No pressure at all. What's holding you back right now?"
- Indecisive: "If you had to pick just one thing that matters most, what would it be?"

LANGUAGE:
- Detect the visitor's language and respond in that same language throughout
- Never switch back to English if they wrote in another language

DECISION SUPPORT (when visitor seems stuck):
- Readiness: "On a scale of 1-10, how ready do you feel to move forward?"
  - 7+: Help take next step
  - 4-6: "What would get you to a higher number?"
  - 1-3: "Let's just explore together, no pressure."
- Choice: "If you had to pick just one - location or size - which matters more?"
- Future: "A year from now, would you regret waiting or regret acting?"

Respond naturally in plain text only:"""

        if session_key not in conversation_memory:
            conversation_memory[session_key] = []
            print(f"🆕 New session started: {session_key}")

        session_timestamps[session_key] = datetime.utcnow()

        history = conversation_memory[session_key]
        history.append({"role": "user", "content": user_message})

        # Check for objections
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
            max_tokens=150,
            presence_penalty=0.8,
            frequency_penalty=0.5
        )

        ai_reply = response.choices[0].message.content.strip()
        
        history.append({
            "role": "assistant",
            "content": ai_reply,
            "name": agency.assistant_name
        })

        lead_data = extract_lead_data(history)

        if is_lead_qualified(lead_data, history):
            try:
                existing_lead = Lead.query.filter_by(
                    agency_id=agency_id,
                    email=lead_data['email']
                ).first()

                if existing_lead:
                    # FIX: Update existing lead with new WhatsApp/phone if not already set
                    updated = False

                    if not existing_lead.whatsapp_number and lead_data.get('whatsapp_number'):
                        existing_lead.whatsapp_number = lead_data['whatsapp_number']
                        existing_lead.contact_preference = 'whatsapp'
                        updated = True
                        print(f"✅ Updated WhatsApp for lead {existing_lead.id}: {lead_data['whatsapp_number']}")

                    if not existing_lead.phone and lead_data.get('phone'):
                        existing_lead.phone = lead_data['phone']
                        if existing_lead.contact_preference == 'email':
                            existing_lead.contact_preference = 'phone'
                        updated = True
                        print(f"✅ Updated Phone for lead {existing_lead.id}: {lead_data['phone']}")

                    if not existing_lead.name and lead_data.get('name'):
                        existing_lead.name = lead_data['name']
                        updated = True
                        print(f"✅ Updated Name for lead {existing_lead.id}: {lead_data['name']}")

                    if updated:
                        db.session.commit()
                        print(f"✅ Lead {existing_lead.id} updated with new contact info")
                    else:
                        print(f"⚠️ Duplicate (no new info): {lead_data['email']}")

                else:
                    ai_summary = generate_lead_summary(history, agency.name)
                    quality_score = analyze_lead_quality(lead_data, history)

                    lead = Lead(
                        agency_id=agency_id,
                        name=lead_data['name'],
                        email=lead_data['email'],
                        phone=lead_data.get('phone'),
                        whatsapp_number=lead_data.get('whatsapp_number'),
                        contact_preference=lead_data.get('contact_preference', 'email'),
                        budget=lead_data['budget'],
                        message=ai_summary,
                        intent_score=quality_score
                    )

                    db.session.add(lead)
                    db.session.commit()

                    print(f"✅ Lead saved: ID {lead.id} | Score: {quality_score}/5")

                    send_lead_email(agency, lead)
                    send_crm_webhook(agency, lead)

            except Exception as save_err:
                print(f"❌ Lead save error: {save_err}")
                db.session.rollback()

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
        
        for key in list(conversation_memory.keys()):
            if key.startswith(f"{lead.agency_id}_"):
                del conversation_memory[key]
                if key in session_timestamps:
                    del session_timestamps[key]
        
        db.session.delete(lead)
        db.session.commit()
        print(f"🗑️ Lead deleted: ID {lead_id} + memory cleared")
        return jsonify({"message": "Lead deleted"})
    except Exception as e:
        print(f"❌ DELETE ERROR: {e}")
        return jsonify({"error": "Failed to delete"}), 500

@app.route("/clear-all-leads/<int:agency_id>", methods=["DELETE"])
def clear_all_leads(agency_id):
    try:
        keys_to_delete = [k for k in conversation_memory.keys() if k.startswith(f"{agency_id}_")]
        for key in keys_to_delete:
            del conversation_memory[key]
            if key in session_timestamps:
                del session_timestamps[key]
        
        deleted_count = Lead.query.filter_by(agency_id=agency_id).delete()
        db.session.commit()
        print(f"🗑️ Cleared {deleted_count} leads + memory for agency {agency_id}")
        return jsonify({"message": f"{deleted_count} leads deleted"})
    except Exception as e:
        print(f"❌ CLEAR ERROR: {e}")
        return jsonify({"error": "Failed to clear"}), 500

@app.route("/export/<int:agency_id>")
def export_leads(agency_id):
    try:
        leads = Lead.query.filter_by(
            agency_id=agency_id
        ).order_by(Lead.intent_score.desc(), Lead.created_at.desc()).all()

        wb = Workbook()
        ws = wb.active
        ws.title = "Leads"

        headers = ["Sr #", "Quality", "Name", "Email", "Contact", "Preference", "Budget", "Customer Insights", "Date"]
        ws.append(headers)

        for cell in ws[1]:
            cell.font = Font(bold=True)

        for i, lead in enumerate(leads, start=1):
            quality_stars = "⭐" * (lead.intent_score or 1)
            contact = lead.whatsapp_number if lead.whatsapp_number else (lead.phone if lead.phone else "—")
            preference = lead.contact_preference.title() if lead.contact_preference else "Email"
            
            ws.append([
                i,
                quality_stars,
                lead.name or "—",
                lead.email or "—",
                contact,
                preference,
                lead.budget or "—",
                lead.message or "—",
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

        return Response(
            buffer,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=leads_agency_{agency_id}.xlsx"}
        )

    except Exception as e:
        print(f"❌ EXPORT ERROR: {e}")
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
    return render_template("pricing.html")


@app.route("/analytics/<int:agency_id>")
def analytics(agency_id):
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
        agency=agency,
        agency_id=agency_id,
        total=total,
        hot=hot,
        high=high,
        avg_score=avg_score,
        quality_dist=quality_dist,
        date_labels=date_labels,
        date_values=date_values,
        this_month=this_month,
        last_month=last_month
    )


@app.route("/update-agency-webhook/<int:agency_id>", methods=["POST"])
def update_agency_webhook(agency_id):
    agency = db.session.get(Agency, agency_id)
    if not agency:
        return jsonify({"error": "Agency not found"}), 404

    webhook_url = request.form.get("webhook_url", "").strip()
    agency.webhook_url = webhook_url if webhook_url else None
    db.session.commit()
    print(f"✅ Webhook URL updated for agency {agency_id}: {webhook_url or 'cleared'}")
    return redirect(f"/analytics/{agency_id}")


@app.route("/send-followups", methods=["GET", "POST"])
def send_followups():
    results = process_pending_followups()
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

        if 'intent_score' not in lead_cols:
            db.session.execute(text("ALTER TABLE lead ADD COLUMN intent_score INTEGER DEFAULT 1;"))
            db.session.commit()
            print("✅ Migration: intent_score added")

        if 'whatsapp_number' not in lead_cols:
            db.session.execute(text("ALTER TABLE lead ADD COLUMN whatsapp_number VARCHAR(50);"))
            db.session.commit()
            print("✅ Migration: whatsapp_number added")

        if 'contact_preference' not in lead_cols:
            db.session.execute(text("ALTER TABLE lead ADD COLUMN contact_preference VARCHAR(20) DEFAULT 'email';"))
            db.session.commit()
            print("✅ Migration: contact_preference added")

        if 'follow_up_1_sent' not in lead_cols:
            db.session.execute(text("ALTER TABLE lead ADD COLUMN follow_up_1_sent INTEGER DEFAULT 0;"))
            db.session.commit()
            print("✅ Migration: follow_up_1_sent added")

        if 'follow_up_7_sent' not in lead_cols:
            db.session.execute(text("ALTER TABLE lead ADD COLUMN follow_up_7_sent INTEGER DEFAULT 0;"))
            db.session.commit()
            print("✅ Migration: follow_up_7_sent added")

        if 'webhook_url' not in agency_cols:
            db.session.execute(text("ALTER TABLE agency ADD COLUMN webhook_url VARCHAR(500);"))
            db.session.commit()
            print("✅ Migration: webhook_url added")

        print("✅ All migrations complete")

    except Exception as e:
        print(f"⚠️ Migration error: {e}")
        db.session.rollback()


# -------------------------
# RUN
# -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)