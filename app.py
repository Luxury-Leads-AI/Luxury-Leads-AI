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
    
    subject = f"🎯 New Lead for {agency.name}"
    body = f"""
New Lead Received from {agency.name}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
👤 Name:    {lead.name or 'Not provided'}
📧 Email:   {lead.email or 'Not provided'}
📱 Phone:   {lead.phone or 'Not provided'}
💰 Budget:  {lead.budget or 'Not provided'}

📝 CUSTOMER INSIGHTS:
{lead.message or 'No summary available'}

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
            from sendgrid import SendGridAPIClient
            from sendgrid.helpers.mail import Mail
            
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
    """
    AI-powered conversation summary with ENHANCED business intelligence
    """
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

Example good summary: "Buyer looking for 3-bed villa in Dubai Marina, budget 2-3M AED, wants to move within 3 months. Prioritizes sea view and proximity to metro. Requested tour availability."

Write the summary now:"""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": analysis_prompt}],
            temperature=0.3,
            max_tokens=120
        )

        summary = response.choices[0].message.content.strip()
        print(f"✅ AI Summary: {summary[:80]}...")
        return summary

    except Exception as e:
        print(f"❌ Summary error: {e}")
        return "Customer engaged in property conversation. Review chat history for details."


def analyze_lead_intent(conversation_history):
    """
    Calculate lead quality score (1-5) based on conversation signals
    """
    score = 1
    intent_keywords = {
        'high': ['buy', 'purchase', 'interested', 'schedule', 'tour', 'viewing', 'offer', 'serious'],
        'medium': ['looking', 'searching', 'find', 'need', 'want', 'considering'],
        'timing': ['asap', 'urgent', 'soon', 'this week', 'this month', 'immediately'],
        'budget': ['budget', 'price', 'afford', 'million', 'lakh', 'crore', 'aed', 'usd']
    }
    
    full_text = " ".join([msg['content'].lower() for msg in conversation_history if msg['role'] == 'user'])
    
    # Check for high-intent keywords
    if any(word in full_text for word in intent_keywords['high']):
        score += 2
    
    # Check for medium-intent keywords
    if any(word in full_text for word in intent_keywords['medium']):
        score += 1
    
    # Check for urgency
    if any(word in full_text for word in intent_keywords['timing']):
        score += 1
    
    # Check for budget discussion
    if any(word in full_text for word in intent_keywords['budget']):
        score += 1
    
    return min(score, 5)  # Cap at 5


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
    message = db.Column(db.Text)  # AI-generated summary
    
    intent_score = db.Column(db.Integer, default=1)  # NEW: Lead quality 1-5
    
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

        return render_template("admin.html", leads=leads, agency=agency, now=datetime.utcnow)

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
    """
    ENHANCED CHAT ENDPOINT
    - Human Sales Consultant personality
    - Context-aware responses
    - Intelligent lead detection
    - Quality scoring
    """
    if request.method == "OPTIONS":
        return "", 200

    try:
        data = request.get_json(force=True)

        user_message = data.get("message", "").strip()
        agency_id = int(data.get("agency_id"))
        
        # Session tracking per visitor
        visitor_ip = request.remote_addr or "unknown"
        user_agent = request.headers.get('User-Agent', '')
        session_hash = hashlib.md5(f"{visitor_ip}{user_agent}".encode()).hexdigest()[:12]
        session_key = f"{agency_id}_{session_hash}"

        if not user_message:
            return jsonify({"error": "Message required"}), 400

        agency = db.session.get(Agency, agency_id)
        if not agency:
            return jsonify({"error": "Invalid agency ID"}), 400

        # -------------------------
        # HUMAN SALES CONSULTANT PERSONALITY SYSTEM PROMPT
        # -------------------------
        system_prompt = f"""You are {agency.assistant_name}, a warm and professional real estate consultant at {agency.name}.

🎯 YOUR PERSONALITY:
- Conversational and friendly (use "I'm", "you're", occasional 👋 😊)
- Proactive helper who provides value BEFORE asking for contact info
- Context-aware: reference what was said earlier
- Natural flow: never sound scripted or robotic

💡 YOUR MISSION:
1. Understand what they're looking for through natural conversation
2. Provide genuine value (neighborhood insights, property details, market knowledge)
3. Collect contact info ONLY when it helps them (to send listings, schedule tour, follow up)
4. Use helpful phrases: "I can help with that", "Let me check that for you", "Would it be helpful if I..."

📋 CONVERSATION STRATEGY:
OPENING (First message):
- Warm greeting: "Hi! 👋 What brings you here today?" or "Hey! Looking for something specific or just browsing?"

QUALIFYING (Messages 2-4):
- Ask about property type: "Are you looking for a villa, apartment, or something else?"
- Location preference: "Which area are you most interested in?"
- Timeline: "Is this something you're looking to move on soon, or just exploring options?"

VALUE DELIVERY (Messages 3-6):
- Share insights: "That area has great schools and the metro is expanding there next year!"
- Provide context: "Properties in that range typically sell within 2-3 weeks in this market."
- Be helpful: "I can send you a few options that match what you're looking for."

LEAD CAPTURE (Natural moment):
- When they show interest: "I'd love to send you some listings! What's the best email to reach you?"
- When scheduling: "Want to see it in person? I can check availability. What's your phone number?"
- When following up: "I'll keep an eye out for you. Quick email so I can update you?"

🚫 NEVER:
- Ask for email/phone in first 2 messages
- Repeat questions already answered
- Sound like a form: "Please provide your name, email, phone..."
- Be pushy: "Buy now!" or "Limited time!"
- Give long, paragraph responses

✅ ALWAYS:
- Keep responses 1-2 sentences (3 max)
- Ask ONE question at a time
- Sound like texting with a knowledgeable friend
- Reference previous conversation
- Provide value before asking for anything

Example good flow:
User: "Hi"
You: "Hey! 👋 Looking for a property or just browsing around?"

User: "Looking for a 2 bedroom apartment"
You: "Nice! Which area were you thinking? Or open to suggestions?"

User: "Dubai Marina or JBR"
You: "Great choice! Both areas have amazing waterfront access. What's your budget range?"

User: "Around 1.5M"
You: "Perfect, there are some great options in that range. Want me to send you a few that just came on the market? I can email them over."

Now respond naturally to the user's message."""

        # -------------------------
        # CONVERSATION MEMORY
        # -------------------------
        if session_key not in conversation_memory:
            conversation_memory[session_key] = []

        history = conversation_memory[session_key]
        history.append({"role": "user", "content": user_message})

        # Build messages with context (last 20 messages)
        messages = [{"role": "system", "content": system_prompt}] + history[-20:]

        # -------------------------
        # CALL OPENAI
        # -------------------------
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.8,  # Higher for more natural variation
            max_tokens=150,
            presence_penalty=0.6,  # Reduce repetition
            frequency_penalty=0.3
        )

        ai_reply = response.choices[0].message.content.strip()
        
        # Add assistant name to memory
        history.append({
            "role": "assistant",
            "content": ai_reply,
            "name": agency.assistant_name
        })

        # -------------------------
        # INTELLIGENT LEAD DETECTION
        # -------------------------
        email_match = re.search(
            r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
            user_message
        )
        phone_match = re.search(
            r"\+?\d[\d\s\-\(\)]{7,}\d",
            user_message
        )
        budget_match = re.search(
            r"(?:budget|price|afford|spend).*?\$?\d+[\d,]*\.?\d*\s?(?:million|m|k|thousand|lakh|crore|pkr|rs|aed|usd)?\b",
            user_message,
            re.IGNORECASE
        )
        name_match = re.search(
            r"(?:i am|i'm|my name is|call me|this is|name's)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
            user_message,
            re.IGNORECASE
        )

        # High-intent keywords
        high_intent_keywords = [
            'schedule', 'tour', 'viewing', 'visit', 'see the property',
            'interested', 'want to buy', 'looking to purchase', 'make an offer',
            'talk to agent', 'speak to someone', 'call me', 'contact me'
        ]
        
        has_high_intent = any(keyword in user_message.lower() for keyword in high_intent_keywords)

        # -------------------------
        # SAVE LEAD LOGIC (ENHANCED)
        # -------------------------
        should_save_lead = (
            email_match or  # Contact info provided
            phone_match or
            (budget_match and len(history) >= 4) or  # Budget + engaged conversation
            (name_match and len(history) >= 4) or  # Name + engaged conversation
            has_high_intent or  # High-intent keywords
            len(history) >= 10  # Long conversation = serious interest
        )

        if should_save_lead:
            # Check if lead already exists for this session
            existing_lead = Lead.query.filter_by(
                agency_id=agency_id,
                email=email_match.group(0) if email_match else None
            ).first() if email_match else None

            if not existing_lead:
                # Generate AI summary
                ai_summary = generate_lead_summary(history, agency.name)
                
                # Calculate intent score
                intent_score = analyze_lead_intent(history)

                lead = Lead(
                    agency_id=agency_id,
                    name=name_match.group(1) if name_match else None,
                    email=email_match.group(0) if email_match else None,
                    phone=phone_match.group(0) if phone_match else None,
                    budget=budget_match.group(0) if budget_match else None,
                    message=ai_summary,
                    intent_score=intent_score
                )

                db.session.add(lead)
                db.session.commit()

                print(f"✅ Lead saved: ID {lead.id} | Score: {intent_score}/5")
                
                # Send email notification
                email_sent = send_lead_email(agency, lead)
                if email_sent:
                    print(f"   📧 Email notification sent to {agency.email}")
                else:
                    print(f"   ⚠️ Email notification failed")

        return jsonify({"reply": ai_reply})

    except Exception as e:
        print(f"❌ CHAT ERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "error": "I'm having trouble right now. Please try again!",
            "details": str(e) if os.getenv('ENV') == 'DEV' else None
        }), 500


@app.route("/export/<int:agency_id>")
def export_leads(agency_id):
    try:
        leads = Lead.query.filter_by(
            agency_id=agency_id
        ).order_by(Lead.intent_score.desc(), Lead.created_at.desc()).all()

        wb = Workbook()
        ws = wb.active
        ws.title = "Leads"

        # Updated headers with Quality Score
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

        # Auto-adjust column widths
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
    
    # Auto-migration: Add intent_score column if missing
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