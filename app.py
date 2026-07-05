from flask import Flask, request, jsonify, render_template, Response, redirect, session
from flask_sqlalchemy import SQLAlchemy
from openai import OpenAI
from dotenv import load_dotenv
from pathlib import Path
from flask_cors import CORS
from datetime import datetime, timedelta
from openpyxl import Workbook
from openpyxl.styles import Font
from io import BytesIO, StringIO
import os
import re
import json
import csv
import smtplib
from email.mime.text import MIMEText
from werkzeug.security import generate_password_hash, check_password_hash
import hashlib
import pytz
from collections import defaultdict

# SendGrid email imports
try:
    from sendgrid import SendGridAPIClient
    from sendgrid.helpers.mail import Mail
    SENDGRID_AVAILABLE = True
except ImportError:
    SENDGRID_AVAILABLE = False
    print("⚠️ SendGrid not installed")

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

import re as _re
app.jinja_env.filters['regex_replace'] = lambda s, find, replace: _re.sub(find, replace, s)

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
# MEMORY STORE
# -------------------------
conversation_memory = {}
session_timestamps = {}

# -------------------------
# EMAIL CONFIG
# -------------------------
SMTP_EMAIL = os.getenv("SMTP_EMAIL")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")


def clean_whatsapp_number(number):
    if not number:
        return None
    cleaned = re.sub(r'\D', '', number)
    cleaned = cleaned.lstrip('0')
    return cleaned if len(cleaned) >= 9 else None


def get_listings_context(agency_id):
    try:
        listings = Listing.query.filter_by(
            agency_id=agency_id,
            status='available'
        ).order_by(Listing.price_numeric.asc()).all()

        if not listings:
            return ""

        lines = ["\n\nAVAILABLE PROPERTIES AT YOUR AGENCY (use these when recommending):"]
        for i, l in enumerate(listings, 1):
            bed_bath = ""
            if l.bedrooms:
                bed_bath += f"{l.bedrooms}bed"
            if l.bathrooms:
                bed_bath += f"/{l.bathrooms}bath"

            price_str = f"${l.price:,.0f}" if l.price else l.price_raw or "Price on request"
            features_str = f" | {l.features}" if l.features else ""
            desc_str = f" - {l.description[:80]}..." if l.description and len(l.description) > 30 else (f" - {l.description}" if l.description else "")

            lines.append(
                f"{i}. {l.title} | {l.location} | {price_str}"
                f"{' | ' + bed_bath if bed_bath else ''}"
                f"{features_str}"
                f"{desc_str}"
            )

        lines.append(
            "\nWhen customer mentions budget or preferences, recommend matching properties by name. "
            "Be specific: mention price, bedrooms, location. Create mild urgency naturally."
        )
        return "\n".join(lines)
    except Exception as e:
        print(f"⚠️ Listings context error: {e}")
        return ""


def send_lead_email(agency, lead):
    subject = f"🎯 New Qualified Lead for {agency.name}"
    contact_info = ""
    if lead.whatsapp_number:
        clean_num = clean_whatsapp_number(lead.whatsapp_number)
        wa_link = f"https://wa.me/{clean_num}" if clean_num else "N/A"
        contact_info = f"💬 WhatsApp: {lead.whatsapp_number}\n🔗 Click to Chat: {wa_link}"
    elif lead.phone:
        contact_info = f"📱 Phone:    {lead.phone}"
    else:
        contact_info = "📱 Phone:    Not provided"

    pref = lead.contact_preference or 'email'
    pref_display = {
        'email': 'Email', 'whatsapp': 'WhatsApp', 'phone': 'Phone',
        'email_and_whatsapp': 'Email & WhatsApp', 'email_and_phone': 'Email & Phone'
    }.get(pref, pref.title())

    body = f"""
New QUALIFIED Lead Received from {agency.name}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
👤 Name:    {lead.name or 'Not provided'}
📧 Email:   {lead.email or 'Not provided'}
{contact_info}
💰 Budget:  {lead.budget or 'Not provided'}
📞 Prefers: {pref_display}

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
    if SENDGRID_API_KEY and SENDGRID_AVAILABLE:
        try:
            print(f"📧 Sending via SendGrid to: {agency.email}")
            message = Mail(from_email=SMTP_EMAIL, to_emails=agency.email,
                           subject=subject, plain_text_content=body)
            sg = SendGridAPIClient(SENDGRID_API_KEY)
            response = sg.send(message)
            print(f"✅ EMAIL SENT via SendGrid (Status: {response.status_code})")
            return True
        except Exception as e:
            print(f"⚠️ SendGrid failed: {e}")
            return False
    else:
        print("⚠️ SendGrid not configured")
        return False


def send_appointment_confirmation(agency, appointment):
    SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
    customer_subject = f"✅ Appointment Confirmed - {agency.name}"
    customer_body = f"""
Dear {appointment.customer_name},

Your property viewing appointment has been confirmed!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🏠 Agency:       {agency.name}
📅 Date:         {appointment.appointment_date}
🕐 Time:         {appointment.appointment_time}
🏡 Property:     {appointment.property_interest or 'To be discussed'}
📋 Status:       Confirmed
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

We look forward to meeting you!

Best regards,
{agency.name} Team
"""
    agency_subject = f"📅 New Appointment Booked - {appointment.customer_name}"
    agency_body = f"""
New Appointment Booked!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
👤 Customer:     {appointment.customer_name}
📧 Email:        {appointment.customer_email}
📅 Date:         {appointment.appointment_date}
🕐 Time:         {appointment.appointment_time}
🏡 Interested In: {appointment.property_interest or 'General viewing'}
📋 Status:       {appointment.status.title()}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

View all appointments:
https://luxury-leads-ai.onrender.com/appointments/{agency.id}
"""
    if SENDGRID_API_KEY and SENDGRID_AVAILABLE:
        try:
            if appointment.customer_email:
                msg1 = Mail(from_email=SMTP_EMAIL, to_emails=appointment.customer_email,
                            subject=customer_subject, plain_text_content=customer_body)
                sg = SendGridAPIClient(SENDGRID_API_KEY)
                sg.send(msg1)
                print(f"✅ Appointment confirmation sent to: {appointment.customer_email}")
            msg2 = Mail(from_email=SMTP_EMAIL, to_emails=agency.email,
                        subject=agency_subject, plain_text_content=agency_body)
            sg = SendGridAPIClient(SENDGRID_API_KEY)
            sg.send(msg2)
            print(f"✅ Appointment notification sent to agency: {agency.email}")
            return True
        except Exception as e:
            print(f"⚠️ Appointment email failed: {e}")
            return False
    return False


def send_crm_webhook(agency, lead):
    if not agency.webhook_url:
        return
    try:
        payload = {
            "event": "lead_qualified",
            "agency_id": agency.id, "agency_name": agency.name,
            "lead": {
                "id": lead.id, "name": lead.name, "email": lead.email,
                "phone": lead.phone, "whatsapp_number": lead.whatsapp_number,
                "contact_preference": lead.contact_preference,
                "budget": lead.budget, "summary": lead.message,
                "intent_score": lead.intent_score,
                "created_at": lead.created_at.isoformat() if lead.created_at else None
            }
        }
        import httpx
        response = httpx.post(agency.webhook_url, json=payload, timeout=5)
        print(f"✅ Webhook sent (Status: {response.status_code})")
    except Exception as e:
        print(f"⚠️ Webhook failed: {e}")


def send_followup_email(agency, lead, day):
    contact = lead.whatsapp_number or lead.phone or "Not provided"
    stars = "⭐" * (lead.intent_score or 1)
    if day == 1:
        subject = f"⏰ Day 1 Follow-up: {lead.name or 'New Lead'} | {agency.name}"
        body = f"""
