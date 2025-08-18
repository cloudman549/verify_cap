import os
import uuid
import logging
from datetime import datetime
from threading import Semaphore, Thread
from concurrent.futures import ThreadPoolExecutor
import time
from flask import Flask, request, jsonify
from flask_cors import CORS
from pymongo import MongoClient
import requests

# ----------------------- Configuration -----------------------
MONGO_URI = os.environ.get("MONGO_URI") or (
    "mongodb+srv://cloudman549:cloudman%40100@cluster0.7s7qba2.mongodb.net/license_db?retryWrites=true&w=majority&appName=Cluster0"
)
DB_NAME = os.environ.get("DB_NAME", "license_db")
TRUECAPTCHA_USERID = os.environ.get("TRUECAPTCHA_USERID", "Alvish")
TRUECAPTCHA_APIKEY = os.environ.get("TRUECAPTCHA_APIKEY", "87v24q7i9VZDXsOi8CAG")
TRUECAPTCHA_ENDPOINT = os.environ.get("TRUECAPTCHA_ENDPOINT", "https://api.apitruecaptcha.org/one/gettext")

# Concurrency settings
WORKER_COUNT = int(os.environ.get("WORKER_COUNT", "8"))
TRUECAPTCHA_CONCURRENCY = int(os.environ.get("TRUECAPTCHA_CONCURRENCY", "30"))
TRUECAPTCHA_SEMAPHORE = Semaphore(TRUECAPTCHA_CONCURRENCY)

# Token TTL seconds (e.g., 12 minutes = 720 seconds)
TOKEN_TTL_SECONDS = int(os.environ.get("TOKEN_TTL_SECONDS", "720"))

# ----------------------- App & DB setup -----------------------
app = Flask(__name__)
CORS(app)
app.config['JSON_AS_ASCII'] = False  # Unicode support (Hindi)

# Logging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Thread pool for async operations
executor = ThreadPoolExecutor(max_workers=WORKER_COUNT)

# MongoDB with connection pooling
client = MongoClient(
    MONGO_URI,
    maxPoolSize=WORKER_COUNT * 2,
    connectTimeoutMS=5000,
    socketTimeoutMS=30000,
    serverSelectionTimeoutMS=5000
)
db = client[DB_NAME]
licenses_col = db.get_collection("licenses")
tokens_col = db.get_collection("tokens")

# Global flag to track background task status
is_background_task_running = False
background_thread = None

# Ensure indexes and log TTL status
try:
    licenses_col.create_index("key", unique=True)
    tokens_col.create_index("token", unique=True)
    tokens_col.create_index("created_at", expireAfterSeconds=TOKEN_TTL_SECONDS)
    logger.info(f"MongoDB indexes ensured with TTL of {TOKEN_TTL_SECONDS} seconds for tokens collection")
except Exception as e:
    logger.exception("Failed to create MongoDB indexes")

# ----------------------- Helpers -----------------------

def strip_data_prefix(b64: str) -> str:
    """Remove data:image/...;base64, prefix if present."""
    if not isinstance(b64, str):
        return b64
    if b64.startswith("data:") and "," in b64:
        return b64.split(",", 1)[1]
    return b64

def validate_license(license_key: str):
    """Validate license key in database"""
    try:
        return licenses_col.find_one({"key": license_key})
    except Exception as e:
        logger.error(f"License validation failed: {str(e)}")
        return None

def check_and_drop_empty_tokens():
    """Check if tokens collection is empty and drop it if so, then stop the task."""
    global is_background_task_running
    try:
        while is_background_task_running:
            time.sleep(60)  # Check every 60 seconds
            # Check if collection exists
            if "tokens" in db.list_collection_names():
                with client.start_session() as session:
                    with session.start_transaction():
                        count = tokens_col.count_documents({}, session=session)
                        logger.info(f"Tokens collection has {count} documents")
                    if count == 0:
                        tokens_col.drop()  # Drop outside transaction
                        logger.info("Tokens collection dropped as it was empty")
                        is_background_task_running = False  # Stop the task
                        break
            else:
                logger.info("Tokens collection does not exist, stopping background task")
                is_background_task_running = False  # Stop if collection doesn't exist
                break
    except Exception as e:
        logger.error(f"Failed to check or drop tokens collection: {str(e)}")
        is_background_task_running = False  # Ensure task stops on error

def start_background_task():
    """Start a background thread to periodically check and drop empty tokens collection."""
    global is_background_task_running, background_thread
    try:
        if not is_background_task_running:
            # Ensure any previous thread is terminated
            if background_thread is not None and background_thread.is_alive():
                logger.warning("Previous background thread still alive, waiting to terminate")
                background_thread.join(timeout=5.0)
                if background_thread.is_alive():
                    logger.error("Previous background thread did not terminate, forcing task stop")
                    is_background_task_running = False
                    return
            is_background_task_running = True
            background_thread = Thread(target=check_and_drop_empty_tokens, daemon=True)
            background_thread.start()
            logger.info("Background task for checking empty tokens collection started")
        else:
            logger.info("Background task already running, no need to start")
    except Exception as e:
        logger.error(f"Failed to start background task: {str(e)}")
        is_background_task_running = False

# ----------------------- Routes -----------------------

