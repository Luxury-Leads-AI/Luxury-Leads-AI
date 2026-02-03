import os
from flask import Flask, request, jsonify, render_template
from dotenv import load_dotenv
from openai import OpenAI

# Load environment variables
load_dotenv()

app = Flask(__name__)

# Initialize OpenAI client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ===== SYSTEM PROMPT =====
SYSTEM_PROMPT = """
You are an elite luxury real estate AI assistant.

Your job:
- Greet professionally.
- Ask if user wants to Buy, Sell, or Invest.
- Ask preferred location.
- Ask budget range.
- Ask property type.
- Ask timeline.
- Collect name, phone, email.
- Ask one question at a time.
- Be professional and premium.
"""

@app.route("/")
def home():
    return render_template("index.html")

@app.route("/chat", methods=["POST"])
def chat():
    user_message = request.json.get("message")

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message}
        ]
    )

    return jsonify({
        "reply": response.choices[0].message.content
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)