Hi {agency.owner_name or agency.name},

Time to follow up with your qualified lead from yesterday!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
👤 Name:    {lead.name or '—'}
📧 Email:   {lead.email or '—'}
📱 Contact: {contact}
💰 Budget:  {lead.budget or '—'}
🌟 Quality: {stars} ({lead.intent_score}/5)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💡 Customer Insights:
{lead.message or 'No summary available'}

🎯 Suggested Action: Reach out via {(lead.contact_preference or 'email').title()} within 24 hours.

Login to view: https://luxury-leads-ai.onrender.com/owner-login
"""
    elif day == 7:
        subject = f"📅 7-Day Check-in: {lead.name or 'Lead'} | {agency.name}"
        body = f"""
Hi {agency.owner_name or agency.name},

It's been 7 days since {lead.name or 'this lead'} qualified. Time for a re-engagement!

━━━━━━━━━━━━━━━━━━━━━━━━━━━━
👤 Name:    {lead.name or '—'}
📧 Email:   {lead.email or '—'}
📱 Contact: {contact}
💰 Budget:  {lead.budget or '—'}
🌟 Quality: {stars} ({lead.intent_score}/5)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💬 Re-engagement script:
"Hey {lead.name or 'there'}, just checking in! Have you found anything you like yet?
I have a couple of new listings that might fit what you're looking for."

