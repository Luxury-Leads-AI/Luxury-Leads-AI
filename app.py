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
# MEMORY STORE - PER VISITOR
# -------------------------
conversation_memory = {}

# -------------------------
# EMAIL CONFIG
# -------------------------
SMTP_EMAIL = os.getenv("SMTP_EMAIL")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

def send_lead_email(agency, lead):
    """Send email notification"""
    subject = f"🎯 New Qualified Lead for {agency.name}"
    body = f"""
New QUALIFIED Lead Received from {agency.name}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
👤 Name:    {lead.name or 'Not provided'}
📧 Email:   {lead.email or 'Not provided'}
📱 Phone:   {lead.phone or 'Not provided'}
💰 Budget:  {lead.budget or 'Not provided'}

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
    
    if SENDGRID_API_KEY:
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
            print(f"✅ EMAIL SENT via SendGrid")
            return True
        except Exception as e:
            print(f"⚠️ SendGrid failed: {e}")
    
    if not SMTP_EMAIL or not SMTP_PASSWORD:
        print("⚠️ SMTP credentials not configured")
        return False

    try:
        print(f"📧 Attempting Gmail SMTP")
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = SMTP_EMAIL
        msg['To'] = agency.email
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=10)
        server.login(SMTP_EMAIL, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()
        print(f"✅ EMAIL SENT via Gmail")
        return True
    except:
        return False


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
    """PRODUCTION: Flexible extraction for all writing styles"""
    full_conversation = " ".join([msg['content'] for msg in conversation_history if msg['role'] == 'user'])
    
    lead_data = {'name': None, 'email': None, 'phone': None, 'budget': None}
    
    # EMAIL
    email_match = re.search(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", full_conversation)
    if email_match:
        lead_data['email'] = email_match.group(0)
    
    # NAME - PRODUCTION FIX: Handles all cases
name_patterns = [
    # Pattern 1: "I am Wajid", "I'm John", "my name is Sarah"
    r"(?:i\s+am|i'm|my\s+name\s+is|name\s+is|call\s+me|this\s+is)\s+([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})?)",
    # Pattern 2: Standalone capitalized name
    r"\b([A-Z][a-z]{2,})\b",
]

for pattern in name_patterns:
    name_match = re.search(pattern, full_conversation, re.IGNORECASE)
    if name_match:
        potential_name = name_match.group(1).strip().title()  # Capitalize properly
        
        # Filter false positives
        false_positives = [
            'looking', 'interested', 'want', 'need', 'like', 'going', 'trying',
            'its', 'it', 'am', 'is', 'are', 'was', 'were', 'have', 'has',
            'beach', 'villa', 'property', 'house', 'apartment', 'usa', 'miami'
        ]
        
        # Must be at least 3 chars and not a false positive
        if (len(potential_name) >= 3 and 
            potential_name.lower() not in false_positives and
            not any(fp in potential_name.lower() for fp in ['looking for', 'interested in'])):
            lead_data['name'] = potential_name
            print(f"✅ Name: {potential_name}")
            break
    
    # PHONE - FIXED: Accept 9+ digits (was 10+)
    phone_patterns = [
        r"\+\d{1,4}[\s\-]?\d{2,4}[\s\-]?\d{3,4}[\s\-]?\d{2,4}",
        r"\+?\d{9,15}",  # FIXED: 9+ instead of 10+
        r"\d{3}[\s\-]?\d{3}[\s\-]?\d{3,4}",
    ]
    
    for pattern in phone_patterns:
        phone_match = re.search(pattern, full_conversation)
        if phone_match:
            phone = phone_match.group(0).strip()
            clean = phone.replace('+', '').replace('-', '').replace(' ', '')
            if len(clean) >= 9:  # FIXED: 9+ digits
                lead_data['phone'] = phone
                print(f"✅ Phone: {phone}")
                break
    
    # BUDGET - All formats
    budget_patterns = [
        r"(\d+(?:\.\d+)?)\s*([MmKk])\s*(?:\$|dollars?)?",
        r"[\$]\s*(\d+(?:\.\d+)?)\s*([MmKk]|million|thousand)?",
        r"(\d+(?:\.\d+)?)\s*(million|thousand|lakh|crore)\s*(?:\$|dollars?|usd|aed)?",
        r"(?:budget|price|around)\s*[\$]?(\d+(?:\.\d+)?)\s*([MmKk]|million)?",
    ]
    
    for i, pattern in enumerate(budget_patterns):
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


def analyze_lead_quality(lead_data, conversation_history):
    """Timeline-aware scoring"""
    score = 1
    
    has_name = bool(lead_data.get('name'))
    has_phone = bool(lead_data.get('phone'))
    has_budget = bool(lead_data.get('budget'))
    
    if has_name:
        score += 1
    if has_budget:
        score += 1
    if has_phone:
        score += 1
    
    # Timeline detection
    full_text = " ".join([msg['content'].lower() for msg in conversation_history if msg['role'] == 'user'])
    urgency = ['asap', 'urgent', 'soon', 'quickly', 'this week', 'this month', 'within', 'month', 'week']
    
    has_urgency = any(kw in full_text for kw in urgency)
    if has_urgency:
        score = min(score + 1, 5)
    
    print(f"📊 Quality: Name={has_name}, Phone={has_phone}, Budget={has_budget}, Timeline={has_urgency} → {score}/5")
    return min(score, 5)


def is_lead_qualified(lead_data, conversation_history):
    """
    PRODUCTION: Email + Name + Budget + 7+ messages
    Phone optional (improves score if present)
    """
    has_email = bool(lead_data.get('email'))
    has_name = bool(lead_data.get('name'))
    has_budget = bool(lead_data.get('budget'))
    
    message_count = len([msg for msg in conversation_history if msg['role'] == 'user'])
    
    # FIXED: 7 messages (phone comes on message 7)
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
    budget = db.Column(db.String(50))
    message = db.Column(db.Text)
    intent_score = db.Column(db.Integer, default=1)
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(pytz.timezone('Asia/Karachi')))  # FIXED: Local time


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
    """PRODUCTION CHAT - All fixes applied"""
    if request.method == "OPTIONS":
        return "", 200

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

        # IMPROVED PROMPT - Less repetitive
        system_prompt = f"""You are {agency.assistant_name}, a professional real estate consultant at {agency.name}.

