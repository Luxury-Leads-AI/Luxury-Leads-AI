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
    name = db.Column(db.String(100))
    prompt = db.Column(db.Text)

class Lead(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer)
    email = db.Column(db.String(100))
    phone = db.Column(db.String(50))
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
    name = data.get("name")
    prompt = data.get("prompt")

    if not name or not prompt:
        return jsonify({"error": "Missing name or prompt"}), 400

    agency = Agency(name=name, prompt=prompt)
    db.session.add(agency)
    db.session.commit()

    return jsonify({"agency_id": agency.id})

# ---------- AGENCY INFO (ADDED) ----------
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

# ---------- ADMIN DASHBOARD ----------
@app.route("/admin/<int:agency_id>")
def admin_dashboard(agency_id):
    leads = Lead.query.filter_by(agency_id=agency_id).all()
    return render_template("admin.html", leads=leads)

# ---------- EXPORT EXCEL ----------
@app.route("/export/<int:agency_id>")
def export_leads(agency_id):
    leads = Lead.query.filter_by(agency_id=agency_id).all()

    wb = Workbook()
    ws = wb.active
    ws.title = "Leads"

    headers = ["Email", "Phone", "Message", "Date", "Time"]
    ws.append(headers)

    bold = Font(bold=True)
    border = Border(left=Side(style='thin'),
                    right=Side(style='thin'),
                    top=Side(style='thin'),
                    bottom=Side(style='thin'))

    for cell in ws[1]:
        cell.font = bold
        cell.border = border

    for lead in leads:
        date = lead.created_at.strftime("%Y-%m-%d") if lead.created_at else ""
        time = lead.created_at.strftime("%H:%M") if lead.created_at else ""

        ws.append([
            lead.email or "",
            lead.phone or "",
            lead.message or "",
            date,
            time
        ])

    for row in ws.iter_rows():
        for cell in row:
            cell.border = border

    for col in ws.columns:
        max_length = 0
        letter = col[0].column_letter
        for cell in col:
            if cell.value:
                max_length = max(max_length, len(str(cell.value)))
        ws.column_dimensions[letter].width = max_length + 3

    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    return Response(
        buffer,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=leads.xlsx"}
    )

# -------------------------
# INIT
# -------------------------

if __name__ == "__main__":
    with app.app_context():
        db.drop_all()
        db.create_all()
    app.run(debug=True)
