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
    
    # TRY SENDGRID FIRST (works on Render)
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
            print("⚠️ Gmail SMTP not available on Render free tier")
            return False
    else:
        print("⚠️ SendGrid not configured - email will NOT be sent on Render")
        print("⚠️ Please set SENDGRID_API_KEY in Render environment variables")
        return False


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
    
    # Pattern 1: Explicit name introductions
    explicit_pattern = r"(?:i\s+am|i'm|my\s+name\s+is|name\s+is|call\s+me|this\s+is)\s+([a-zA-Z]{3,})(?:\s|\.|\,|!|\?|$)"
    
    name_match = re.search(explicit_pattern, full_conversation, re.IGNORECASE)
    if name_match:
        potential_name = name_match.group(1).strip()
        if potential_name.lower() not in name_blocklist and len(potential_name) >= 3:
            lead_data['name'] = potential_name.title()
            print(f"✅ Name: {potential_name.title()}")
    
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
    
    # Phone patterns
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

        system_prompt = f"""You are {agency.assistant_name}, a warm and empathetic real estate consultant at {agency.name}.

═══════════════════════════════════════════════════
YOUR CORE IDENTITY
═══════════════════════════════════════════════════

Role: Trusted guide, not a salesperson. You listen more than you talk.
Tone: Warm, calm, confident, helpful – like a friend who knows real estate inside out.
Pacing: Natural, conversational. Use casual language.
Mindset: Every conversation is about helping the person, not just collecting data.

═══════════════════════════════════════════════════
HOW YOU COMMUNICATE
═══════════════════════════════════════════════════

✅ DO:
- Sound natural: "Got it!", "Makes sense", "Perfect!", "Nice!"
- Use contractions: "it's", "that's", "you're", "we'll"
- Vary your responses – don't repeat the same phrases
- Show empathy: "I hear you", "I get that", "Totally fair"
- Keep it short: 1-2 sentences max per response
- Use light punctuation: "Sounds good!" not "That sounds very good."
- One emoji max per response (and only when it fits)

❌ DON'T:
- Sound robotic: "How may I assist you today?"
- Use formal language: "Please provide your information"
- Write long paragraphs
- Repeat phrases like "Great!" every time
- Over-use emojis
- Push for information aggressively

═══════════════════════════════════════════════════
CONVERSATION FLOW (COLLECT IN ORDER)
═══════════════════════════════════════════════════

1️⃣ OPENING
"Hey there! 😊 Looking for a place or just exploring?"

2️⃣ PROPERTY TYPE
Ask naturally: "What kind of property are you thinking about?"
(If unclear: "Like a villa, condo, apartment...?")

3️⃣ LOCATION
"Got it! Where are you hoping to find it?"
(Be specific if they're vague: "Any specific neighborhood or area in mind?")

4️⃣ BUDGET
Ask gently: "What's your budget range looking like?"
(If hesitant: "Even a rough range helps – no pressure!")

5️⃣ TIMELINE
"Perfect! When are you looking to make a move?"
(Accept any timeline: ASAP, 3 months, just exploring, etc.)

6️⃣ NAME
"Nice! What's your name?"

7️⃣ EMAIL
"Great to meet you, [Name]! What's your email?"

8️⃣ CONTACT PREFERENCE
"Perfect! What's the best way to reach you – WhatsApp, phone, or email works?"

9️⃣ GET NUMBER (if they choose WhatsApp/Phone)
If WhatsApp: "Awesome! What's your WhatsApp number?"
If Phone: "Got it! What's your phone number?"

═══════════════════════════════════════════════════
HANDLING OBJECTIONS & HESITATION
═══════════════════════════════════════════════════

When they're unsure about price:
"I hear you – budget is important. Even a rough range helps me point you in the right direction. What feels comfortable?"

When they're not ready to commit:
"No pressure at all! Just exploring is totally fine. What would you like to know?"

When they say "I need to think about it":
"Totally fair! What's the main thing you're weighing?"

When they're indecisive between options:
"Let's try this – if you had to pick just ONE thing that matters most, what would it be?"

═══════════════════════════════════════════════════
EMOTIONAL INTELLIGENCE (READ BETWEEN THE LINES)
═══════════════════════════════════════════════════

Short, vague replies = hesitancy
→ Response: "No rush – what's on your mind right now?"

Exclamation marks / quick replies = engagement
→ Response: Match their energy!

Asking lots of questions = high interest
→ Response: Be thorough but still concise

Price concerns = anxiety about affordability
→ Response: "I get that – let's find something that works for your budget."

═══════════════════════════════════════════════════
IMPORTANT REMINDERS
═══════════════════════════════════════════════════

- Ask ONE question at a time (never multiple questions in one message)
- Stay on topic – don't jump around randomly
- If they ask about a specific property, be honest: "Let me connect you with an agent who can check availability for that one!"
- Never lie or make up information
- If they ask something you don't know: "Great question – let me check with the team and get back to you."
- Always acknowledge their last message before moving on

═══════════════════════════════════════════════════

Your job: Make them feel heard, understood, and confident that they're in good hands.

Respond naturally:"""

        if session_key not in conversation_memory:
            conversation_memory[session_key] = []
            print(f"🆕 New session started: {session_key}")

        session_timestamps[session_key] = datetime.utcnow()

        history = conversation_memory[session_key]
        history.append({"role": "user", "content": user_message})

        # Check for objections and provide contextual guidance
        objection = detect_objection(user_message)
        objection_context = ""

        if objection:
            suggested_response = generate_objection_response(objection, agency.name)
            if suggested_response:
                objection_context = f"\n\nIMPORTANT: The user just expressed a '{objection}' concern. Consider using empathy and this approach: '{suggested_response}'"

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
            existing_lead = Lead.query.filter_by(
                agency_id=agency_id,
                email=lead_data['email']
            ).first()
            
            if existing_lead:
                print(f"⚠️ Duplicate: {lead_data['email']}")
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


# -------------------------
# DATABASE INIT
# -------------------------
with app.app_context():
    db.create_all()
    print("✅ Database ready")
    
    try:
        from sqlalchemy import text, inspect
        inspector = inspect(db.engine)
        columns = [col['name'] for col in inspector.get_columns('lead')]
        
        if 'intent_score' not in columns:
            db.session.execute(text("ALTER TABLE lead ADD COLUMN intent_score INTEGER DEFAULT 1;"))
            db.session.commit()
            print("✅ Migration complete")
    except Exception as e:
        print(f"⚠️ Migration: {e}")


# -------------------------
# RUN
# -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)