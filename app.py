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
    """Send email notification - Tries SendGrid first, then Gmail SMTP"""
    
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
    
    # Try SendGrid first (works on Render free tier)
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
            
            print(f"✅ EMAIL SENT via SendGrid (Status: {response.status_code})")
            return True
            
        except Exception as e:
            print(f"⚠️ SendGrid failed: {e}")
            print("   Trying Gmail SMTP as backup...")
    
    # Fallback to Gmail SMTP (may be blocked on Render)
    if not SMTP_EMAIL or not SMTP_PASSWORD:
        print("⚠️ SMTP credentials not configured")
        return False

    try:
        print(f"📧 Attempting Gmail SMTP to: {agency.email}")
        
        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = SMTP_EMAIL
        msg['To'] = agency.email

        server = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=10)
        server.login(SMTP_EMAIL, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()

        print(f"✅ EMAIL SENT via Gmail SMTP")
        return True

    except OSError as e:
        if e.errno == 101:
            print(f"❌ Network Error: Render is blocking Gmail SMTP (port 465)")
            print(f"   Solution: Set up SendGrid API key in environment variables")
            print(f"   Sign up at: https://signup.sendgrid.com/")
        else:
            print(f"❌ Network Error: {e}")
        return False
        
    except Exception as e:
        print(f"❌ EMAIL ERROR: {type(e).__name__}: {e}")
        return False


def generate_lead_summary(conversation_history, agency_name):
    """AI-powered conversation summary with ENHANCED business intelligence"""
    try:
        conversation_text = "\n".join([
            f"{'Customer' if msg['role'] == 'user' else msg.get('name', 'Assistant')}: {msg['content']}"
            for msg in conversation_history
        ])

        analysis_prompt = f"""
Analyze this real estate conversation and create a BUSINESS-FOCUSED summary (2-3 sentences max).

You are summarizing for a real estate agent at {agency_name}. Focus on ACTION and INTENT.

PRIORITIZE:
1. Buying/selling intent and timeline
2. Property type and specific requirements
3. Budget/price range
4. Location preferences
5. Urgency signals ("looking now", "need to move by", etc.)
6. Hot buttons (schools, commute, specific amenities)

Conversation:
{conversation_text}

Format: "[INTENT] + [REQUIREMENTS] + [NEXT ACTION]"

Example: "Buyer looking for 3-bed villa in Dubai Marina, budget 2-3M AED, wants to move within 3 months. Prioritizes sea view and proximity to metro. Ready for property tour."

Write summary now:"""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": analysis_prompt}],
            temperature=0.3,
            max_tokens=120
        )

        summary = response.choices[0].message.content.strip()
        print(f"✅ AI Summary generated")
        return summary

    except Exception as e:
        print(f"❌ Summary error: {e}")
        return "Customer engaged in property conversation. Review chat history for details."


