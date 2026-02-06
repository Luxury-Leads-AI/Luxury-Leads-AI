import csv
from flask import Response
from flask import render_template
from flask import Flask, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from openai import OpenAI
from dotenv import load_dotenv
from pathlib import Path
from flask_cors import CORS
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
    name = db.Column(db.String(100))
    prompt = db.Column(db.Text)

class Lead(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer)
    email = db.Column(db.String(100))
    phone = db.Column(db.String(50))
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
    if request.method == "OPTIONS":
        return "", 200

    data = request.json
    name = data.get("name")
    prompt = data.get("prompt")

    if not name or not prompt:
        return jsonify({"error": "Missing name or prompt"}), 400

    agency = Agency(name=name, prompt=prompt)
    db.session.add(agency)
    db.session.commit()

    return jsonify({"agency_id": agency.id})

# ---------- CHAT ----------
@app.route("/chat", methods=["POST", "OPTIONS"])
def chat():
    if request.method == "OPTIONS":
        return "", 200

    try:
        data = request.get_json(force=True)

        user_message = data.get("message")
        agency_id = int(data.get("agency_id"))

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

        # Lead detection
        email = re.search(r"\S+@\S+\.\S+", user_message)
        phone = re.search(r"\+?\d[\d\s\-]{7,}\d", user_message)

        if email or phone:
            lead = Lead(
                agency_id=agency_id,
                email=email.group(0) if email else None,
                phone=phone.group(0) if phone else None,
                message=user_message
            )
            db.session.add(lead)
            db.session.commit()

        return jsonify({"reply": ai_reply})

    except Exception as e:
        print("CHAT ERROR:", e)
        return jsonify({"error": "Server error"}), 500

    # ---------- LEAD CAPTURE ----------
    email = re.search(r"\S+@\S+\.\S+", user_message)
    phone = re.search(r"\+?\d[\d\s\-]{7,}\d", user_message)

    if email or phone:
        lead = Lead(
            agency_id=agency_id,
            email=email.group(0) if email else None,
            phone=phone.group(0) if phone else None,
            message=user_message
        )
        db.session.add(lead)
        db.session.commit()

    return jsonify({"reply": ai_reply})

# ---------- VIEW LEADS ----------
@app.route("/leads/<int:agency_id>", methods=["GET"])
def view_leads(agency_id):
    leads = Lead.query.filter_by(agency_id=agency_id).all()
    return jsonify([
        {
            "email": l.email,
            "phone": l.phone,
            "message": l.message
        } for l in leads
    ])

# ---------- AGENCY INFO ----------
@app.route("/agency/<int:agency_id>", methods=["GET", "OPTIONS"])
def agency_info(agency_id):
    if request.method == "OPTIONS":
        return "", 200

    agency = Agency.query.get(agency_id)
    if not agency:
        return jsonify({"error": "Invalid agency ID"}), 404

    return jsonify({"name": agency.name})
@app.route("/admin/<int:agency_id>")
def admin_dashboard(agency_id):
    leads = Lead.query.filter_by(agency_id=agency_id).all()
    return render_template("admin.html", leads=leads)
@app.route("/export/<int:agency_id>")
def export_leads(agency_id):
    leads = Lead.query.filter_by(agency_id=agency_id).all()

    def generate():
        data = csv.writer()
        yield "Email,Phone,Message\n"
        for lead in leads:
            yield f"{lead.email},{lead.phone},{lead.message}\n"

    return Response(generate(), mimetype="text/csv",
                    headers={"Content-Disposition":
                             "attachment;filename=leads.csv"})

# -------------------------
# INIT
# -------------------------

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)
