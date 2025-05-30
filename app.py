import google.generativeai as genai
import os
import time
from collections import deque
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify
from flask_pymongo import PyMongo
from bson.objectid import ObjectId
from datetime import datetime
import re
from html import escape
from flask_httpauth import HTTPBasicAuth

# --- Load environment variables from .env file ---
load_dotenv()

# --- Basic Auth Setup ---
auth = HTTPBasicAuth()
users = {
    os.getenv("AUTH_USERNAME"): os.getenv("AUTH_PASSWORD")
}

@auth.verify_password
def verify_password(username, password):
    if username in users and users[username] == password:
        return username

# --- Flask App Initialization ---
app = Flask(__name__)
app.secret_key = os.urandom(24)

# --- MongoDB Configuration ---
MONGO_URI = os.getenv("MONGO_URI")
if not MONGO_URI:
    raise ValueError("❌ MONGO_URI not set in environment variables or .env")
app.config["MONGO_URI"] = MONGO_URI
mongo = PyMongo(app)

# --- Gemini API Configuration ---
API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    raise ValueError("❌ GEMINI_API_KEY not found in environment variables or .env file.")
genai.configure(api_key=API_KEY)

MODEL_NAME = 'gemini-2.0-flash'
RATE_LIMIT_RPM = 15
TIME_WINDOW_SECONDS = 60
MIN_INTERVAL_BETWEEN_REQUESTS = TIME_WINDOW_SECONDS / RATE_LIMIT_RPM

# --- Rate Limiter ---
request_timestamps = deque()

def build_link_focused_prompt(user_input):
    return (
        "The user has the following question, task, or goal:\n\n"
        f"\"{user_input}\"\n\n"
        "Your job is to return a list of 3 to 7 useful websites or platforms to help the user.\n"
        "Do NOT answer the question directly.\n\n"
        "Format your response EXACTLY like this:\n\n"
        "### Platform Name\n"
        "- Link: https://example.com\n"
        "- Free to Use: Yes/No (add details if needed)\n"
        "- Note: A brief 2-4 sentence description about the platform and its benefits.\n\n"
        "Repeat this format for each platform."
    )

def call_gemini_api_backend(prompt_text):
    current_time = time.time()
    while request_timestamps and current_time - request_timestamps[0] >= TIME_WINDOW_SECONDS:
        request_timestamps.popleft()

    if len(request_timestamps) >= RATE_LIMIT_RPM:
        time_to_wait = (request_timestamps[0] + TIME_WINDOW_SECONDS) - current_time
        return f"RATE_LIMIT_EXCEEDED: Please wait {time_to_wait:.2f} seconds."

    if request_timestamps:
        elapsed = current_time - request_timestamps[-1]
        if elapsed < MIN_INTERVAL_BETWEEN_REQUESTS:
            time.sleep(MIN_INTERVAL_BETWEEN_REQUESTS - elapsed)

    request_timestamps.append(time.time())

    try:
        model = genai.GenerativeModel(MODEL_NAME)
        response = model.generate_content(prompt_text)
        return response.text
    except Exception as e:
        return f"ERROR: An error occurred during API call: {e}"

def store_message(user_id, message, sender, conversation_id=None):
    message_data = {
        'user_id': user_id,
        'message': message,
        'sender': sender,
        'timestamp': datetime.utcnow()
    }
    if conversation_id:
        message_data['conversation_id'] = conversation_id
    return mongo.db.chats.insert_one(message_data).inserted_id

def indent_subheadings(raw_text):
    def indent_line(match):
        label = escape(match.group(1))
        content = match.group(2).strip()
        if label.lower() == "link" and re.match(r'https?://', content):
            content = f'<a href="{escape(content)}" target="_blank" rel="noopener noreferrer">{escape(content)}</a>'
        else:
            content = escape(content)
        return (
            '<div style="display: flex; align-items: flex-start; margin-left: 20px;">'
            '<span style="font-size: 90%; font-weight: normal; padding-right: 10px;">o</span>'
            f'<div><strong>{label}:</strong> {content}</div>'
            '</div>'
        )

    raw_text = re.sub(
        r'^###\s*(.+)',
        lambda m: f'<br><div><strong>• {escape(m.group(1).strip())}</strong></div><br>',
        raw_text,
        flags=re.MULTILINE
    )
    raw_text = re.sub(r'-\s*(Link|Free to Use|Note):\s*(.*)', indent_line, raw_text)
    return raw_text

# --- Flask Routes ---
@app.route('/')
def home():
    return render_template('front.html')  # Public route

@app.route('/chat_interface')
@auth.login_required
def chat_interface():
    return render_template('index.html')

@app.route('/get_conversation/<user_id>', methods=['GET'])
@auth.login_required
def get_conversation(user_id):
    try:
        conversation = list(mongo.db.chats.find({'user_id': user_id}).sort('timestamp', 1))
        for item in conversation:
            item['_id'] = str(item['_id'])
            item['timestamp'] = item['timestamp'].isoformat()
        return jsonify(conversation)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/chat', methods=['POST'])
@auth.login_required
def chat():
    user_message = request.json.get('message')
    if not user_message:
        return jsonify({'response': 'Please enter a message.'}), 400

    store_message('anonymous', user_message, 'user')
    link_prompt = build_link_focused_prompt(user_message)
    gemini_response = call_gemini_api_backend(link_prompt)

    if gemini_response.startswith("RATE_LIMIT_EXCEEDED"):
        return jsonify({'response': gemini_response, 'status': 'rate_limit'}), 429
    elif gemini_response.startswith("ERROR:"):
        return jsonify({'response': gemini_response, 'status': 'error'}), 500

    formatted = indent_subheadings(gemini_response)
    store_message('anonymous', formatted, 'bot')

    return jsonify({'response': formatted})

@app.route('/get_history', methods=['GET'])
@auth.login_required
def get_history():
    try:
        history = list(mongo.db.chats.find({'user_id': 'anonymous', 'sender': 'user'}).sort('timestamp', -1).limit(50))
        for item in history:
            item['_id'] = str(item['_id'])
            item['timestamp'] = item['timestamp'].isoformat()
        return jsonify(history)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ✅ Do NOT use app.run() in PythonAnywhere WSGI setup!
if __name__ == '__main__':
    app.run(debug=True)