PERSONALITY:
- Warm and helpful
- Short responses (1-2 sentences)
- Ask ONE question at a time
- Vary your language - don't repeat phrases

COLLECT IN ORDER:
1. Property type
2. Location
3. Budget
4. Timeline: "When are you looking to buy/move?"
5. Name: "What's your name?"
6. Email
7. Phone (optional)

CONVERSATION TIPS:
- Open: "Hi! What brings you here today?"
- Vary responses: Instead of always saying "Great!", use "Perfect", "Got it", "Understood", "Nice"
- Don't overuse emojis (max 1 per response)
- Sound natural, not robotic

Respond naturally:"""

        # FIXED: Clear memory when starting new conversation
        if session_key not in conversation_memory:
            conversation_memory[session_key] = []

        history = conversation_memory[session_key]
        history.append({"role": "user", "content": user_message})

        messages = [{"role": "system", "content": system_prompt}] + history[-20:]

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.7,  # Reduced for less repetition
            max_tokens=150,
            presence_penalty=0.8,  # Increased to avoid repetition
            frequency_penalty=0.5  # Increased to avoid repetition
        )

        ai_reply = response.choices[0].message.content.strip()
        
        history.append({
            "role": "assistant",
            "content": ai_reply,
            "name": agency.assistant_name
        })

        # Extract and qualify
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
                    phone=lead_data['phone'],
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
    """FIXED: Clear memory when lead deleted"""
    try:
        lead = db.session.get(Lead, lead_id)
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        
        # FIXED: Clear conversation memory for this lead's email
        for key in list(conversation_memory.keys()):
            if key.startswith(f"{lead.agency_id}_"):
                del conversation_memory[key]
        
        db.session.delete(lead)
        db.session.commit()
        print(f"🗑️ Lead deleted: ID {lead_id} + memory cleared")
        return jsonify({"message": "Lead deleted"})
    except Exception as e:
        print(f"❌ DELETE ERROR: {e}")
        return jsonify({"error": "Failed to delete"}), 500

@app.route("/clear-all-leads/<int:agency_id>", methods=["DELETE"])
def clear_all_leads(agency_id):
    """FIXED: Clear memory when all leads cleared"""
    try:
        # FIXED: Clear all conversation memory for this agency
        keys_to_delete = [k for k in conversation_memory.keys() if k.startswith(f"{agency_id}_")]
        for key in keys_to_delete:
            del conversation_memory[key]
        
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

        headers = ["Sr #", "Quality", "Name", "Email", "Phone", "Budget", "Customer Insights", "Date", "Time"]
        ws.append(headers)

        for cell in ws[1]:
            cell.font = Font(bold=True)

        for i, lead in enumerate(leads, start=1):
            quality_stars = "⭐" * (lead.intent_score or 1)
            
            ws.append([
                i,
                quality_stars,
                lead.name or "—",
                lead.email or "—",
                lead.phone or "—",
                lead.budget or "—",
                lead.message or "—",
                lead.created_at.strftime('%Y-%m-%d') if lead.created_at else "—",
                lead.created_at.strftime('%H:%M:%S') if lead.created_at else "—"
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