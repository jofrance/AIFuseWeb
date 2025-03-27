import logging
import sys
import threading
import requests
import json
import time
import os
import jwt
from flask import Flask, request, jsonify, render_template_string

# ------------------
# Logging Configuration
# ------------------
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger(__name__)
file_handler = logging.FileHandler("app.log")
file_handler.setLevel(logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# ------------------
# Configuration Setup
# ------------------
# This CONFIG dictionary now includes parameters needed for Managed Identity authentication.
CONFIG = {
    "client_id": "405a9ef7-5457-4381-9c0c-f3c9321e4a89",      # Your APP_REGISTRATION_CLIENT_ID
    #"authority": "https://login.microsoftonline.com/72f988bf-86f1-41af-91ab-2d7cd011db47",
    "authority": "https://login.microsoftonline.com/16b3c013-d300-468d-ac64-7eda0820b6d3",
    "scopes": "api://9021b3a5-1f0d-4fb7-ad3f-d6989f0432d8/.default",  # Zebra API scope
    "apiUrl": "https://zebra-ai-api-prd.azurewebsites.net/",
    "experimentId": "582c5e80-b307-43f9-bc86-efd0a6551907",
    "API_TIMEOUT": 30,  # seconds
    "Custom Chat Instructions": {
         "ChatCustomization": "Be formal, courteous, and clear in your responses."
    },
    # Managed Identity / Federated credentials
    "RESOURCE_TENANT_ID": "72f988bf-86f1-41af-91ab-2d7cd011db47",   # Corp Tenant ID
    "MI_CLIENT_ID": "4f6c8552-7b64-4327-9b2b-8d32b41bfe44",           # Managed Identity Client ID
    # For broker flows, you might also include "AZURE_REGION": "eSTS-R", etc.
}

# Globals for token management.
access_token = None
token_lock = threading.Lock()

# ------------------
# Managed Identity Authentication using FederatedApplicationCredential
# ------------------
from azure.identity import ManagedIdentityCredential, ClientAssertionCredential
from azure.core.credentials import TokenCredential, AccessToken

MSI_ASSERTION_SCOPE = "api://AzureADTokenExchange/.default"
ZEBRA_API_SCOPE = CONFIG["scopes"]

class FederatedApplicationCredential(TokenCredential):
    def __init__(self, tenant_id: str, msi_client_id: str, app_client_id: str) -> None:
        self.managed_identity = ManagedIdentityCredential(client_id=msi_client_id)
        self.client_assertion = ClientAssertionCredential(
            tenant_id=tenant_id,
            client_id=app_client_id,
            func=self.compute_assertion
        )
        super().__init__()

    def get_token(self, *scopes, **kwargs) -> AccessToken:
        return self.client_assertion.get_token(*scopes, **kwargs)

    def compute_assertion(self):
        msi_token = self.managed_identity.get_token(MSI_ASSERTION_SCOPE)
        logger.info("Obtained MSI token for assertion.")
        return msi_token.token

def get_access_token():
    """Acquires an access token using Managed Identity with MSAL."""
    mi_client_id = CONFIG.get("MI_CLIENT_ID")
    app_client_id = CONFIG.get("client_id")
    tenant_id = CONFIG.get("RESOURCE_TENANT_ID")
    if not all([mi_client_id, app_client_id, tenant_id]):
        msg = "Missing required configuration for Managed Identity."
        logger.error(msg)
        raise ValueError(msg)
    cred = FederatedApplicationCredential(
         tenant_id=tenant_id,
         msi_client_id=mi_client_id,
         app_client_id=app_client_id
    )
    try:
        token_obj = cred.get_token(ZEBRA_API_SCOPE)
        access_tok = token_obj.token
        # Decode token payload (without verifying signature) to log claims.
        token_payload = jwt.decode(access_tok, options={"verify_signature": False, "verify_aud": False})
        logger.info(f"Access token claims: {token_payload}")
        logger.info("Successfully obtained ZebraAI API access token.")
        return access_tok
    except Exception as e:
        logger.error(f"Error obtaining API access token: {e}", exc_info=True)
        raise e

# ------------------
# Chat Functionality
# ------------------
conversation_history = []

def call_chat_api(payload, headers):
    """
    Calls the API endpoint with the given payload and headers.
    Retries every 5 seconds until a valid reply is obtained.
    Returns the assistant's reply and updates the conversation_history.
    """
    api_endpoint = f'{CONFIG["apiUrl"]}experiment/{CONFIG["experimentId"]}'
    reply = None
    logger.debug(f"Calling API at {api_endpoint} with payload: {payload} and headers: {headers}")
    while reply is None:
        try:
            response = requests.post(api_endpoint, headers=headers, data=json.dumps(payload), timeout=CONFIG["API_TIMEOUT"])
            logger.debug(f"API response code: {response.status_code}")
            if response.status_code == 200:
                data = response.json()
                logger.debug(f"API response JSON: {data}")
                messages = data.get("chatHistory", {}).get("messages", [])
                if messages:
                    last_message = messages[-1]
                    reply = last_message.get("content", "No content in reply.")
                else:
                    reply = "No messages in API response."
                if "chatHistory" in data and "messages" in data["chatHistory"]:
                    conversation_history[:] = data["chatHistory"]["messages"]
            else:
                logger.error(f"Error {response.status_code}: {response.text}")
        except Exception as e:
            logger.exception(f"Exception during API call: {e}")
        if reply is None:
            logger.debug("No reply obtained, retrying in 5 seconds...")
            time.sleep(5)
    logger.debug(f"Final reply obtained: {reply}")
    return reply

# ------------------
# Flask Application
# ------------------
app = Flask(__name__)

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
    logger.debug("Rendering index page.")
    return render_template_string(CHAT_TEMPLATE, conversation=conversation_history)

@app.route("/chat", methods=["POST"])
def chat_route():
    global access_token
    user_message = request.form.get("message", "").strip()
    logger.debug(f"User message received: '{user_message}'")
    if user_message:
        conversation_history.append({
            "id": f"user-{len(conversation_history)+1}",
            "role": "user",
            "content": user_message
        })
    else:
        if not conversation_history:
            default_search = "123"
            conversation_history.append({
                "id": f"user-{len(conversation_history)+1}",
                "role": "user",
                "content": default_search
            })
            user_message = default_search
            logger.debug("No user message provided; using default search.")

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
    logger.debug(f"Payload built: {payload}")

    with token_lock:
        token = access_token
    if not token:
        logger.debug("No existing access token found, acquiring new token.")
        token = get_access_token()
        with token_lock:
            access_token = token
    logger.debug(f"Access token: {token}")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

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
        logger.debug(f"System message appended: {system_msg}")

    # Prevent duplicate assistant messages:
    if not conversation_history or conversation_history[-1]["role"] != "assistant" or conversation_history[-1]["content"] != reply:
        assistant_msg = {
            "id": f"assistant-{len(conversation_history)+1}",
            "role": "assistant",
            "content": reply
        }
        conversation_history.append(assistant_msg)
        logger.debug(f"Assistant message appended: {assistant_msg}")
    else:
        logger.debug("Assistant message already present; not appending duplicate.")

    logger.debug(f"Returning conversation history: {conversation_history}")
    return jsonify({"reply": reply, "conversation_history": conversation_history})

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    logger.debug(f"Starting Flask app on port {port}")
    app.run(host='0.0.0.0', port=port, debug=True)

