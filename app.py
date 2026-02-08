from flask import Flask, request, jsonify, render_template, Response
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
# DATABASE MODELS
# -------------------------

class Agency(db.Model):
    id = db.Column(db.Integer, primary_key=True)

    # Basic
    name = db.Column(db.String(100))
    prompt = db.Column(db.Text)

    # Owner Info
    owner_name = db.Column(db.String(100))
    email = db.Column(db.String(150))
    whatsapp = db.Column(db.String(50))

    # Subscription
    subscription_type = db.Column(db.String(50))  # Basic / Pro / Premium
    status = db.Column(db.String(50), default="Pending")  # Pending / Active / Expired

    # Dates
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
@app.route("/create-agency", methods=["POST", "OPTIONS"])
def create_agency():
    if request.method == "OPTIONS":
        return "", 200

    data = request.json

    agency = Agency(
        name=data.get("name"),
        prompt=data.get("prompt"),
        owner_name=data.get("owner_name"),
        email=data.get("email"),
        whatsapp=data.get("whatsapp"),
        subscription_type=data.get("subscription_type"),
        status="Pending"
    )

    db.session.add(agency)
    db.session.commit()

    return jsonify({
        "message": "Agency created",
        "agency_id": agency.id
    })


# ---------- AGENCY INFO ----------
@app.route("/agency/<int:agency_id>")
def agency_info(agency_id):
    agency = Agency.query.get(agency_id)
    if not agency:
        return jsonify({"error": "Invalid agency ID"}), 404
    return jsonify({"name": agency.name})

# ---------- CHAT ----------
@app.route("/chat", methods=["POST", "OPTIONS"])
def chat():
    if request.method == "OPTIONS":
        return "", 200

    try:
        data = request.get_json(force=True)

        user_message = data.get("message")
        agency_id = int(data.get("agency_id"))

        agency = Agency.query.get(agency_id)
        if not agency:
            return jsonify({"error": "Invalid agency ID"}), 400

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": agency.prompt},
                {"role": "user", "content": user_message}
            ]
        )

        ai_reply = response.choices[0].message.content

        # -------------------------
        # ADVANCED LEAD DETECTION
        # -------------------------
        email_match = re.search(r"\S+@\S+\.\S+", user_message)
        phone_match = re.search(r"\+?\d[\d\s\-]{7,}\d", user_message)
        budget_match = re.search(r"\b\d+(\.\d+)?\s?(m|million|k)?\b", user_message, re.IGNORECASE)
        name_match = re.search(r"(?:i am|i'm|my name is)\s+([A-Za-z]+)", user_message, re.IGNORECASE)

        if email_match or phone_match or name_match or budget_match:
            lead = Lead(
                agency_id=agency_id,
                name=name_match.group(1) if name_match else None,
                email=email_match.group(0) if email_match else None,
                phone=phone_match.group(0) if phone_match else None,
                budget=budget_match.group(0) if budget_match else None,
                message=user_message
            )
            db.session.add(lead)
            db.session.commit()

        return jsonify({"reply": ai_reply})

    except Exception as e:
        print("CHAT ERROR:", e)
        return jsonify({"error": "Server error"}), 500

# ---------- ADMIN DASHBOARD ----------
@app.route("/admin/<int:agency_id>")
def admin_dashboard(agency_id):
    leads = Lead.query.filter_by(agency_id=agency_id).all()
    return render_template("admin.html", leads=leads)

@app.route("/owner-login", methods=["GET","POST"])
def owner_login():
    if request.method == "POST":
        agency_id = request.form.get("agency_id")
        password = request.form.get("password")

        # TEMP SIMPLE PASSWORD
        if password == "1234":
            return redirect(f"/owner-dashboard/{agency_id}")

    return render_template("owner_login.html")


@app.route("/owner-dashboard/<int:agency_id>")
def owner_dashboard(agency_id):
    leads = Lead.query.filter_by(agency_id=agency_id).all()
    return render_template("admin.html", leads=leads)

# ---------- EXPORT EXCEL ----------
@app.route("/export/<int:agency_id>")
def export_leads(agency_id):
    leads = Lead.query.filter_by(agency_id=agency_id).all()

    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"

    headers = ["Sr #", "Name", "Email", "Phone", "Budget", "Message", "Date", "Time"]

    ws.append(headers)

    bold = Font(bold=True)
    border = Border(left=Side(style='thin'),
                    right=Side(style='thin'),
                    top=Side(style='thin'),
                    bottom=Side(style='thin'))

    for cell in ws[1]:
        cell.font = bold
        cell.border = border

    for i, lead in enumerate(leads, start=1):
        date = lead.created_at.strftime("%Y-%m-%d") if lead.created_at else ""
        time = lead.created_at.strftime("%H:%M") if lead.created_at else ""

        ws.append([
            i,                     # Sr #
            lead.name or "",
            lead.email or "",
            lead.phone or "",
            lead.budget or "",
            lead.message or "",
            date,
            time
        ])


    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    return Response(
        buffer,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=leads.xlsx"}
    )
@app.route("/owner")
def owner_panel():
    return render_template("owner.html")

@app.route("/agencies")
def get_agencies():
    agencies = Agency.query.all()
    return jsonify([{"id":a.id,"name":a.name} for a in agencies])

@app.route("/delete-agency/<int:id>", methods=["DELETE"])
def delete_agency(id):
    agency = Agency.query.get(id)
    if agency:
        db.session.delete(agency)
        db.session.commit()
    return jsonify({"status":"deleted"})

# -------------------------
# INIT (DEV / PROD SAFE)
# -------------------------
ENV = os.getenv("ENV", "DEV")

with app.app_context():
    if ENV == "DEV":
        db.drop_all()
    db.create_all()