def extract_lead_data(conversation_history):
    """
    PRODUCTION FIX: Extract lead information with ALL budget formats
    """
    full_conversation = " ".join([msg['content'] for msg in conversation_history if msg['role'] == 'user'])
    
    lead_data = {
        'name': None,
        'email': None,
        'phone': None,
        'budget': None
    }
    
    # Extract email (most reliable)
    email_match = re.search(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
        full_conversation
    )
    if email_match:
        lead_data['email'] = email_match.group(0)
    
    # Extract name - IMPROVED
    name_patterns = [
        r"(?:my name is|name is|i'm|i am|call me|this is)\s+([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,})?)",
        r"(?:^|\s)([A-Z][a-z]{2,}\s+[A-Z][a-z]{2,})(?:\s|$)",
    ]
    
    for pattern in name_patterns:
        name_match = re.search(pattern, full_conversation, re.IGNORECASE | re.MULTILINE)
        if name_match:
            potential_name = name_match.group(1).strip()
            false_positives = ['looking for', 'interested in', 'searching for', 'want to', 'need to', 'like to', 'going to']
            if not any(fp in potential_name.lower() for fp in false_positives):
                lead_data['name'] = potential_name
                print(f"✅ Name extracted: {potential_name}")
                break
    
    # Extract phone - IMPROVED
    phone_patterns = [
        r"\+\d{1,4}[\s\-]?\d{2,4}[\s\-]?\d{3,4}[\s\-]?\d{3,4}",
        r"\+?\d[\d\s\-\(\)]{9,}",
    ]
    
    for pattern in phone_patterns:
        phone_match = re.search(pattern, full_conversation)
        if phone_match:
            lead_data['phone'] = phone_match.group(0).strip()
            print(f"✅ Phone extracted: {lead_data['phone']}")
            break
    
    # Extract budget - COMPREHENSIVE FIX for ALL formats
    budget_patterns = [
        # Pattern 1: "10M $" or "10M$" (number + unit + $)
        r"(\d+(?:\.\d+)?)\s*([MmKk])\s*\$",
        # Pattern 2: "$10M" or "$ 10M" ($ + number + unit)
        r"\$\s*(\d+(?:\.\d+)?)\s*([MmKk])",
        # Pattern 3: "10 million $" (number + word + $)
        r"(\d+(?:\.\d+)?)\s*(million|thousand|lakh|crore)\s*\$",
        # Pattern 4: "$10 million" ($ + number + word)
        r"\$\s*(\d+(?:\.\d+)?)\s*(million|thousand|lakh|crore)",
        # Pattern 5: "budget 10M" or "around 10M" (no currency)
        r"(?:budget|price|afford|spend|around|approximately|about)\s+(\d+(?:\.\d+)?)\s*([MmKk]|million|thousand)?",
        # Pattern 6: Plain number with currency mention
        r"(\d+(?:\.\d+)?)\s*(million|m|k|thousand|lakh|crore)?\s*(?:aed|usd|pkr|rs|dollars?)",
    ]
    
    for i, pattern in enumerate(budget_patterns):
        budget_match = re.search(pattern, full_conversation, re.IGNORECASE)
        if budget_match:
            amount = budget_match.group(1)
            unit = budget_match.group(2) if len(budget_match.groups()) > 1 and budget_match.group(2) else ''
            
            # Normalize unit
            if unit:
                unit_lower = unit.lower()
                if unit_lower in ['m', 'million']:
                    unit = 'million'
                elif unit_lower in ['k', 'thousand']:
                    unit = 'thousand'
                else:
                    unit = unit_lower
            
            # Detect currency
            currency = ''
            if '$' in full_conversation or 'dollar' in full_conversation.lower() or 'usd' in full_conversation.lower():
                currency = 'USD'
            elif 'aed' in full_conversation.lower():
                currency = 'AED'
            elif 'pkr' in full_conversation.lower() or 'rs' in full_conversation.lower() or 'rupee' in full_conversation.lower():
                currency = 'PKR'
            
            # Format
            if unit:
                lead_data['budget'] = f"{amount} {unit} {currency}".strip()
            else:
                lead_data['budget'] = f"{amount} {currency}".strip()
            
            print(f"✅ Budget extracted (pattern {i+1}): {lead_data['budget']}")
            break
    
    return lead_data


def analyze_lead_quality(lead_data, conversation_history):
    """
    PRODUCTION QUALITY SCORING:
    5 stars: Name + Email + Phone + Budget + Timeline/Urgency
    4 stars: Name + Email + Phone + Budget
    3 stars: Name + Email + Budget (no phone)
    2 stars: Email + (Name OR Budget)
    1 star: Email only
    """
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
    
    # Check urgency
    full_text = " ".join([msg['content'].lower() for msg in conversation_history if msg['role'] == 'user'])
    urgency_keywords = ['asap', 'urgent', 'soon', 'this week', 'this month', 'immediately', 'now', 'quickly', 'move soon', 'moving soon']
    
    has_urgency = any(keyword in full_text for keyword in urgency_keywords)
    if has_urgency:
        score = min(score + 1, 5)
    
    print(f"📊 Quality Score: Name={has_name}, Phone={has_phone}, Budget={has_budget}, Urgency={has_urgency} → {score}/5")
    
    return min(score, 5)


def is_lead_qualified(lead_data, conversation_history):
    """
    STRICTER QUALIFICATION:
    Must have: Email + Name + (Budget OR Phone) + at least 5 messages
    
    This ensures we wait for more complete data before saving
    """
    has_email = bool(lead_data.get('email'))
    has_name = bool(lead_data.get('name'))
    has_budget = bool(lead_data.get('budget'))
    has_phone = bool(lead_data.get('phone'))
    
    # Count user messages (indicates conversation depth)
    message_count = len([msg for msg in conversation_history if msg['role'] == 'user'])
    
    # STRICTER: Need email + name + (budget OR phone) + at least 5 exchanges
    is_qualified = (
        has_email and 
        has_name and 
        (has_budget or has_phone) and
        message_count >= 5
    )
    
    if is_qualified:
        print(f"✅ Lead QUALIFIED: Email={has_email}, Name={has_name}, Budget={has_budget}, Phone={has_phone}, Messages={message_count}")
    else:
        print(f"⚠️ NOT qualified yet: Email={has_email}, Name={has_name}, Budget={has_budget}, Phone={has_phone}, Messages={message_count}/5")
    
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


# -------------------------
# TEMPLATE ROUTES
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
    except Exception:
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


# -------------------------
# API ROUTES
# -------------------------

