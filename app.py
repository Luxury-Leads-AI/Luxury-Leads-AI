from flask import Flask, request, jsonify, render_template, Response, redirect
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

# -------------------------
# LOAD ENV VARIABLES
# -------------------------
BASE_DIR = Path(__file__).resolve().parent
env_path = BASE_DIR / ".env"
load_dotenv(dotenv_path=env_path)

# -------------------------
# APP SETUP
# -------------------------
app = Flask(__name__, static_folder="static")
CORS(app, resources={r"/*": {"origins": "*"}})

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///luxury_leads.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

# -------------------------
# OPENAI CLIENT
# -------------------------
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise ValueError("OPENAI_API_KEY is not set.")

client = OpenAI(api_key=api_key)

# -------------------------
# MEMORY STORE (AI MEMORY)
# -------------------------
conversation_memory = {}

# -------------------------
# DATABASE MODELS
# -------------------------
class Agency(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    name = db.Column(db.String(100))
    prompt = db.Column(db.Text)
    assistant_name = db.Column(db.String(100), default="AI Assistant")

    owner_name = db.Column(db.String(100))
    email = db.Column(db.String(150))
    whatsapp = db.Column(db.String(50))

    subscription_type = db.Column(db.String(50))
    status = db.Column(db.String(50), default="Pending")

    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Lead(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer)
    name = db.Column(db.String(100))
    email = db.Column(db.String(100))
    phone = db.Column(db.String(50))
    budget = db.Column(db.String(50))
    message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# -------------------------
# ROUTES
# -------------------------
@app.route("/")
def home():
    return "Luxury Leads AI SaaS is Running"

# ---------- CREATE AGENCY ----------
@app.route("/create-agency", methods=["POST","OPTIONS"])
def create_agency():
    if request.method == "OPTIONS":
        return "", 200

    data = request.json

    agency = Agency(
        name=data.get("name"),
        prompt=data.get("prompt"),
        assistant_name=data.get("assistant_name","AI Assistant"),
        owner_name=data.get("owner_name"),
        email=data.get("email"),
        whatsapp=data.get("whatsapp"),
        subscription_type=data.get("subscription_type"),
        status="Pending"
    )

    db.session.add(agency)
    db.session.commit()

    return jsonify({"agency_id": agency.id})

# ---------- AGENCY INFO ----------
@app.route("/agency/<int:agency_id>")
def agency_info(agency_id):
    agency = Agency.query.get(agency_id)
    if not agency:
        return jsonify({"error":"Invalid agency ID"}),404

    return jsonify({
        "name": agency.name,
        "assistant": agency.assistant_name or "AI Assistant"
    })

# ---------- CHAT ----------
@app.route("/chat", methods=["POST","OPTIONS"])
def chat():
    if request.method == "OPTIONS":
        return "",200

    try:
        data = request.get_json(force=True)

        user_message = data.get("message")
        agency_id = int(data.get("agency_id"))

        agency = Agency.query.get(agency_id)
        if not agency:
            return jsonify({"error":"Invalid agency ID"}),400

        # ---------- SYSTEM PROMPT (SMART LEAD HUNTER) ----------
        system_prompt = f"""
You are {agency.assistant_name}, a professional luxury real estate sales assistant.

RULES:
- Reply short like WhatsApp (2–4 lines max)
- Friendly and human tone
- Never say you are AI
- Never give long essays
- Ask only ONE question each reply
- NEVER repeat a question already answered
- If name/budget/location already provided, do NOT ask again
- Gradually collect: Name, Budget, Location, Timeline, Email, Phone
- Goal = convert visitor into qualified lead
"""

        # ---------- MEMORY HANDLING ----------
        if agency_id not in conversation_memory:
            conversation_memory[agency_id] = []

        history = conversation_memory[agency_id]

        # add user message to memory
        history.append({"role": "user", "content": user_message})

        messages = [{"role": "system", "content": system_prompt}] + history[-10:]

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages
        )

        ai_reply = response.choices[0].message.content

        # add AI reply to memory
        history.append({"role": "assistant", "content": ai_reply})

        # ---------- LEAD DETECTION ----------
        email = re.search(r"\S+@\S+\.\S+", user_message)
        phone = re.search(r"\+?\d[\d\s\-]{7,}\d", user_message)
        budget = re.search(r"\b\d+(\.\d+)?\s?(m|million|k|b)?\b", user_message, re.IGNORECASE)
        name = re.search(r"(?:i am|i'm|my name is)\s+([A-Za-z]+)", user_message, re.IGNORECASE)

        if email or phone or budget or name:
            lead = Lead(
                agency_id=agency_id,
                name=name.group(1) if name else None,
                email=email.group(0) if email else None,
                phone=phone.group(0) if phone else None,
                budget=budget.group(0) if budget else None,
                message=user_message
            )
            db.session.add(lead)
            db.session.commit()

        return jsonify({"reply": ai_reply})

    except Exception as e:
        print("CHAT ERROR:", e)
        return jsonify({"error":"Server error"}),500

# ---------- ADMIN DASHBOARD ----------
@app.route("/admin/<int:agency_id>")
def admin_dashboard(agency_id):
    leads = Lead.query.filter_by(agency_id=agency_id).all()
    return render_template("admin.html", leads=leads)

# ---------- OWNER LOGIN ----------
@app.route("/owner-login", methods=["GET","POST"])
def owner_login():
    if request.method=="POST":
        agency_id = request.form.get("agency_id")
        password = request.form.get("password")

        if password=="1234" and agency_id.isdigit():
            return redirect(f"/owner-dashboard/{agency_id}")

    return render_template("owner_login.html")

@app.route("/owner-dashboard/<int:agency_id>")
def owner_dashboard(agency_id):
    leads = Lead.query.filter_by(agency_id=agency_id).all()
    return render_template("admin.html", leads=leads)

@app.route("/signup")
def signup():
    return render_template("signup.html")

# ---------- EXPORT EXCEL ----------
@app.route("/export/<int:agency_id>")
def export_leads(agency_id):
    leads = Lead.query.filter_by(agency_id=agency_id).all()

    wb = Workbook()
    ws = wb.active
    ws.title="Leads"

    headers=["Sr #","Name","Email","Phone","Budget","Message","Date","Time"]
    ws.append(headers)

    bold=Font(bold=True)
    border=Border(left=Side(style='thin'),right=Side(style='thin'),
                  top=Side(style='thin'),bottom=Side(style='thin'))

    for cell in ws[1]:
        cell.font=bold
        cell.border=border

    for i,lead in enumerate(leads,start=1):
        date = lead.created_at.strftime("%Y-%m-%d") if lead.created_at else ""
        time = lead.created_at.strftime("%H:%M") if lead.created_at else ""

        ws.append([
            i,
            lead.name or "",
            lead.email or "",
            lead.phone or "",
            lead.budget or "",
            lead.message or "",
            date,
            time
        ])

    buffer=BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    return Response(buffer,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition":"attachment; filename=leads.xlsx"}
    )

# ---------- OWNER PANEL ----------
@app.route("/owner")
def owner_panel():
    return render_template("owner.html")

# -------------------------
# INIT SAFE
# -------------------------
ENV = os.getenv("ENV","DEV")

with app.app_context():
    if ENV=="DEV":
        db.drop_all()
    db.create_all()