Login to view: https://luxury-leads-ai.onrender.com/owner-login
"""
    else:
        return False

    SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
    if SENDGRID_API_KEY and SENDGRID_AVAILABLE:
        try:
            message = Mail(from_email=SMTP_EMAIL, to_emails=agency.email,
                           subject=subject, plain_text_content=body)
            sg = SendGridAPIClient(SENDGRID_API_KEY)
            response = sg.send(message)
            print(f"✅ Follow-up Day {day} sent (Status: {response.status_code})")
            return True
        except Exception as e:
            print(f"⚠️ Follow-up Day {day} failed: {e}")
            return False
    return False


def process_pending_followups():
    try:
        now = datetime.utcnow()
        day1_count = 0
        day7_count = 0
        day1_cutoff = now - timedelta(hours=24)
        day1_leads = Lead.query.filter(Lead.follow_up_1_sent == 0, Lead.created_at <= day1_cutoff).all()
        for lead in day1_leads:
            agency = db.session.get(Agency, lead.agency_id)
            if agency and send_followup_email(agency, lead, 1):
                lead.follow_up_1_sent = 1
                day1_count += 1
        db.session.commit()
        day7_cutoff = now - timedelta(days=7)
        day7_leads = Lead.query.filter(Lead.follow_up_7_sent == 0, Lead.created_at <= day7_cutoff).all()
        for lead in day7_leads:
            agency = db.session.get(Agency, lead.agency_id)
            if agency and send_followup_email(agency, lead, 7):
                lead.follow_up_7_sent = 1
                day7_count += 1
        db.session.commit()
        print(f"✅ Follow-ups processed: D1={day1_count}, D7={day7_count}")
        return {"day1": day1_count, "day7": day7_count}
    except Exception as e:
        print(f"⚠️ Follow-up error: {e}")
        db.session.rollback()
        return {"error": str(e)}


def clean_expired_sessions():
    try:
        current_time = datetime.utcnow()
        expired_keys = [
            key for key in list(session_timestamps.keys())
            if (current_time - session_timestamps[key]).total_seconds() > 1800
        ]
        for key in expired_keys:
            conversation_memory.pop(key, None)
            session_timestamps.pop(key, None)
            print(f"🧹 Expired session cleared: {key}")
    except Exception as e:
        print(f"⚠️ Session cleanup error: {e}")


def generate_lead_summary(conversation_history, agency_name):
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
            temperature=0.3, max_tokens=120
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        print(f"❌ Summary error: {e}")
        return "Customer engaged in property conversation."


def extract_name_from_context(conversation_history):
    not_a_name = {
        'yes', 'no', 'ok', 'okay', 'sure', 'fine', 'good', 'great',
        'hello', 'hi', 'hey', 'thanks', 'thank', 'please', 'sorry',
        'email', 'phone', 'whatsapp', 'call', 'text', 'message',
        'looking', 'interested', 'want', 'need', 'like', 'going',
        'villa', 'house', 'apartment', 'property', 'condo', 'flat', 'home',
        'beach', 'miami', 'malibu', 'florida', 'california', 'usa',
        'within', 'about', 'around', 'budget', 'price', 'cost',
        'month', 'week', 'year', 'soon', 'asap', 'later', 'today',
        'just', 'also', 'here', 'there', 'then', 'when', 'where',
        'what', 'how', 'why', 'who', 'which', 'that', 'this', 'with',
        'from', 'have', 'been', 'will', 'would', 'could', 'should',
        'south', 'north', 'east', 'west', 'central', 'downtown',
        'coconut', 'grove', 'hilton', 'santa', 'monica', 'myrtle',
        'asking', 'checking', 'getting', 'making', 'looking', 'trying'
    }
    name_question_patterns = [
        "what's your name", "what is your name", "whats your name",
        "your name?", "may i have your name", "can i get your name",
        "could i get your name", "mind sharing your name",
        "first name", "tell me your name", "know your name"
    ]
    for i, msg in enumerate(conversation_history):
        if msg['role'] == 'assistant':
            ai_text = msg['content'].lower()
            if any(pattern in ai_text for pattern in name_question_patterns):
                if i + 1 < len(conversation_history):
                    next_msg = conversation_history[i + 1]
                    if next_msg['role'] == 'user':
                        candidate = next_msg['content'].strip()
                        candidate = re.sub(
                            r'^(i\s+am|i\'m|my\s+name\s+is|name\s+is|it\'s|its|call\s+me|this\s+is)\s+',
                            '', candidate, flags=re.IGNORECASE).strip()
                        first_word = candidate.split()[0] if candidate.split() else ''
                        if (first_word and re.match(r'^[a-zA-Z]{2,30}$', first_word)
                                and first_word.lower() not in not_a_name):
                            print(f"✅ Name (context): {first_word.title()}")
                            return first_word.title()

    explicit_pattern = r'(?:i\s+am|i\'m|my\s+name\s+is|name\s+is|call\s+me|this\s+is)\s+([a-zA-Z]{2,30})(?:\s|[.,!?]|$)'
    found_names = []
    for msg in conversation_history:
        if msg['role'] == 'user':
            for match in re.finditer(explicit_pattern, msg['content'], re.IGNORECASE):
                candidate = match.group(1).strip()
                if re.match(r'^[a-zA-Z]{2,30}$', candidate) and candidate.lower() not in not_a_name:
                    found_names.append(candidate.title())
    if found_names:
        print(f"✅ Name (explicit): {found_names[-1]}")
        return found_names[-1]
    print("⚠️ Name: Not found")
    return None


def extract_lead_data(conversation_history):
    full_conversation_user = " ".join([
        msg['content'] for msg in conversation_history if msg['role'] == 'user'
    ])
    lead_data = {
        'name': None, 'email': None, 'phone': None,
        'whatsapp_number': None, 'contact_preference': 'email', 'budget': None
    }
    email_match = re.search(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b", full_conversation_user)
    if email_match:
        lead_data['email'] = email_match.group(0)

    lead_data['name'] = extract_name_from_context(conversation_history)

    contact_question_patterns = [
        "best way to reach you", "how can i reach you", "reach you",
        "contact you", "whatsapp, phone, or email", "phone, or email"
    ]
    for i, msg in enumerate(conversation_history):
        if msg['role'] == 'assistant':
            ai_text = msg['content'].lower()
            if any(pattern in ai_text for pattern in contact_question_patterns):
                if i + 1 < len(conversation_history):
                    next_msg = conversation_history[i + 1]
                    if next_msg['role'] == 'user':
                        user_pref = next_msg['content'].lower()
                        has_email = 'email' in user_pref
                        has_whatsapp = 'whatsapp' in user_pref or 'wa' in user_pref
                        has_phone = 'phone' in user_pref or 'call' in user_pref
                        if has_email and has_whatsapp:
                            lead_data['contact_preference'] = 'email_and_whatsapp'
                        elif has_email and has_phone:
                            lead_data['contact_preference'] = 'email_and_phone'
                        elif has_whatsapp:
                            lead_data['contact_preference'] = 'whatsapp'
                        elif has_phone:
                            lead_data['contact_preference'] = 'phone'
                        elif has_email:
                            lead_data['contact_preference'] = 'email'
                        break

    whatsapp_keywords = ['whatsapp', 'whats app']
    mentions_whatsapp = any(kw in full_conversation_user.lower() for kw in whatsapp_keywords)
    if lead_data['contact_preference'] in ('whatsapp', 'email_and_whatsapp'):
        mentions_whatsapp = True

    phone_patterns = [
        r"\+\d{1,4}[\s\-]?\d{2,4}[\s\-]?\d{3,4}[\s\-]?\d{2,4}",
        r"\+?\d{9,15}", r"\d{3}[\s\-]?\d{3}[\s\-]?\d{3,4}",
    ]
    for pattern in phone_patterns:
        phone_match = re.search(pattern, full_conversation_user)
        if phone_match:
            phone = phone_match.group(0).strip()
            clean = phone.replace('+', '').replace('-', '').replace(' ', '')
            if len(clean) >= 9:
                if mentions_whatsapp:
                    lead_data['whatsapp_number'] = phone
                    print(f"✅ WhatsApp: {phone}")
                else:
                    lead_data['phone'] = phone
                    print(f"✅ Phone: {phone}")
                break

    budget_patterns = [
        r"(\d+(?:\.\d+)?)\s*([MmKk])(?![a-zA-Z])\s*(?:\$|dollars?)?",   # M/K must not be part of a word (fixes "1 month")
        r"[\$]\s*(\d+(?:\.\d+)?)\s*([MmKk](?![a-zA-Z])|million|thousand)?",
        r"(\d+(?:\.\d+)?)\s*(million|thousand|lakh|crore)\s*(?:\$|dollars?|usd|aed)?",
        r"(?:budget|price|around|afford)\s*[\$]?(\d+(?:\.\d+)?)\s*([MmKk](?![a-zA-Z])|million|thousand)?",
    ]
    for pattern in budget_patterns:
        budget_match = re.search(pattern, full_conversation_user, re.IGNORECASE)
        if budget_match:
            amount = budget_match.group(1)
            unit = budget_match.group(2) if len(budget_match.groups()) > 1 and budget_match.group(2) else ''
            if unit:
                unit = unit.lower()
                if unit in ['m', 'million']: unit = 'million'
                elif unit in ['k', 'thousand']: unit = 'thousand'
            currency = ''
            if '$' in full_conversation_user or 'dollar' in full_conversation_user.lower():
                currency = 'USD'
            elif 'aed' in full_conversation_user.lower():
                currency = 'AED'
            lead_data['budget'] = f"{amount} {unit} {currency}".strip() if unit else f"{amount} {currency}".strip()
            print(f"✅ Budget: {lead_data['budget']}")
            break
    return lead_data


def extract_appointment_data(conversation_history):
    full_text = " ".join([msg['content'] for msg in conversation_history]).lower()
    appointment_data = {'day': None, 'time': None, 'property_interest': None, 'requested': False}
    booking_keywords = [
        'schedule', 'appointment', 'viewing', 'visit', 'see the property',
        'book a visit', 'arrange a viewing', 'show me', 'can i see',
        'i would like to see', 'i want to see', 'visit the property'
    ]
    appointment_data['requested'] = any(kw in full_text for kw in booking_keywords)
    days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday',
            'tomorrow', 'today', 'weekend', 'this week', 'next week']
    for day in days:
        if day in full_text:
            appointment_data['day'] = day.title()
            break
    time_patterns = [
        (r'\b(10\s*am|10\s*o\'?clock)\b', '10:00 AM'),
        (r'\b(12\s*pm|noon|12\s*o\'?clock)\b', '12:00 PM'),
        (r'\b(2\s*pm|2\s*o\'?clock)\b', '2:00 PM'),
        (r'\b(4\s*pm|4\s*o\'?clock)\b', '4:00 PM'),
        (r'\b(6\s*pm|6\s*o\'?clock)\b', '6:00 PM'),
        (r'\bmorning\b', '10:00 AM'),
        (r'\b(afternoon|midday)\b', '2:00 PM'),
        (r'\b(evening|late afternoon)\b', '4:00 PM'),
    ]
    for pattern, time_label in time_patterns:
        if re.search(pattern, full_text):
            appointment_data['time'] = time_label
            break
    return appointment_data


def contact_step_completed(conversation_history):
    contact_question_patterns = [
        "best way to reach you", "how can i reach you", "reach you",
        "contact you", "whatsapp, phone, or email", "phone, or email"
    ]
    number_question_patterns = [
        "whatsapp number", "phone number", "your number",
        "share your number", "what's your"
    ]
    asked_contact_pref = False
    asked_number = False
    gave_number = False
    user_said_email_only = False
    user_declined_number = False

    for i, msg in enumerate(conversation_history):
        if msg['role'] == 'assistant':
            ai_text = msg['content'].lower()
            if any(p in ai_text for p in contact_question_patterns):
                asked_contact_pref = True
                if i + 1 < len(conversation_history):
                    next_msg = conversation_history[i + 1]
                    if next_msg['role'] == 'user':
                        user_text = next_msg['content'].lower()
                        if 'email' in user_text and 'whatsapp' not in user_text and 'phone' not in user_text:
                            user_said_email_only = True
            if any(p in ai_text for p in number_question_patterns):
                asked_number = True
                if i + 1 < len(conversation_history):
                    next_msg = conversation_history[i + 1]
                    if next_msg['role'] == 'user':
                        user_text = next_msg['content'].strip()
                        if re.search(r'\+?\d{9,15}', user_text.replace(' ', '').replace('-', '')):
                            gave_number = True
                        decline_words = ['no', 'nope', 'skip', 'pass', 'later', 'not now', "don't", 'prefer not']
                        if any(w in user_text.lower() for w in decline_words):
                            user_declined_number = True

    if user_said_email_only: return True
    if asked_number and (gave_number or user_declined_number): return True
    user_msg_count = len([m for m in conversation_history if m['role'] == 'user'])
    if asked_contact_pref and user_msg_count >= 10: return True
    return False


def detect_objection(user_message):
    user_message_lower = user_message.lower()

    # If message contains a number/amount, user is GIVING information, not objecting
    if re.search(r'\d', user_message):
        return None

    objections = {
        'price': ['expensive', 'too much', 'costly', "can't afford", 'cannot afford', 'high price', 'over budget', 'out of my budget'],
        'timing': ['not ready', 'not sure', 'need time', 'thinking about it', 'maybe later', 'unsure'],
        'indecision': ['torn', 'confused', 'cant decide', "can't decide"],
        'trust': ['scam', 'legit', 'is this real', 'can i trust', 'safe', 'reliable']
    }
    for objection_type, keywords in objections.items():
        if any(keyword in user_message_lower for keyword in keywords):
            return objection_type
    return None


def generate_objection_response(objection_type, agency_name):
    responses = {
        'price': "I hear you – budget is key. Even a rough range helps me point you in the right direction. What feels comfortable for you?",
        'timing': "Totally fair! No pressure at all. What's the main thing making you hesitant right now?",
        'indecision': "I get that – it's a big decision! Let's try this: if you had to pick just ONE thing that matters most to you, what would it be?",
        'trust': f"I understand the concern. {agency_name} is a licensed real estate agency. Would you like to know more about us, or would you prefer to just explore properties for now?"
    }
    return responses.get(objection_type, None)


def analyze_lead_quality(lead_data, conversation_history):
    score = 1
    has_name = bool(lead_data.get('name'))
    has_phone = bool(lead_data.get('phone') or lead_data.get('whatsapp_number'))
    has_budget = bool(lead_data.get('budget'))
    if has_name: score += 1
    if has_budget: score += 1
    if has_phone: score += 1
    full_text = " ".join([msg['content'].lower() for msg in conversation_history if msg['role'] == 'user'])
    urgency = ['asap', 'urgent', 'soon', 'quickly', 'this week', 'this month', 'within', 'month', 'week']
    if any(kw in full_text for kw in urgency):
        score = min(score + 1, 5)
    print(f"📊 Quality: Name={has_name}, Phone={has_phone}, Budget={has_budget} → {score}/5")
    return min(score, 5)


def is_lead_qualified(lead_data, conversation_history):
    has_email = bool(lead_data.get('email'))
    has_name = bool(lead_data.get('name'))
    has_budget = bool(lead_data.get('budget'))
    message_count = len([msg for msg in conversation_history if msg['role'] == 'user'])
    contact_done = contact_step_completed(conversation_history)
    is_qualified = (has_email and has_name and has_budget and message_count >= 7 and contact_done)
    if is_qualified:
        print(f"✅ QUALIFIED: Email={has_email}, Name={has_name}, Budget={has_budget}, Msgs={message_count}, ContactDone={contact_done}")
    else:
        print(f"⚠️ Not yet: Email={has_email}, Name={has_name}, Budget={has_budget}, Msgs={message_count}/7, ContactDone={contact_done}")
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
    webhook_url = db.Column(db.String(500))
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
    lead_status = db.Column(db.String(20), default='new')
    notes = db.Column(db.Text, default='[]')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(pytz.timezone('Asia/Karachi')))
    follow_up_1_sent = db.Column(db.Integer, default=0)
    follow_up_7_sent = db.Column(db.Integer, default=0)


class Appointment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer, nullable=False)
    lead_id = db.Column(db.Integer, nullable=True)
    customer_name = db.Column(db.String(100))
    customer_email = db.Column(db.String(150))
    appointment_date = db.Column(db.String(100))
    appointment_time = db.Column(db.String(50))
    property_interest = db.Column(db.String(200))
    status = db.Column(db.String(20), default='pending')
    notes = db.Column(db.Text, default='')
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(pytz.timezone('Asia/Karachi')))


class Listing(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    agency_id = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(200), nullable=False)
    location = db.Column(db.String(200))
    price_raw = db.Column(db.String(100))
    price = db.Column(db.Float, nullable=True)
    price_numeric = db.Column(db.Float, nullable=True)
    bedrooms = db.Column(db.Integer, nullable=True)
    bathrooms = db.Column(db.Integer, nullable=True)
    property_type = db.Column(db.String(50))
    features = db.Column(db.String(500))
    description = db.Column(db.Text)
    status = db.Column(db.String(20), default='available')
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
        "id": a.id, "name": a.name,
        "assistant_name": a.assistant_name or "AI Assistant",
        "owner_name": a.owner_name or "—",
        "email": a.email, "status": a.status,
        "created_at": a.created_at.isoformat()
    } for a in agencies])

@app.route("/delete-agency/<int:agency_id>", methods=["DELETE"])
def delete_agency(agency_id):
    agency = db.session.get(Agency, agency_id)
    if not agency:
        return jsonify({"error": "Agency not found"}), 404
    Lead.query.filter_by(agency_id=agency_id).delete()
    Appointment.query.filter_by(agency_id=agency_id).delete()
    Listing.query.filter_by(agency_id=agency_id).delete()
    db.session.delete(agency)
    db.session.commit()
    return jsonify({"message": "Agency deleted"})

@app.route("/agency/<int:agency_id>")
def agency_info(agency_id):
    agency = db.session.get(Agency, agency_id)
    if not agency:
        return jsonify({"error": "Invalid agency ID"}), 404
    return jsonify({"name": agency.name, "assistant": agency.assistant_name or "AI Assistant"})


# ─────────────────────────────────────────────────────
# PHASE 2B ROUTES
# ─────────────────────────────────────────────────────

@app.route("/update-lead-status/<int:lead_id>", methods=["POST"])
def update_lead_status(lead_id):
    try:
        lead = db.session.get(Lead, lead_id)
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        data = request.get_json(force=True)
        new_status = data.get("status", "new")
        if new_status not in ['new', 'contacted', 'meeting', 'closed', 'lost']:
            return jsonify({"error": "Invalid status"}), 400
        lead.lead_status = new_status
        db.session.commit()
        return jsonify({"success": True, "status": new_status})
    except Exception as e:
        return jsonify({"error": "Failed to update status"}), 500


@app.route("/add-lead-note/<int:lead_id>", methods=["POST"])
def add_lead_note(lead_id):
    try:
        lead = db.session.get(Lead, lead_id)
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        data = request.get_json(force=True)
        note_text = data.get("note", "").strip()
        if not note_text:
            return jsonify({"error": "Note cannot be empty"}), 400
        try:
            notes = json.loads(lead.notes or '[]')
        except:
            notes = []
        new_note = {
            "id": len(notes) + 1,
            "text": note_text,
            "timestamp": datetime.now(pytz.timezone('Asia/Karachi')).strftime('%B %d, %Y at %I:%M %p')
        }
        notes.append(new_note)
        lead.notes = json.dumps(notes)
        db.session.commit()
        return jsonify({"success": True, "note": new_note, "total_notes": len(notes)})
    except Exception as e:
        return jsonify({"error": "Failed to add note"}), 500


@app.route("/delete-lead-note/<int:lead_id>/<int:note_id>", methods=["DELETE"])
def delete_lead_note(lead_id, note_id):
    try:
        lead = db.session.get(Lead, lead_id)
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        try:
            notes = json.loads(lead.notes or '[]')
        except:
            notes = []
        notes = [n for n in notes if n.get('id') != note_id]
        lead.notes = json.dumps(notes)
        db.session.commit()
        return jsonify({"success": True, "total_notes": len(notes)})
    except Exception as e:
        return jsonify({"error": "Failed to delete note"}), 500


@app.route("/get-lead-detail/<int:lead_id>")
def get_lead_detail(lead_id):
    try:
        lead = db.session.get(Lead, lead_id)
        if not lead:
            return jsonify({"error": "Lead not found"}), 404
        try:
            notes = json.loads(lead.notes or '[]')
        except:
            notes = []
        clean_num = clean_whatsapp_number(lead.whatsapp_number)
        wa_link = f"https://wa.me/{clean_num}" if clean_num else None
        return jsonify({
            "id": lead.id, "name": lead.name or "—",
            "email": lead.email or "—", "phone": lead.phone or None,
            "whatsapp_number": lead.whatsapp_number or None,
            "whatsapp_link": wa_link,
            "contact_preference": lead.contact_preference or "email",
            "budget": lead.budget or "—", "message": lead.message or "—",
            "intent_score": lead.intent_score or 1,
            "lead_status": lead.lead_status or "new", "notes": notes,
            "created_at": lead.created_at.strftime('%B %d, %Y at %I:%M %p') if lead.created_at else "—"
        })
    except Exception as e:
        return jsonify({"error": "Failed to get lead"}), 500


@app.route("/bulk-delete-leads", methods=["POST"])
def bulk_delete_leads():
    try:
        data = request.get_json(force=True)
        lead_ids = data.get("lead_ids", [])
        if not lead_ids:
            return jsonify({"error": "No leads selected"}), 400
        deleted = 0
        for lead_id in lead_ids:
            lead = db.session.get(Lead, int(lead_id))
            if lead:
                for key in list(conversation_memory.keys()):
                    if key.startswith(f"{lead.agency_id}_"):
                        conversation_memory.pop(key, None)
                        session_timestamps.pop(key, None)
                db.session.delete(lead)
                deleted += 1
        db.session.commit()
        return jsonify({"success": True, "deleted": deleted})
    except Exception as e:
        return jsonify({"error": "Failed to bulk delete"}), 500


# ─────────────────────────────────────────────────────
# PHASE 2C ROUTES
# ─────────────────────────────────────────────────────

@app.route("/appointments/<int:agency_id>")
def appointments(agency_id):
    agency = db.session.get(Agency, agency_id)
    if not agency:
        return redirect("/owner-login?error=Agency+not+found")
    appts = Appointment.query.filter_by(
        agency_id=agency_id
    ).order_by(Appointment.created_at.desc()).all()
    return render_template("appointments.html", agency=agency, appointments=appts)


@app.route("/book-appointment", methods=["POST"])
def book_appointment():
    try:
        data = request.get_json(force=True)
        agency_id = data.get("agency_id")
        agency = db.session.get(Agency, int(agency_id))
        if not agency:
            return jsonify({"error": "Agency not found"}), 404
        appt = Appointment(
            agency_id=int(agency_id),
            lead_id=data.get("lead_id"),
            customer_name=data.get("customer_name", ""),
            customer_email=data.get("customer_email", ""),
            appointment_date=data.get("appointment_date", ""),
            appointment_time=data.get("appointment_time", ""),
            property_interest=data.get("property_interest", ""),
            status="pending",
            notes=data.get("notes", "")
        )
        db.session.add(appt)
        db.session.commit()
        print(f"✅ Appointment booked: ID {appt.id} for {appt.customer_name}")
        send_appointment_confirmation(agency, appt)
        return jsonify({
            "success": True, "appointment_id": appt.id,
            "message": f"Appointment booked for {appt.appointment_date} at {appt.appointment_time}"
        })
    except Exception as e:
        print(f"❌ Book appointment error: {e}")
        db.session.rollback()
        return jsonify({"error": "Failed to book appointment"}), 500


@app.route("/update-appointment-status/<int:appt_id>", methods=["POST"])
def update_appointment_status(appt_id):
    try:
        appt = db.session.get(Appointment, appt_id)
        if not appt:
            return jsonify({"error": "Appointment not found"}), 404
        data = request.get_json(force=True)
        new_status = data.get("status", "pending")
        if new_status not in ['pending', 'confirmed', 'cancelled']:
            return jsonify({"error": "Invalid status"}), 400
        appt.status = new_status
        db.session.commit()
        return jsonify({"success": True, "status": new_status})
    except Exception as e:
        return jsonify({"error": "Failed to update"}), 500


@app.route("/delete-appointment/<int:appt_id>", methods=["DELETE"])
def delete_appointment(appt_id):
    try:
        appt = db.session.get(Appointment, appt_id)
        if not appt:
            return jsonify({"error": "Appointment not found"}), 404
        db.session.delete(appt)
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": "Failed to delete"}), 500


@app.route("/get-appointments-count/<int:agency_id>")
def get_appointments_count(agency_id):
    try:
        total = Appointment.query.filter_by(agency_id=agency_id).count()
        pending = Appointment.query.filter_by(agency_id=agency_id, status='pending').count()
        confirmed = Appointment.query.filter_by(agency_id=agency_id, status='confirmed').count()
        return jsonify({"total": total, "pending": pending, "confirmed": confirmed})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────
# PHASE 2D ROUTES - PROPERTY LISTINGS
# ─────────────────────────────────────────────────────

def parse_price(price_str):
    if not price_str:
        return None
    try:
        clean = re.sub(r'[\$,\s]', '', str(price_str))
        if clean.lower().endswith('m'):
            return float(clean[:-1]) * 1_000_000
        elif clean.lower().endswith('k'):
            return float(clean[:-1]) * 1_000
        return float(clean)
    except:
        return None


@app.route("/listings/<int:agency_id>")
def listings(agency_id):
    agency = db.session.get(Agency, agency_id)
    if not agency:
        return redirect("/owner-login?error=Agency+not+found")
    all_listings = Listing.query.filter_by(
        agency_id=agency_id
    ).order_by(Listing.status.asc(), Listing.price_numeric.asc()).all()
    return render_template("listings.html", agency=agency, listings=all_listings)


@app.route("/add-listing/<int:agency_id>", methods=["POST"])
def add_listing(agency_id):
    try:
        agency = db.session.get(Agency, agency_id)
        if not agency:
            return jsonify({"error": "Agency not found"}), 404
        data = request.get_json(force=True)
        price_numeric = parse_price(data.get("price", ""))
        listing = Listing(
            agency_id=agency_id,
            title=data.get("title", "").strip(),
            location=data.get("location", "").strip(),
            price_raw=data.get("price", "").strip(),
            price=price_numeric,
            price_numeric=price_numeric,
            bedrooms=int(data["bedrooms"]) if data.get("bedrooms") else None,
            bathrooms=int(data["bathrooms"]) if data.get("bathrooms") else None,
            property_type=data.get("property_type", "").strip(),
            features=data.get("features", "").strip(),
            description=data.get("description", "").strip(),
            status="available"
        )
        db.session.add(listing)
        db.session.commit()
        print(f"✅ Listing added: {listing.title} (ID {listing.id})")
        return jsonify({"success": True, "listing_id": listing.id, "title": listing.title})
    except Exception as e:
        print(f"❌ Add listing error: {e}")
        db.session.rollback()
        return jsonify({"error": "Failed to add listing"}), 500


@app.route("/upload-listings/<int:agency_id>", methods=["POST"])
def upload_listings(agency_id):
    try:
        agency = db.session.get(Agency, agency_id)
        if not agency:
            return jsonify({"error": "Agency not found"}), 404
        if 'file' not in request.files:
            return jsonify({"error": "No file uploaded"}), 400
        file = request.files['file']
        if not file.filename.endswith('.csv'):
            return jsonify({"error": "Only CSV files are supported"}), 400
        content = file.read().decode('utf-8-sig')
        reader = csv.DictReader(StringIO(content))
        added = 0
        errors = []
        for i, row in enumerate(reader, 1):
            try:
                row = {k.lower().strip(): v.strip() for k, v in row.items() if k}
                title = row.get('title', '').strip()
                if not title:
                    errors.append(f"Row {i}: Missing title, skipped")
                    continue
                price_str = row.get('price', '')
                price_numeric = parse_price(price_str)
                beds = None
                baths = None
                try:
                    if row.get('bedrooms'):
                        beds = int(float(row['bedrooms']))
                except:
                    pass
                try:
                    if row.get('bathrooms'):
                        baths = int(float(row['bathrooms']))
                except:
                    pass
                listing = Listing(
                    agency_id=agency_id,
                    title=title,
                    location=row.get('location', ''),
                    price_raw=price_str,
                    price=price_numeric,
                    price_numeric=price_numeric,
                    bedrooms=beds,
                    bathrooms=baths,
                    property_type=row.get('type', row.get('property_type', '')),
                    features=row.get('features', ''),
                    description=row.get('description', ''),
                    status='available'
                )
                db.session.add(listing)
                added += 1
            except Exception as row_err:
                errors.append(f"Row {i}: {str(row_err)}")
                continue
        db.session.commit()
        print(f"✅ CSV upload: {added} listings added for agency {agency_id}")
        return jsonify({
            "success": True, "added": added,
            "errors": errors,
            "message": f"{added} listings imported successfully"
        })
    except Exception as e:
        print(f"❌ CSV upload error: {e}")
        db.session.rollback()
        return jsonify({"error": f"Upload failed: {str(e)}"}), 500


@app.route("/toggle-listing-status/<int:listing_id>", methods=["POST"])
def toggle_listing_status(listing_id):
    try:
        listing = db.session.get(Listing, listing_id)
        if not listing:
            return jsonify({"error": "Listing not found"}), 404
        data = request.get_json(force=True)
        new_status = data.get("status", "available")
        if new_status not in ['available', 'sold', 'pending']:
            return jsonify({"error": "Invalid status"}), 400
        listing.status = new_status
        db.session.commit()
        return jsonify({"success": True, "status": new_status})
    except Exception as e:
        return jsonify({"error": "Failed to update"}), 500


@app.route("/delete-listing/<int:listing_id>", methods=["DELETE"])
def delete_listing(listing_id):
    try:
        listing = db.session.get(Listing, listing_id)
        if not listing:
            return jsonify({"error": "Listing not found"}), 404
        db.session.delete(listing)
        db.session.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": "Failed to delete"}), 500


@app.route("/delete-all-listings/<int:agency_id>", methods=["DELETE"])
def delete_all_listings(agency_id):
    try:
        count = Listing.query.filter_by(agency_id=agency_id).delete()
        db.session.commit()
        return jsonify({"success": True, "deleted": count})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": "Failed to delete listings"}), 500


@app.route("/get-listings/<int:agency_id>")
def get_listings_api(agency_id):
    try:
        status_filter = request.args.get('status', 'all')
        query = Listing.query.filter_by(agency_id=agency_id)
        if status_filter != 'all':
            query = query.filter_by(status=status_filter)
        all_listings = query.order_by(Listing.price_numeric.asc()).all()
        return jsonify([{
            "id": l.id, "title": l.title, "location": l.location,
            "price": l.price_raw, "price_numeric": l.price_numeric,
            "bedrooms": l.bedrooms, "bathrooms": l.bathrooms,
            "type": l.property_type, "features": l.features,
            "description": l.description, "status": l.status
        } for l in all_listings])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ─────────────────────────────────────────────────────

@app.route("/chat", methods=["POST", "OPTIONS"])
def chat():
    if request.method == "OPTIONS":
        return "", 200
    clean_expired_sessions()
    try:
        data = request.get_json(force=True)
        user_message = data.get("message", "").strip()
        agency_id = int(data.get("agency_id"))
        # Prefer widget-generated session_id (unique per page load = perfect isolation)
        widget_session_id = data.get("session_id")
        if widget_session_id:
            session_key = f"{agency_id}_{widget_session_id}"
        else:
            visitor_ip = request.remote_addr or "unknown"
            user_agent = request.headers.get('User-Agent', '')
            session_hash = hashlib.md5(f"{visitor_ip}{user_agent}".encode()).hexdigest()[:12]
            session_key = f"{agency_id}_{session_hash}"
        if not user_message:
            return jsonify({"error": "Message required"}), 400
        agency = db.session.get(Agency, agency_id)
        if not agency:
            return jsonify({"error": "Invalid agency ID"}), 400

        listings_context = get_listings_context(agency_id)

        system_prompt = f"""You are {agency.assistant_name}, a real estate consultant at {agency.name}.
{listings_context}