@app.route('/generate-token', methods=['POST'])
def generate_token():
    """Generate a short-lived token bound to a (license_key, device_id)."""
    try:
        data = request.get_json(force=True, silent=True) or {}
        license_key = data.get('licenseKey')
        device_id = data.get('deviceId')

        if not license_key or not device_id:
            logger.error("Missing licenseKey or deviceId in request")
            return jsonify({"success": False, "message": "Missing licenseKey or deviceId"}), 400

        def _generate_token():
            lic = validate_license(license_key)
            if not lic:
                raise ValueError("License key not found")
            if not lic.get("active", False):
                raise ValueError("License is deactivated")
            if not lic.get("paid", False):
                raise ValueError("License is unpaid")

            current_mac = lic.get("mac", "")
            if current_mac and current_mac != device_id:
                raise ValueError("License bound to another device")

            if not current_mac:
                licenses_col.update_one(
                    {"key": license_key},
                    {"$set": {"mac": device_id}}
                )

            tokens_col.delete_many({
                "license_key": license_key,
                "device_id": device_id
            })

            token = str(uuid.uuid4())
            token_doc = {
                "token": token,
                "license_key": license_key,
                "device_id": device_id,
                "created_at": datetime.utcnow(),
                "used": False
            }
            tokens_col.insert_one(token_doc)

            logger.info(f"Token generation successful for license_key: {license_key}, device_id: {device_id}")
            start_background_task()  # Start background task after token creation
            return token

        future = executor.submit(_generate_token)
        token = future.result(timeout=10)
        return jsonify({"success": True, "authToken": token}), 200

    except Exception as e:
        logger.error(f"Token generation failed: {str(e)}")
        return jsonify({"success": False, "message": str(e)}), 400

@app.route('/solve-truecaptcha', methods=['POST'])
def solve_truecaptcha():
    """Solve captcha by forwarding image to TrueCaptcha API."""
    try:
        token = request.headers.get('X-Auth-Token')
        if not token:
            logger.error("Missing auth token in request")
            return jsonify({"error": "Missing auth token"}), 401

        data = request.get_json(force=True, silent=True) or {}
        image_content = data.get('imageContent')
        if not image_content:
            logger.error("Missing imageContent in request")
            return jsonify({"error": "Missing imageContent"}), 400

        image_content = strip_data_prefix(image_content)

        def _verify_token():
            token_doc = tokens_col.find_one({"token": token})
            if not token_doc:
                raise ValueError("Invalid or expired token")
            return True

        future = executor.submit(_verify_token)
        future.result(timeout=5)

        if not TRUECAPTCHA_SEMAPHORE.acquire(blocking=False):
            logger.warning("Server busy, semaphore not acquired")
            return jsonify({"error": "Server busy, try again later"}), 429

        def _solve_captcha():
            try:
                payload = {
                    'userid': TRUECAPTCHA_USERID,
                    'apikey': TRUECAPTCHA_APIKEY,
                    'data': image_content
                }
                response = requests.post(
                    TRUECAPTCHA_ENDPOINT,
                    json=payload,
                    timeout=20
                )
                response.raise_for_status()
                result = response.json().get('result')
                if not result:
                    raise ValueError("No result from TrueCaptcha")
                return result
            except requests.exceptions.RequestException as e:
                logger.error(f"TrueCaptcha API error: {str(e)}")
                raise ValueError("Captcha service unavailable")
            except Exception as e:
                logger.error(f"Captcha processing error: {str(e)}")
                raise

        try:
            future = executor.submit(_solve_captcha)
            result = future.result(timeout=25)
            logger.info("Captcha solved successfully")
            return jsonify({"result": result}), 200
        except Exception as e:
            logger.error(f"Captcha solving failed: {str(e)}")
            return jsonify({"error": str(e)}), 502
        finally:
            TRUECAPTCHA_SEMAPHORE.release()

    except Exception as e:
        logger.error(f"Captcha solving failed: {str(e)}")
        return jsonify({"error": str(e)}), 400

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    try:
        def _check_db():
            try:
                db.command('ping')
                return True
            except Exception as e:
                logger.error(f"DB health check failed: {str(e)}")
                return False

        future = executor.submit(_check_db)
        db_ok = future.result(timeout=5)
        
        status = {
            "status": "ok",
            "database": "connected" if db_ok else "disconnected",
            "workers": WORKER_COUNT,
            "concurrency": TRUECAPTCHA_CONCURRENCY,
            "timestamp": datetime.utcnow().isoformat(),
            "background_task_running": is_background_task_running
        }
        logger.info(f"Health check: {status}")
        return jsonify(status), 200 if db_ok else 503

    except Exception as e:
        logger.error(f"Health check failed: {str(e)}")
        return jsonify({"status": "error", "error": str(e)}), 500

# ----------------------- Start Background Task -----------------------
if __name__ == '__main__':
    try:
        logger.info(f"Starting server with {WORKER_COUNT} workers")
        start_background_task()  # Start the background task on server startup
        from gevent.pywsgi import WSGIServer
        http_server = WSGIServer(('0.0.0.0', 5000), app)
        http_server.serve_forever()
    except Exception as e:
        logger.error(f"Server failed to start: {str(e)}")
        raise