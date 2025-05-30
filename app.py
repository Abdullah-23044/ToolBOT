import google.generativeai as genai
import os
import time
from collections import deque
from dotenv import load_dotenv
from flask import Flask, render_template, request, jsonify
from flask_pymongo import PyMongo
from bson.objectid import ObjectId
from datetime import datetime

# --- Load environment variables from .env file ---
load_dotenv()

# --- Flask App Initialization ---
app = Flask(__name__)
app.secret_key = os.urandom(24)  # Used for sessions

# --- MongoDB Configuration ---
app.config["MONGO_URI"] = os.getenv("MONGO_URI", "mongodb://localhost:27017/toolbot")
mongo = PyMongo(app)

# --- Gemini API Configuration ---
API_KEY = os.getenv("GEMINI_API_KEY")
if not API_KEY:
    raise ValueError("GEMINI_API_KEY not found in environment variables or .env file.")
genai.configure(api_key=API_KEY)

MODEL_NAME = 'gemini-2.0-flash'
RATE_LIMIT_RPM = 15
TIME_WINDOW_SECONDS = 60
MIN_INTERVAL_BETWEEN_REQUESTS = TIME_WINDOW_SECONDS / RATE_LIMIT_RPM

# --- Rate Limiter Implementation ---
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
    """
    Calls the Gemini API, respecting the rate limit.
    Returns the response text or an error message.
    """
    current_time = time.time()

    # Clean up old timestamps from the deque
    while request_timestamps and current_time - request_timestamps[0] >= TIME_WINDOW_SECONDS:
        request_timestamps.popleft()

    # Check if we've hit the rate limit
    if len(request_timestamps) >= RATE_LIMIT_RPM:
        time_to_wait = (request_timestamps[0] + TIME_WINDOW_SECONDS) - current_time
        return f"RATE_LIMIT_EXCEEDED: Please wait {time_to_wait:.2f} seconds before sending another message."

    # Ensure minimum interval between requests
    if request_timestamps:
        last_request_time = request_timestamps[-1]
        elapsed_since_last = current_time - last_request_time
        if elapsed_since_last < MIN_INTERVAL_BETWEEN_REQUESTS:
            time.sleep(MIN_INTERVAL_BETWEEN_REQUESTS - elapsed_since_last)

    # Add the current request's timestamp
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

import markdown
def convert_markdown_to_html(raw_text):
    import re
    from html import escape

    lines = raw_text.strip().split('\n')
    html = []

    for line in lines:
        line = line.strip()
        if not line:
            html.append('<br>')
            continue

        # Match headings like "### Shopify"
        heading_match = re.match(r'^###\s+(.*)', line)
        if heading_match:
            platform = heading_match.group(1).strip()
            html.append(f'<div style="margin-top: 12px;"><strong>• {escape(platform)}</strong></div>')
            continue

        # Match subpoints like "- Link: value"
        subpoint_match = re.match(r'^-\s*(Link|Free to Use|Note):\s*(.+)', line)
        if subpoint_match:
            key = subpoint_match.group(1)
            value = subpoint_match.group(2).strip()

            # Convert links to anchor tags
            url_match = re.match(r'\[.*?\]\((https?://[^\s)]+)\)', value)
            if url_match:
                url = url_match.group(1)
                value = f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer">{escape(url)}</a>'
            elif value.startswith("http"):
                value = f'<a href="{escape(value)}" target="_blank" rel="noopener noreferrer">{escape(value)}</a>'

            html.append(f'<div style="margin-left: 20px;"><strong>- {escape(key)}:</strong> {escape(value)}</div>')
            continue

        # Fallback
        html.append(f'<div>{escape(line)}</div>')

    return '\n'.join(html)

def indent_subheadings(raw_text):
    import re
    from html import escape

    def indent_line(match):
        label = escape(match.group(1))
        content = match.group(2).strip()

        # Make URLs clickable
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

    # Add line break before and after each heading
    raw_text = re.sub(
        r'^###\s*(.+)',
        lambda m: f'<br><div><strong>• {escape(m.group(1).strip())}</strong></div><br>',
        raw_text,
        flags=re.MULTILINE
    )

    # Apply indentation to subpoints
    raw_text = re.sub(r'-\s*(Link|Free to Use|Note):\s*(.*)', indent_line, raw_text)

    return raw_text