GOLDEN RULE - ONE QUESTION PER MESSAGE:
- Never ask two questions in one response. Ever.
- WRONG: "Interested in learning more? When are you hoping to move in?"
- RIGHT: "Interested in learning more about it?"
- Wait for their answer before asking the next thing.

CONVERSATION START - GET NAME FIRST:
- When the client greets you or sends their first message, warmly ask who you're speaking with.
- Example: Client says "Hi" → You say "Hello! May I know who I'm speaking with?"
- Client gives name → "Nice to meet you, [Name]! What's on your mind today?"
- Use their name naturally throughout the conversation.

PACE - LET THE CLIENT LEAD:
- The client came to ask questions. Answer them patiently and helpfully.
- Do not rush to collect information. Help them think and decide first.
- Only after they seem satisfied with a property choice, collect: email, then contact preference.
- Never interrogate. One relaxed question at a time.

PROPERTY RECOMMENDATIONS:
- When you know their budget or location, check listings above for a match.
- If matched: mention it by name with price and key features in 1-2 sentences. Then ask ONE question: "Would you like to know more?"
- If no match: "We don't have anything in that exact range right now, but I can keep an eye out and get back to you with options."

VIEWING FLOW:
- If client selects a property FROM THE LISTINGS and shows interest, offer a viewing: "Would you like to see it in person?"
- Viewing days: Monday to Saturday (Sunday closed). Slots: 10:00 AM, 12:00 PM, 2:00 PM, 4:00 PM, 6:00 PM.
- Ask day first. Then time. Then email if you don't have it yet. Then confirm: "You're booked for [Day] at [Time], [Name]. Confirmation will go to your email."
- If client wants to view a property NOT in the listings: say "Unfortunately we don't currently have a property matching your requirements. I'll find suitable options and get back to you to plan a viewing." Do NOT offer day/time slots in this case. Just collect their email and contact preference so the agency can follow up.

