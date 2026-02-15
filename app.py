from flask import Flask, request, jsonify, render_template, Response, redirect, session
from flask_sqlalchemy import SQLAlchemy
from openai import OpenAI
from dotenv import load_dotenv
from pathlib import Path
from flask_cors import CORS
from datetime import datetime
from openpyxl import Workbook
from openpyxl.styles import Font, Border, Side
from io import BytesIO
import os
import re
import smtplib
from email.mime.text import MIMEText
from werkzeug.security import generate_password_hash, check_password_hash

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

# SECURITY: Restrict CORS to your domain only in production
CORS(app, resources={r"/*": {"origins": "*"}})  # TODO: Change to your domain

app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL', 'sqlite:///luxury_leads.db')
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
# MEMORY STORE
# -------------------------
conversation_memory = {}

# -------------------------
# EMAIL CONFIG
# -------------------------
SMTP_EMAIL = os.getenv("SMTP_EMAIL")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

def send_lead_email(agency, lead):
    """Send email notification when new lead is captured"""
    if not SMTP_EMAIL or not SMTP_PASSWORD:
        print("⚠️ SMTP credentials not configured")
        return False
    
    try:
        subject = f"🎯 New Lead for {agency.name}"
        body = f"""
New Lead Received from {agency.name}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
👤 Name: {lead.name or 'Not provided'}
📧 Email: {lead.email or 'Not provided'}
📱 Phone: {lead.phone or 'Not provided'}
💰 Budget: {lead.budget or 'Not provided'}

📝 CUSTOMER INSIGHTS:
{lead.message or 'No summary available'}

📅 Date: {lead.created_at.strftime('%Y-%m-%d %H:%M:%S')}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Login to view all leads:
https://luxury-leads-ai.onrender.com/owner-login
"""

        msg = MIMEText(body)
        msg['Subject'] = subject
        msg['From'] = SMTP_EMAIL
        msg['To'] = agency.email

        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(SMTP_EMAIL, SMTP_PASSWORD)
        server.send_message(msg)
        server.quit()
        
        print(f"✅ Email sent to {agency.email}")
        return True
        
    except Exception as e:
        print(f"❌ EMAIL ERROR: {e}")
        return False


def generate_lead_summary(conversation_history):
    """
    Uses AI to analyze the entire conversation and generate
    an intelligent summary of customer needs and preferences
    """
    try:
        # Build full conversation context
        conversation_text = "\n".join([
            f"{'Customer' if msg['role'] == 'user' else 'Assistant'}: {msg['content']}"
            for msg in conversation_history
        ])
        
        # AI analysis prompt
        analysis_prompt = f"""
Analyze this real estate conversation and create a SHORT business summary (max 2-3 sentences).

Focus on:
- What property type they want (villa/apartment/land)
- Location preferences
- Budget range mentioned
- Key requirements (bedrooms, amenities, investment/residence)
- Urgency level if mentioned

Conversation:
{conversation_text}

Write a concise business summary that helps a real estate agent quickly understand this lead's needs:"""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": analysis_prompt}],
            temperature=0.3,
            max_tokens=100
        )
        
        summary = response.choices[0].message.content.strip()
        print(f"✅ AI Summary generated: {summary[:50]}...")
        return summary
        
    except Exception as e:
        print(f"❌ Summary generation error: {e}")
        return "Customer engaged in conversation about properties."


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
    
    password_hash = db.Column(db.String(200))  # For owner login
    
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
    message = db.Column(db.Text)  # Now stores AI-generated summary
    
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# -------------------------
# ROUTES - TEMPLATE PAGES
# -------------------------

@app.route("/")
def home():
    """Landing page"""
    return render_template("index.html")


@app.route("/signup")
def signup():
    """Agency signup page"""
    return render_template("signup.html")


@app.route("/owner-login", methods=["GET", "POST"])
def owner_login():
    """Owner login page with authentication"""
    if request.method == "GET":
        return render_template("owner_login.html")
    
    # POST - Handle login
    agency_id = request.form.get("agency_id")
    password = request.form.get("password")
    
    if not agency_id or not password:
        return "Missing credentials", 400
    
    agency = Agency.query.get(agency_id)
    
    if not agency:
        return "Invalid Agency ID", 401
    
    # For now, simple password check (default: "admin123")
    # TODO: Implement proper password check with agency.check_password()
    if password == "admin123":
        session['agency_id'] = agency_id
        return redirect(f"/admin?agency_id={agency_id}")
    else:
        return "Invalid password", 401


@app.route("/admin")
def admin():
    """Agency dashboard - shows leads"""
    agency_id = request.args.get("agency_id")
    
    if not agency_id:
        return "Agency ID required", 400
    
    leads = Lead.query.filter_by(agency_id=agency_id).order_by(Lead.created_at.desc()).all()
    
    return render_template("admin.html", leads=leads)


@app.route("/owner")
def owner():
    """Owner panel for managing agencies"""
    return render_template("owner.html")


# -------------------------
# API ROUTES
# -------------------------

@app.route("/ping")
def ping():
    """Health check"""
    return jsonify({"status": "ok", "message": "pong"})


@app.route("/create-agency", methods=["POST", "OPTIONS"])
def create_agency():
    """Create new agency"""
    if request.method == "OPTIONS":
        return "", 200
    
    data = request.json
    
    # Validation
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
    
    # Set default password
    agency.set_password("admin123")
    
    db.session.add(agency)
    db.session.commit()
    
    return jsonify({
        "agency_id": agency.id,
        "message": "Agency created successfully"
    })