@app.route("/ping")
def ping():
    return jsonify({"status": "ok", "message": "pong", "timestamp": datetime.utcnow().isoformat()})

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

    return jsonify({
        "agency_id": agency.id,
        "message": "Agency created successfully"
    })

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
    """PRODUCTION CHAT with FIXED timing and extraction"""
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

        system_prompt = f"""You are {agency.assistant_name}, a professional real estate consultant at {agency.name}.

🎯 YOUR PERSONALITY:
- Warm, conversational, and helpful
- Natural language (use "I'm", "you're", occasional 👋)
- Short responses (1-2 sentences max)
- Ask ONE question at a time
- Never repeat questions

💡 YOUR MISSION - COLLECT IN THIS ORDER:
1. What they're looking for (property type)
2. Location preference
3. Budget range
4. Their NAME (always ask: "And what's your name?")
5. Email (to send listings)
6. Phone (optional - "What's the best number to reach you?")

📋 CONVERSATION FLOW:
OPENING:
"Hi! 👋 What brings you here today?"

QUALIFYING:
- Ask about property type first
- Then location
- Then budget
- THEN ask for their name: "Great! And what's your name?"
- Then email: "Perfect! What's your email so I can send you some listings?"
- Then phone: "And what's the best number to reach you?"

🚫 NEVER:
- Skip asking for name
- Ask for contact info in first 2 messages
- Sound like a form
- Give long responses

✅ ALWAYS:
- Ask for name after budget discussion
- Keep it conversational
- Reference previous answers
- Sound human and helpful

Now respond to the user naturally:"""

        if session_key not in conversation_memory:
            conversation_memory[session_key] = []

        history = conversation_memory[session_key]
        history.append({"role": "user", "content": user_message})

        messages = [{"role": "system", "content": system_prompt}] + history[-20:]

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.8,
            max_tokens=150,
            presence_penalty=0.6,
            frequency_penalty=0.3
        )

        ai_reply = response.choices[0].message.content.strip()
        
        history.append({
            "role": "assistant",
            "content": ai_reply,
            "name": agency.assistant_name
        })

        # Extract data
        lead_data = extract_lead_data(history)
        
        # Check qualification (STRICTER now)
        if is_lead_qualified(lead_data, history):
            existing_lead = Lead.query.filter_by(
                agency_id=agency_id,
                email=lead_data['email']
            ).first()
            
            if existing_lead:
                print(f"⚠️ Duplicate prevented: {lead_data['email']} (Lead ID: {existing_lead.id})")
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
                
                email_sent = send_lead_email(agency, lead)
                if email_sent:
                    print(f"   📧 Email sent")
                else:
                    print(f"   ⚠️ Email failed")

        return jsonify({"reply": ai_reply})

    except Exception as e:
        print(f"❌ CHAT ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "error": "I'm having trouble right now. Please try again!",
            "details": str(e) if os.getenv('ENV') == 'DEV' else None
        }), 500

@app.route("/delete-lead/<int:lead_id>", methods=["DELETE"])
def delete_lead(lead_id):
    try:
        lead = db.session.get(Lead, lead_id)
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        
        db.session.delete(lead)
        db.session.commit()
        print(f"🗑️ Lead deleted: ID {lead_id}")
        return jsonify({"message": "Lead deleted successfully"})
    except Exception as e:
        print(f"❌ DELETE ERROR: {e}")
        return jsonify({"error": "Failed to delete lead"}), 500

@app.route("/clear-all-leads/<int:agency_id>", methods=["DELETE"])
def clear_all_leads(agency_id):
    try:
        deleted_count = Lead.query.filter_by(agency_id=agency_id).delete()
        db.session.commit()
        print(f"🗑️ Cleared {deleted_count} leads for agency {agency_id}")
        return jsonify({"message": f"{deleted_count} leads deleted successfully"})
    except Exception as e:
        print(f"❌ CLEAR ALL ERROR: {e}")
        return jsonify({"error": "Failed to clear leads"}), 500

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
# DATABASE INIT & MIGRATION
# -------------------------
with app.app_context():
    db.create_all()
    print("✅ Database tables created/verified")
    
    try:
        from sqlalchemy import text, inspect
        
        inspector = inspect(db.engine)
        columns = [col['name'] for col in inspector.get_columns('lead')]
        
        if 'intent_score' not in columns:
            print("🔄 Running migration: Adding intent_score column...")
            
            db.session.execute(text("""
                ALTER TABLE lead 
                ADD COLUMN intent_score INTEGER DEFAULT 1;
            """))
            
            db.session.execute(text("""
                UPDATE lead 
                SET intent_score = 3 
                WHERE intent_score IS NULL;
            """))
            
            db.session.commit()
            print("✅ Migration complete: intent_score column added")
        else:
            print("✅ intent_score column already exists")
            
    except Exception as e:
        print(f"⚠️ Migration check: {e}")
        pass


# -------------------------
# RUN
# -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)