INFORMATION TO COLLECT (strictly one at a time, in this order):
1. Name (at the very start)
2. Property type ("What kind of property are you looking for?")
3. Location ONLY ("Any particular area in mind?") - do NOT mention budget yet
4. Budget ONLY (after location is answered: "And what budget are you working with?")
5. Email (after they're satisfied or a viewing is planned)
6. Contact preference: "Best way to reach you - WhatsApp, phone, or email?"
7. If WhatsApp/phone chosen: ask for the number. If they decline or say email only, that's fine.

NEVER combine location and budget in one question.
WRONG: "Could you share the location and your budget?"
RIGHT: "Any particular area in mind?" → wait → "And what's your budget?"

If customer volunteers multiple details in one message (e.g. "Miami, 10K per month"), accept ALL of it gracefully - acknowledge and move to the NEXT missing item. Never re-ask something they already told you.

FORMATTING RULES:
- Never use markdown: no **, no *, no _, no #, no bullets, no numbered lists
- Plain conversational text only
- Short responses: 1-2 sentences
- Use contractions: "it's", "that's", "you're"

TONE:
- Warm, natural, like a knowledgeable friend
- Vary your acknowledgements - don't repeat "Perfect", "Great", "Awesome" more than once each
- Acknowledge what they said before responding

HANDLING HESITATION:
- Price concern: "I hear you - even a rough range helps. What feels comfortable?"
- Not ready: "No pressure at all. What's holding you back right now?"
- Indecisive: "If you had to pick just one thing that matters most, what would it be?"

LANGUAGE:
- Detect the visitor's language and respond in that same language throughout

Respond naturally in plain text only:"""

    # ─── Session management (widget session_id guarantees isolation) ───
        if session_key not in conversation_memory:
            conversation_memory[session_key] = []
            print(f"🆕 New session started: {session_key}")

        session_timestamps[session_key] = datetime.utcnow()
        history = conversation_memory[session_key]
        history.append({"role": "user", "content": user_message})

        objection = detect_objection(user_message)
        objection_context = ""
        if objection:
            suggested_response = generate_objection_response(objection, agency.name)
            if suggested_response:
                objection_context = f"\n\nNOTE: User expressed a '{objection}' concern. Respond with empathy: '{suggested_response}'"

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
        history.append({"role": "assistant", "content": ai_reply, "name": agency.assistant_name})

        lead_data = extract_lead_data(history)

        # ─── Auto-appointment: requires name + email + day + time ───
        # Only book ONE appointment per session (prevents duplicates from continued chat)
        appt_data = extract_appointment_data(history)
        session_appt_key = f"appt_booked_{session_key}"
        already_booked_this_session = conversation_memory.get(session_appt_key, False)

        if (not already_booked_this_session
                and appt_data['requested']
                and appt_data['day']
                and appt_data['time']
                and lead_data.get('email')
                and lead_data.get('name')):
            existing_appt = Appointment.query.filter_by(
                agency_id=agency_id,
                customer_email=lead_data['email'],
                appointment_date=appt_data['day'],
                appointment_time=appt_data['time']
            ).first()
            if not existing_appt:
                try:
                    new_appt = Appointment(
                        agency_id=agency_id,
                        customer_name=lead_data.get('name'),
                        customer_email=lead_data['email'],
                        appointment_date=appt_data['day'],
                        appointment_time=appt_data['time'],
                        property_interest=lead_data.get('budget', '') + ' property viewing',
                        status='pending'
                    )
                    db.session.add(new_appt)
                    db.session.commit()
                    conversation_memory[session_appt_key] = True  # ← mark as booked
                    print(f"✅ Appointment auto-booked: {new_appt.customer_name} | {appt_data['day']} at {appt_data['time']}")
                    send_appointment_confirmation(agency, new_appt)
                except Exception as appt_err:
                    print(f"⚠️ Auto-appointment error: {appt_err}")
                    db.session.rollback()
            else:
                conversation_memory[session_appt_key] = True  # existing appt found, mark done

        if is_lead_qualified(lead_data, history):
            try:
                existing_lead = Lead.query.filter_by(
                    agency_id=agency_id, email=lead_data['email']
                ).first()
                if existing_lead:
                    updated = False
                    if not existing_lead.whatsapp_number and lead_data.get('whatsapp_number'):
                        existing_lead.whatsapp_number = lead_data['whatsapp_number']
                        existing_lead.contact_preference = lead_data['contact_preference']
                        updated = True
                    if not existing_lead.phone and lead_data.get('phone'):
                        existing_lead.phone = lead_data['phone']
                        existing_lead.contact_preference = lead_data['contact_preference']
                        updated = True
                    if not existing_lead.name and lead_data.get('name'):
                        existing_lead.name = lead_data['name']
                        updated = True
                    if updated:
                        db.session.commit()
                        print(f"✅ Lead {existing_lead.id} silently updated")
                    else:
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
                        intent_score=quality_score,
                        lead_status='new',
                        notes='[]'
                    )
                    db.session.add(lead)
                    db.session.commit()
                    print(f"✅ Lead saved: ID {lead.id} | Score: {quality_score}/5")
                    send_lead_email(agency, lead)
                    send_crm_webhook(agency, lead)
            except Exception as save_err:
                print(f"❌ Lead save error: {save_err}")
                db.session.rollback()

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
                conversation_memory.pop(key, None)
                session_timestamps.pop(key, None)
        db.session.delete(lead)
        db.session.commit()
        return jsonify({"message": "Lead deleted"})
    except Exception as e:
        return jsonify({"error": "Failed to delete"}), 500


@app.route("/clear-all-leads/<int:agency_id>", methods=["DELETE"])
def clear_all_leads(agency_id):
    try:
        keys_to_delete = [k for k in conversation_memory.keys() if k.startswith(f"{agency_id}_")]
        for key in keys_to_delete:
            conversation_memory.pop(key, None)
            session_timestamps.pop(key, None)
        deleted_count = Lead.query.filter_by(agency_id=agency_id).delete()
        db.session.commit()
        return jsonify({"message": f"{deleted_count} leads deleted"})
    except Exception as e:
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
        headers = ["Sr #", "Quality", "Status", "Name", "Email", "Contact",
                   "Preference", "Budget", "Customer Insights", "Date"]
        ws.append(headers)
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for i, lead in enumerate(leads, start=1):
            quality_stars = "⭐" * (lead.intent_score or 1)
            contact = lead.whatsapp_number if lead.whatsapp_number else (lead.phone if lead.phone else "—")
            preference = lead.contact_preference.replace('_', ' ').title() if lead.contact_preference else "Email"
            status = (lead.lead_status or 'new').title()
            ws.append([
                i, quality_stars, status, lead.name or "—", lead.email or "—",
                contact, preference, lead.budget or "—", lead.message or "—",
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
        return Response(buffer,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f"attachment; filename=leads_agency_{agency_id}.xlsx"})
    except Exception as e:
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


@app.route("/analytics/<int:agency_id>")
def analytics(agency_id):
    agency = db.session.get(Agency, agency_id)
    if not agency:
        return redirect("/owner-login?error=Agency+not+found")
    leads = Lead.query.filter_by(agency_id=agency_id).all()
    now = datetime.utcnow()
    total = len(leads)
    hot = sum(1 for l in leads if l.intent_score == 5)
    high = sum(1 for l in leads if (l.intent_score or 1) >= 4)
    avg_score = round(sum(l.intent_score or 1 for l in leads) / total, 1) if total else 0.0
    quality_dist = {i: sum(1 for l in leads if (l.intent_score or 1) == i) for i in range(1, 6)}
    thirty_days_ago = now - timedelta(days=30)
    daily_counts = defaultdict(int)
    for lead in leads:
        if lead.created_at:
            try:
                dt = lead.created_at
                if hasattr(dt, 'tzinfo') and dt.tzinfo is not None:
                    dt = dt.replace(tzinfo=None)
                if dt >= thirty_days_ago:
                    daily_counts[dt.strftime('%Y-%m-%d')] += 1
            except Exception:
                pass
    date_labels, date_values = [], []
    for i in range(29, -1, -1):
        day = now - timedelta(days=i)
        date_labels.append(day.strftime('%b %d'))
        date_values.append(daily_counts.get(day.strftime('%Y-%m-%d'), 0))
    this_month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    last_month_end = this_month_start - timedelta(seconds=1)
    last_month_start = last_month_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    def naive(dt):
        if dt and hasattr(dt, 'tzinfo') and dt.tzinfo is not None:
            return dt.replace(tzinfo=None)
        return dt
    this_month = sum(1 for l in leads if naive(l.created_at) and naive(l.created_at) >= this_month_start)
    last_month = sum(1 for l in leads if naive(l.created_at) and last_month_start <= naive(l.created_at) <= last_month_end)
    return render_template("analytics.html",
        agency=agency, agency_id=agency_id, total=total, hot=hot, high=high,
        avg_score=avg_score, quality_dist=quality_dist, date_labels=date_labels,
        date_values=date_values, this_month=this_month, last_month=last_month)


@app.route("/update-agency-webhook/<int:agency_id>", methods=["POST"])
def update_agency_webhook(agency_id):
    agency = db.session.get(Agency, agency_id)
    if not agency:
        return jsonify({"error": "Agency not found"}), 404
    webhook_url = request.form.get("webhook_url", "").strip()
    agency.webhook_url = webhook_url if webhook_url else None
    db.session.commit()
    return redirect(f"/analytics/{agency_id}")


@app.route("/send-followups", methods=["GET", "POST"])
def send_followups():
    results = process_pending_followups()
    return jsonify({"status": "ok", "results": results})


# -------------------------
# DATABASE INIT
# -------------------------
with app.app_context():
    db.create_all()
    print("✅ Database ready")
    try:
        from sqlalchemy import text, inspect
        inspector = inspect(db.engine)
        lead_cols = [col['name'] for col in inspector.get_columns('lead')]
        agency_cols = [col['name'] for col in inspector.get_columns('agency')]

        if 'intent_score' not in lead_cols:
            db.session.execute(text("ALTER TABLE lead ADD COLUMN intent_score INTEGER DEFAULT 1;"))
            db.session.commit()
        if 'whatsapp_number' not in lead_cols:
            db.session.execute(text("ALTER TABLE lead ADD COLUMN whatsapp_number VARCHAR(50);"))
            db.session.commit()
        if 'contact_preference' not in lead_cols:
            db.session.execute(text("ALTER TABLE lead ADD COLUMN contact_preference VARCHAR(20) DEFAULT 'email';"))
            db.session.commit()
        if 'follow_up_1_sent' not in lead_cols:
            db.session.execute(text("ALTER TABLE lead ADD COLUMN follow_up_1_sent INTEGER DEFAULT 0;"))
            db.session.commit()
        if 'follow_up_7_sent' not in lead_cols:
            db.session.execute(text("ALTER TABLE lead ADD COLUMN follow_up_7_sent INTEGER DEFAULT 0;"))
            db.session.commit()
        if 'webhook_url' not in agency_cols:
            db.session.execute(text("ALTER TABLE agency ADD COLUMN webhook_url VARCHAR(500);"))
            db.session.commit()
        if 'lead_status' not in lead_cols:
            db.session.execute(text("ALTER TABLE lead ADD COLUMN lead_status VARCHAR(20) DEFAULT 'new';"))
            db.session.commit()
        if 'notes' not in lead_cols:
            db.session.execute(text("ALTER TABLE lead ADD COLUMN notes TEXT DEFAULT '[]';"))
            db.session.commit()

        print("✅ All migrations complete")
    except Exception as e:
        print(f"⚠️ Migration error: {e}")
        db.session.rollback()


# -------------------------
# RUN
# -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)