@app.route("/agencies")
def get_agencies():
    """Get all agencies (for owner panel)"""
    agencies = Agency.query.all()
    
    return jsonify([{
        "id": a.id,
        "name": a.name,
        "email": a.email,
        "status": a.status,
        "created_at": a.created_at.isoformat()
    } for a in agencies])


@app.route("/delete-agency/<int:agency_id>", methods=["DELETE"])
def delete_agency(agency_id):
    """Delete agency and all its leads"""
    agency = Agency.query.get(agency_id)
    
    if not agency:
        return jsonify({"error": "Agency not found"}), 404
    
    # Delete all leads first
    Lead.query.filter_by(agency_id=agency_id).delete()
    
    # Delete agency
    db.session.delete(agency)
    db.session.commit()
    
    return jsonify({"message": "Agency deleted"})


@app.route("/agency/<int:agency_id>")
def agency_info(agency_id):
    """Get agency info for widget"""
    agency = Agency.query.get(agency_id)
    
    if not agency:
        return jsonify({"error": "Invalid agency ID"}), 404
    
    return jsonify({
        "name": agency.name,
        "assistant": agency.assistant_name or "AI Assistant"
    })


@app.route("/chat", methods=["POST", "OPTIONS"])
def chat():
    """Chat endpoint with intelligent lead detection and AI summary"""
    if request.method == "OPTIONS":
        return "", 200
    
    try:
        data = request.get_json(force=True)
        
        user_message = data.get("message")
        agency_id = int(data.get("agency_id"))
        
        if not user_message:
            return jsonify({"error": "Message required"}), 400
        
        agency = Agency.query.get(agency_id)
        if not agency:
            return jsonify({"error": "Invalid agency ID"}), 400
        
        # System prompt
        system_prompt = f"""
You are {agency.assistant_name}, a professional luxury real estate sales assistant.

Rules:
- Reply in 1-2 short sentences max
- Ask ONE question at a time
- Be friendly and conversational
- Gradually collect: name, budget, location preference, email, phone
- Never repeat questions
- If user gives info, acknowledge and ask next question naturally
- Sound human, not robotic
"""
        
        # Conversation memory
        if agency_id not in conversation_memory:
            conversation_memory[agency_id] = []
        
        history = conversation_memory[agency_id]
        history.append({"role": "user", "content": user_message})
        
        # Build messages (last 10 for context)
        messages = [{"role": "system", "content": system_prompt}] + history[-10:]
        
        # Call OpenAI
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            temperature=0.7,
            max_tokens=150
        )
        
        ai_reply = response.choices[0].message.content
        history.append({"role": "assistant", "content": ai_reply})
        
        # LEAD DETECTION - Improved regex
        email_match = re.search(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", user_message)
        phone_match = re.search(r"\+?\d[\d\s\-\(\)]{7,}\d", user_message)
        budget_match = re.search(r"\$?\d+[\d,]*\.?\d*\s?(million|m|k|thousand|lakh|crore|pkr|rs)?\b", user_message, re.IGNORECASE)
        name_match = re.search(r"(?:i am|i'm|my name is|call me|this is)\s+([A-Z][a-z]+)", user_message, re.IGNORECASE)
        
        # Save lead if ANY valuable info detected OR conversation has meaningful content
        if email_match or phone_match or budget_match or name_match or len(history) >= 6:
            
            # Generate AI summary of the conversation
            ai_summary = generate_lead_summary(history)
            
            lead = Lead(
                agency_id=agency_id,
                name=name_match.group(1) if name_match else None,
                email=email_match.group(0) if email_match else None,
                phone=phone_match.group(0) if phone_match else None,
                budget=budget_match.group(0) if budget_match else None,
                message=ai_summary  # AI-generated intelligent summary
            )
            
            db.session.add(lead)
            db.session.commit()
            
            print(f"✅ Lead saved with AI summary: {lead.id}")
            
            # Send email notification
            send_lead_email(agency, lead)
        
        return jsonify({"reply": ai_reply})
    
    except Exception as e:
        print(f"❌ CHAT ERROR: {e}")
        return jsonify({"error": "Server error", "details": str(e)}), 500


@app.route("/export/<int:agency_id>")
def export_leads(agency_id):
    """Export leads to Excel"""
    try:
        leads = Lead.query.filter_by(agency_id=agency_id).order_by(Lead.created_at.desc()).all()
        
        # Create workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Leads"
        
        # Headers
        headers = ["Sr #", "Name", "Email", "Phone", "Budget", "Customer Insights", "Date", "Time"]
        ws.append(headers)
        
        # Style headers
        for cell in ws[1]:
            cell.font = Font(bold=True)
        
        # Add data
        for i, lead in enumerate(leads, start=1):
            ws.append([
                i,
                lead.name or "—",
                lead.email or "—",
                lead.phone or "—",
                lead.budget or "—",
                lead.message or "—",  # AI-generated summary
                lead.created_at.strftime('%Y-%m-%d') if lead.created_at else "—",
                lead.created_at.strftime('%H:%M:%S') if lead.created_at else "—"
            ])
        
        # Auto-adjust column widths
        for column in ws.columns:
            max_length = 0
            column_letter = column[0].column_letter
            for cell in column:
                try:
                    if len(str(cell.value)) > max_length:
                        max_length = len(cell.value)
                except:
                    pass
            adjusted_width = min(max_length + 2, 50)
            ws.column_dimensions[column_letter].width = adjusted_width
        
        # Save to buffer
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
# DATABASE INITIALIZATION
# -------------------------
with app.app_context():
    db.create_all()
    print("✅ Database initialized")


# -------------------------
# RUN APP
# -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)