def format_links_response(raw_text):
    import re
    from html import escape

    lines = raw_text.strip().split('\n')
    html = []
    in_platform_section = False

    for line in lines:
        line = line.strip()

        if not line:
            # blank line, close any open section
            in_platform_section = False
            html.append('<br>')
            continue

        # Match main headings like "**Indeed:**"
        heading_match = re.match(r'^\*\*(.+?):\*\*$', line)
        if heading_match:
            # Close previous platform section if open
            if in_platform_section:
                html.append('</ul>')
            platform_name = heading_match.group(1).strip()
            html.append(f'<h3 style="font-weight: 700; margin-bottom: 8px;">{escape(platform_name)}</h3>')
            html.append('<ul style="margin-left: 20px; margin-bottom: 15px;">')
            in_platform_section = True
            continue

        # Match bullet points starting with '* **key:** value'
        bullet_match = re.match(r'^\*\s+\*\*(.+?):\*\*\s*(.+)$', line)
        if bullet_match and in_platform_section:
            key = bullet_match.group(1).strip()
            val = bullet_match.group(2).strip()
            # Convert URLs inside val to links (if any)
            url_match = re.search(r'\[(.*?)\]\((https?://[^\s]+)\)', val)
            if url_match:
                text = url_match.group(1)
                url = url_match.group(2)
                val = re.sub(r'\[.*?\]\(.*?\)', f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer">{escape(text)}</a>', val)
            else:
                # If val itself looks like a URL without markdown, convert it to a link
                if re.match(r'https?://', val):
                    val = f'<a href="{escape(val)}" target="_blank" rel="noopener noreferrer">{escape(val)}</a>'

            html.append(f'<li><strong>{escape(key)}:</strong> {val}</li>')
            continue

        # For any other text lines outside lists, close section and add as paragraph
        if in_platform_section:
            html.append('</ul>')
            in_platform_section = False
        html.append(f'<p>{escape(line)}</p>')

    # Close any open ul at the end
    if in_platform_section:
        html.append('</ul>')

    return ''.join(html)


# --- Flask Routes ---
@app.route('/')
def home():
    """Renders the home page."""
    return render_template('front.html')

@app.route('/chat_interface')
def chat_interface():
    """Renders the main chat interface page."""
    return render_template('index.html')

@app.route('/get_conversation/<user_id>', methods=['GET'])
def get_conversation(user_id):
    try:
        conversation = list(mongo.db.chats.find(
            {'user_id': user_id}
        ).sort('timestamp', 1))  # Sort by timestamp ascending
        
        for item in conversation:
            item['_id'] = str(item['_id'])
            item['timestamp'] = item['timestamp'].isoformat()
        
        return jsonify(conversation)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/chat', methods=['POST'])
def chat():
    user_message = request.json.get('message')
    if not user_message:
        return jsonify({'response': 'Please enter a message.'}), 400

    # Store user message
    store_message('anonymous', user_message, 'user')

    # ✅ Build Gemini prompt focused on website suggestions
    link_prompt = build_link_focused_prompt(user_message)
    gemini_response = call_gemini_api_backend(link_prompt)

    if gemini_response.startswith("RATE_LIMIT_EXCEEDED"):
        return jsonify({'response': gemini_response, 'status': 'rate_limit'}), 429
    elif gemini_response.startswith("ERROR:"):
        return jsonify({'response': gemini_response, 'status': 'error'}), 500

    # ✅ Format the Gemini response to clickable HTML
    formatted = indent_subheadings(gemini_response)

    # Store the bot response
    store_message('anonymous', formatted, 'bot')

    return jsonify({'response': formatted})

@app.route('/get_history', methods=['GET'])
def get_history():
    try:
        # Only fetch messages where sender is 'user'
        history = list(mongo.db.chats.find(
            {'user_id': 'anonymous', 'sender': 'user'}  # <-- Added sender filter
        ).sort('timestamp', -1).limit(50))
        
        for item in history:
            item['_id'] = str(item['_id'])
            item['timestamp'] = item['timestamp'].isoformat()
        
        return jsonify(history)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# --- Run the Flask App ---
if __name__ == '__main__':
    app.run(debug=True)
