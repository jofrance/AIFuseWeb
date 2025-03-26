import threading
import requests
import json
import time
from flask import Flask, request, jsonify, render_template_string

# ------------------
# Configuration Setup
# ------------------
# Here we mimic your config module in-line.
CONFIG = {
    "client_id": "751c47e2-782e-4d75-b304-37f68a9d45fd",
    "authority": "https://login.microsoftonline.com/72f988bf-86f1-41af-91ab-2d7cd011db47",
    "scopes": "api://9021b3a5-1f0d-4fb7-ad3f-d6989f0432d8/.default",
    "apiUrl": "https://zebra-ai-api-prd.azurewebsites.net/",         # Replace with your API URL
    "experimentId": "582c5e80-b307-43f9-bc86-efd0a6551907",       # Replace with your experiment ID
    "API_TIMEOUT": 30,                          # In seconds
    "Custom Chat Instructions": {
         "ChatCustomization": "Be formal, courteous, and clear in your responses."
    }
}

# Globals for token management.
access_token = None
token_lock = threading.Lock()

def get_access_token():
    """Acquire an access token using MSAL."""
    from msal import PublicClientApplication
    # Check if the "msal_app" key exists in CONFIG; if not, initialize it.
    if CONFIG.get("msal_app") is None:
        CONFIG["msal_app"] = PublicClientApplication(
            client_id=CONFIG["client_id"],
            authority=CONFIG["authority"],
            # enable_broker_on_windows=True  # Uncomment if needed.
        )
    accounts = CONFIG["msal_app"].get_accounts()
    # Ensure scopes is a list.
    scopes = CONFIG["scopes"]
    if isinstance(scopes, str):
        scopes = [scopes]
    result = None
    if accounts:
        result = CONFIG["msal_app"].acquire_token_silent(scopes, account=accounts[0])
        if result and "access_token" in result:
            return result["access_token"]
    result = CONFIG["msal_app"].acquire_token_interactive(scopes)
    if "access_token" in result:
        return result["access_token"]
    else:
        raise Exception("Failed to get access token")

# ------------------
# Chat Functionality
# ------------------
# Global conversation history for chat mode.
conversation_history = []

def call_chat_api(payload, headers):
    """
    Calls the API endpoint with the given payload and headers.
    Retries every 5 seconds until a valid reply is obtained.
    Returns the assistant's reply and updates the conversation_history
    from the API response if provided.
    """
    api_endpoint = f'{CONFIG["apiUrl"]}experiment/{CONFIG["experimentId"]}'
    reply = None
    while reply is None:
        try:
            response = requests.post(api_endpoint, headers=headers, data=json.dumps(payload), timeout=CONFIG["API_TIMEOUT"])
            if response.status_code == 200:
                data = response.json()
                messages = data.get("chatHistory", {}).get("messages", [])
                if messages:
                    # Assume the last message is the assistant reply.
                    last_message = messages[-1]
                    reply = last_message.get("content", "No content in reply.")
                else:
                    reply = "No messages in API response."
                # If the API returns a chatHistory, update our conversation.
                if "chatHistory" in data and "messages" in data["chatHistory"]:
                    conversation_history[:] = data["chatHistory"]["messages"]
            else:
                print(f"Error {response.status_code}: {response.text}")
        except Exception as e:
            print(f"Exception during API call: {e}")
        if reply is None:
            time.sleep(5)
    return reply

# ------------------
# Flask Application
# ------------------
app = Flask(__name__)

# HTML template (self-contained) for the chat interface.
CHAT_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Chat with Model</title>
    <style>
      body { font-family: Arial, sans-serif; }
      #chat-display {
          width: 500px;
          height: 400px;
          border: 1px solid #ccc;
          overflow-y: scroll;
          padding: 10px;
          margin-bottom: 10px;
          white-space: pre-wrap;
          font-family: monospace;
      }
      .separator {
          border-top: 1px dashed #aaa;
          margin: 5px 0;
      }
    </style>
</head>
<body>
    <h1>Chat with Model</h1>
    <div id="chat-display">
        {% for msg in conversation %}
            <p><strong>{{ msg.role.capitalize() }}:</strong> {{ msg.content }}</p>
            <div class="separator"></div>
        {% endfor %}
    </div>
    <form id="chat-form">
        <input type="text" id="messageBox" placeholder="Type your message here..." style="width:400px;">
        <button type="submit">Send</button>
    </form>
    <script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
    <script>
        $("#chat-form").submit(function(event) {
            event.preventDefault();
            var message = $("#messageBox").val();
            $.post("/chat", { message: message }, function(data) {
                var chatDisplay = $("#chat-display");
                chatDisplay.empty();
                data.conversation_history.forEach(function(msg) {
                    chatDisplay.append("<p><strong>" + msg.role.charAt(0).toUpperCase() + msg.role.slice(1) + ":</strong> " + msg.content + "</p>");
                    chatDisplay.append("<div class='separator'></div>");
                });
                $("#messageBox").val("");
                chatDisplay.scrollTop(chatDisplay[0].scrollHeight);
            });
        });
    </script>
</body>
</html>
"""

@app.route("/")
def index():
    # Render the chat template with the current conversation_history.
    return render_template_string(CHAT_TEMPLATE, conversation=conversation_history)

@app.route("/chat", methods=["POST"])
def chat_route():
    global access_token
    user_message = request.form.get("message", "").strip()
    if user_message:
        conversation_history.append({
            "id": f"user-{len(conversation_history)+1}",
            "role": "user",
            "content": user_message
        })
    else:
        # On first call, if no message is provided, use a default value.
        if not conversation_history:
            default_search = "123"
            conversation_history.append({
                "id": f"user-{len(conversation_history)+1}",
                "role": "user",
                "content": default_search
            })
            user_message = default_search

    # Build payload.
    payload = {
        "dataSearchKey": "CaseNumber",
        "DataSearchOptions": {
            "Search": "123",
            "SearchMode": "all"
        },
        "chatHistory": {
            "messages": conversation_history
        },
        "MaxNumberOfRows": 5000
    }

    with token_lock:
        token = access_token
    if not token:
        token = get_access_token()
        with token_lock:
            access_token = token

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    # Call the API (this will block until a valid reply is received).
    reply = call_chat_api(payload, headers)

    # Inject custom system prompt if no system message exists.
    if not any(msg["role"] == "system" for msg in conversation_history):
        custom_instruction = CONFIG.get("Custom Chat Instructions", {}).get("ChatCustomization", "Run a job to start the conversation.")
        greeting = f"Hi, {custom_instruction}"
        system_msg = {
            "id": "system-001",
            "role": "system",
            "content": greeting
        }
        conversation_history.append(system_msg)

    conversation_history.append({
        "id": f"assistant-{len(conversation_history)+1}",
        "role": "assistant",
        "content": reply
    })

    return jsonify({"reply": reply, "conversation_history": conversation_history})

if __name__ == "__main__":
    app.run(debug=True)

