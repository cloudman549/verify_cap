from flask import Flask, request, jsonify
from flask_cors import CORS
from pymongo import MongoClient
from datetime import datetime
import uuid
import requests

app = Flask(__name__)
CORS(app)
app.config['JSON_AS_ASCII'] = False  # Hindi/Unicode support

# ✅ MongoDB Atlas connection
client = MongoClient("mongodb+srv://cloudman549:cloudman%40100@cluster0.7s7qba2.mongodb.net/license_db?retryWrites=true&w=majority&appName=Cluster0")
db = client["license_db"]
licenses_col = db["licenses"]
tokens_col = db["tokens"]  # New collection for tokens

# ✅ TrueCaptcha credentials
TRUECAPTCHA_USERID = "Alvish"
TRUECAPTCHA_APIKEY = "zH29k4ht5R8UWhpFifO8"

# ==========================
# ✅ Endpoint: /generate-token
# ==========================
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

    # MAC binding check
    if lic.get("mac") not in ["", device_id]:
        return jsonify({"success": False, "message": "License bound to another device"}), 403

    # Bind MAC if first time
    if lic.get("mac", "") == "":
        licenses_col.update_one({"key": license_key}, {"$set": {"mac": device_id}})

    # Create token
    token = str(uuid.uuid4())
    tokens_col.insert_one({
        "token": token,
        "license_key": license_key,
        "device_id": device_id,
        "created_at": datetime.utcnow(),
        "used": False
    })

    return jsonify({"success": True, "authToken": token}), 200

# ==========================
# ✅ Endpoint: /solve-truecaptcha
# ==========================
@app.route('/solve-truecaptcha', methods=['POST'])
def solve_truecaptcha():
    token = request.headers.get('X-Auth-Token')
    if not token:
        return jsonify({"error": "Missing auth token"}), 401

    token_doc = tokens_col.find_one({"token": token})
    if not token_doc:
        return jsonify({"error": "Invalid or expired token"}), 403

    data = request.get_json()
    image_content = data.get('imageContent')
    if not image_content:
        return jsonify({"error": "Missing imageContent"}), 400

    payload = {
        'userid': TRUECAPTCHA_USERID,
        'apikey': TRUECAPTCHA_APIKEY,
        'data': image_content
    }

    try:
        response = requests.post('https://api.apitruecaptcha.org/one/gettext', json=payload)
        if response.status_code != 200:
            return jsonify({'error': 'TrueCaptcha error'}), 502

        result = response.json().get('result')
        return jsonify({'result': result}), 200

    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ==========================
# ✅ Run App
# ==========================
if __name__ == '__main__':
    app.run(port=5001, debug=True)
