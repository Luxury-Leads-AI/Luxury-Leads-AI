from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from openai import OpenAI
from dotenv import load_dotenv
from pathlib import Path
from flask_cors import CORS
import os
import re

# -------------------------
# LOAD ENV VARIABLES (ABSOLUTE PATH)
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
    raise ValueError("OPENAI_API_KEY is not set. Check your .env file.")

client = OpenAI(api_key=api_key)

# -------------------------
# DATABASE MODELS
# -------------------------

class Agency(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    prompt = db.Column(db.Text)

class Lead(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer)
    name = db.Column(db.String(100))
    email = db.Column(db.String(100))
    phone = db.Column(db.String(50))
    budget = db.Column(db.String(50))
    location = db.Column(db.String(100))
    message = db.Column(db.Text)

# -------------------------
# ROUTES
# -------------------------

@app.route("/")
def home():
    return "Luxury Leads AI SaaS is Running"

# ---------- CREATE AGENCY ----------
@app.route("/create-agency", methods=["POST", "OPTIONS"])
def create_agency():
    data = request.json

    name = data.get("name")
    prompt = data.get("prompt")

    if not name or not prompt:
        return jsonify({"error": "Missing name or prompt"}), 400

    new_agency = Agency(name=name, prompt=prompt)
    db.session.add(new_agency)
    db.session.commit()

    return jsonify({
        "message": "Agency created successfully",
        "agency_id": new_agency.id
    })

# ---------- CHAT ----------
@app.route("/chat", methods=["POST", "OPTIONS"])
def chat():
    data = request.json

    user_message = data.get("message")
    agency_id = data.get("agency_id")

    if not user_message or not agency_id:
        return jsonify({"error": "Missing message or agency_id"}), 400

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

    # ---------- LEAD DETECTION ----------
    email_match = re.search(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", user_message)
    phone_match = re.search(r"\+?\d[\d\s\-]{7,}\d", user_message)

    if email_match or phone_match:
        new_lead = Lead(
            agency_id=agency_id,
            email=email_match.group(0) if email_match else None,
            phone=phone_match.group(0) if phone_match else None,
            message=user_message
        )
        db.session.add(new_lead)
        db.session.commit()

    return jsonify({"reply": ai_reply})

# ---------- VIEW LEADS ----------
@app.route("/leads/<int:agency_id>", methods=["GET"])
def view_leads(agency_id):
    leads = Lead.query.filter_by(agency_id=agency_id).all()

    result = []
    for lead in leads:
        result.append({
            "id": lead.id,
            "name": lead.name,
            "email": lead.email,
            "phone": lead.phone,
            "budget": lead.budget,
            "location": lead.location,
            "message": lead.message
        })

    return jsonify(result)

# ---------- AGENCY INFO ----------
@app.route("/agency/<int:agency_id>", methods=["GET", "OPTIONS"])
def agency_info(agency_id):
    agency = Agency.query.get(agency_id)

    if not agency:
        return jsonify({"error": "Invalid agency ID"}), 404

    return jsonify({
        "name": agency.name
    })

# -------------------------
# INIT DATABASE
# -------------------------

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
