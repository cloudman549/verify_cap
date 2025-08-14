from flask import Flask, request, jsonify
from flask_cors import CORS
from pymongo import MongoClient
from datetime import datetime
import uuid
import asyncio
import httpx  # async HTTP client

app = Flask(__name__)
CORS(app)
app.config['JSON_AS_ASCII'] = False  # Hindi/Unicode support

# ✅ MongoDB Atlas connection
client = MongoClient(
    "mongodb+srv://cloudman549:cloudman%40100@cluster0.7s7qba2.mongodb.net/license_db?retryWrites=true&w=majority&appName=Cluster0"
)
db = client["license_db"]
licenses_col = db["licenses"]
tokens_col = db["tokens"]

# ✅ MongoDB Indexing
licenses_col.create_index("key")
tokens_col.create_index("token")
tokens_col.create_index("created_at", expireAfterSeconds=720)  # TTL index for 12 mins

# ✅ TrueCaptcha credentials
TRUECAPTCHA_USERID = "Alvish"
TRUECAPTCHA_APIKEY = "87v24q7i9VZDXsOi8CAG"


@app.route('/generate-token', methods=['POST'])
def generate_token():
    data = request.get_json()
    license_key = data.get('licenseKey')
    device_id = data.get('deviceId')

    if not license_key or not device_id:
        return jsonify({"success": False, "message": "Missing licenseKey or deviceId"}), 400

    lic = licenses_col.find_one({"key": license_key})
    if not lic:
        return jsonify({"success": False, "message": "License key not found"}), 404
    if not lic.get("active", False):
        return jsonify({"success": False, "message": "License is deactivated"}), 403
    if not lic.get("paid", False):
        return jsonify({"success": False, "message": "License is unpaid"}), 403

    if lic.get("mac") not in ["", device_id]:
        return jsonify({"success": False, "message": "License bound to another device"}), 403

    if lic.get("mac", "") == "":
        licenses_col.update_one({"key": license_key}, {"$set": {"mac": device_id}})

    # ✅ Delete any existing token for same license + device (if any)
    tokens_col.delete_many({"license_key": license_key, "device_id": device_id})

    # ✅ Create new token
    token = str(uuid.uuid4())
    tokens_col.insert_one({
        "token": token,
        "license_key": license_key,
        "device_id": device_id,
        "created_at": datetime.utcnow(),  # For TTL deletion
        "used": False
    })

    return jsonify({"success": True, "authToken": token}), 200


@app.route('/solve-truecaptcha', methods=['POST'])
async def solve_truecaptcha():
    # Enhanced token validation
    token = request.headers.get('X-Auth-Token')
    if not token:
        return jsonify({"error": "Missing auth token"}), 401

    token_doc = tokens_col.find_one({"token": token})
    if not token_doc or token_doc.get("used", False):
        return jsonify({"error": "Invalid, expired, or already used token"}), 403

    data = request.get_json()
    image_content = data.get('imageContent')
    if not image_content:
        return jsonify({"error": "Missing imageContent"}), 400

    payload = {
        'userid': TRUECAPTCHA_USERID,
        'apikey': TRUECAPTCHA_APIKEY,
        'data': image_content,
        # Add more parameters if needed
        'case': 'mixed',  # or 'upper' or 'lower' depending on your needs
        'mode': 'human'   # or 'auto'
    }

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:  # increased timeout
            response = await client.post(
                'https://api.apitruecaptcha.org/one/gettext',
                json=payload,
                headers={'Content-Type': 'application/json'}
            )

        response_data = response.json()
        
        # Better error handling for TrueCaptcha API
        if response.status_code != 200:
            error_msg = response_data.get('error', 'Unknown TrueCaptcha error')
            return jsonify({'error': f'TrueCaptcha error: {error_msg}'}), 502
            
        if not response_data.get('success', False):
            return jsonify({'error': response_data.get('error', 'Captcha solve failed')}), 400

        result = response_data.get('result')
        if not result:
            return jsonify({'error': 'Empty result from TrueCaptcha'}), 400

        # Mark token as used after successful captcha solve
        tokens_col.update_one(
            {"token": token},
            {"$set": {"used": True}}
        )

        return jsonify({'result': result}), 200

    except httpx.TimeoutException:
        return jsonify({'error': 'TrueCaptcha API timeout'}), 504
    except Exception as e:
        return jsonify({'error': f'Internal server error: {str(e)}'}), 500