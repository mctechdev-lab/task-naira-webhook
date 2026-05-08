from flask import Flask, request, jsonify
import sqlite3
import hashlib
import logging
from datetime import datetime
import requests

# ==========================================
# CONFIGURATION
# ==========================================
WANNADS_SECRET = "52716d365c"
USD_TO_NGN_RATE = 1100
USER_SPLIT = 0.70  # User gets 70%
ADMIN_SPLIT = 0.30  # You keep 30%

BOT_TOKEN = "8682910268:AAF9xv2KpzY43p58WPD6HNXKgP8iibcpPz4"
CHANNEL_USERNAME = "@task_naira"

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# ==========================================
# DATABASE HELPER
# ==========================================
def db_query(query, params=(), fetchone=False, fetchall=False, commit=False):
    try:
        conn = sqlite3.connect('bot_data.db', timeout=10)
        c = conn.cursor()
        c.execute(query, params)
        res = None
        if fetchone: res = c.fetchone()
        if fetchall: res = c.fetchall()
        if commit: conn.commit()
        conn.close()
        return res
    except Exception as e:
        logging.error(f"Database error: {e}")
        return None

# ==========================================
# REFERRAL COMMISSION PAYMENT
# ==========================================
def get_active_referrers(user_id):
    """Get all active referrers (not expired) for a user."""
    now = datetime.now().isoformat()
    results = db_query(
        "SELECT referrer_id, level FROM referral_tree WHERE user_id=? AND expiry_date > ?",
        (user_id, now), fetchall=True
    )
    return results if results else []

def pay_referral_commissions(user_id, amount):
    """Pay commissions to L1/L2/L3 referrers if still active."""
    rates = {1: 0.10, 2: 0.05, 3: 0.02}  # 10%, 5%, 2%
    
    referrers = get_active_referrers(user_id)
    for ref_id, level in referrers:
        commission = amount * rates[level]
        db_query("UPDATE users SET balance=balance+? WHERE user_id=?", (commission, ref_id), commit=True)
        
        # Log transaction
        db_query(
            "INSERT INTO transactions (user_id, type, amount, description, timestamp) VALUES (?, ?, ?, ?, ?)",
            (ref_id, 'referral_commission', commission, f'L{level} CPA commission from user {user_id}', datetime.now().isoformat()),
            commit=True
        )

# ==========================================
# PAYMENT PROOF POSTING
# ==========================================
def post_payment_proof(user_id, username, amount):
    """Post CPA payment proof to @task_naira channel."""
    try:
        text = f"✅ **CPA TASK COMPLETED**\n\n👤 User: @{username or user_id}\n💰 Earned: ₦{amount:,.2f}\n🕒 {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHANNEL_USERNAME,
            "text": text,
            "parse_mode": "Markdown"
        }
        requests.post(url, json=payload, timeout=10)
        logging.info(f"Payment proof posted for user {user_id}")
    except Exception as e:
        logging.error(f"Failed to post payment proof: {e}")

def send_telegram_notification(user_id, message):
    """Send notification to user via Telegram bot."""
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": user_id,
            "text": message,
            "parse_mode": "Markdown"
        }
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logging.error(f"Failed to send Telegram notification: {e}")

# ==========================================
# POSTBACK VERIFICATION
# ==========================================
def verify_postback(params):
    """Verify Wannads postback signature."""
    # Wannads sends: userId, offerName, amount, hash
    # Hash = md5(userId + offerName + amount + SECRET)
    
    user_id = params.get('userId', '')
    offer_name = params.get('offerName', '')
    amount = params.get('amount', '')
    received_hash = params.get('hash', '')
    
    # Generate expected hash
    raw_string = f"{user_id}{offer_name}{amount}{WANNADS_SECRET}"
    expected_hash = hashlib.md5(raw_string.encode()).hexdigest()
    
    if received_hash == expected_hash:
        return True
    else:
        logging.warning(f"Invalid postback signature. Expected: {expected_hash}, Got: {received_hash}")
        return False

# ==========================================
# POSTBACK ENDPOINT
# ==========================================
@app.route('/postback', methods=['GET', 'POST'])
def postback():
    try:
        # Get parameters (Wannads sends via GET)
        params = request.args.to_dict()
        
        logging.info(f"Received postback: {params}")
        
        # Verify signature
        if not verify_postback(params):
            logging.error("Postback verification FAILED")
            return jsonify({"status": "error", "message": "Invalid signature"}), 403
        
        # Extract data
        user_id = int(params.get('userId', 0))
        offer_name = params.get('offerName', 'Unknown Offer')
        usd_amount = float(params.get('amount', 0))
        
        if user_id == 0 or usd_amount == 0:
            logging.error("Invalid user_id or amount")
            return jsonify({"status": "error", "message": "Invalid data"}), 400
        
        # Check if user exists
        user = db_query("SELECT username FROM users WHERE user_id=?", (user_id,), fetchone=True)
        if not user:
            logging.error(f"User {user_id} not found in database")
            return jsonify({"status": "error", "message": "User not found"}), 404
        
        # Convert USD to NGN
        ngn_total = usd_amount * USD_TO_NGN_RATE
        
        # Apply 70/30 split
        user_amount = ngn_total * USER_SPLIT
        admin_amount = ngn_total * ADMIN_SPLIT
        
        # Credit user
        db_query("UPDATE users SET balance=balance+? WHERE user_id=?", (user_amount, user_id), commit=True)
        
        # Log transaction
        db_query(
            "INSERT INTO transactions (user_id, type, amount, description, timestamp) VALUES (?, ?, ?, ?, ?)",
            (user_id, 'cpa_earning', user_amount, f'CPA: {offer_name} (${usd_amount:.2f})', datetime.now().isoformat()),
            commit=True
        )
        
        # Pay referral commissions
        pay_referral_commissions(user_id, user_amount)
        
        # Post payment proof to channel
        post_payment_proof(user_id, user[0], user_amount)
        
        # Notify user
        send_telegram_notification(
            user_id,
            f"✅ **CPA Task Completed!**\n\n"
            f"💼 Offer: {offer_name}\n"
            f"💰 You earned: ₦{user_amount:,.2f}\n"
            f"💵 Original: ${usd_amount:.2f} → ₦{ngn_total:,.2f}\n\n"
            f"Keep earning! 🚀"
        )
        
        logging.info(f"Successfully credited user {user_id} with ₦{user_amount:,.2f} (70% of ₦{ngn_total:,.2f})")
        
        return jsonify({"status": "success", "message": "Postback processed"}), 200
        
    except Exception as e:
        logging.error(f"Postback processing error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

# ==========================================
# HEALTH CHECK
# ==========================================
@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "message": "Webhook server is running"}), 200

# ==========================================
# RUN SERVER
# ==========================================
if __name__ == '__main__':
    # For local testing
    app.run(host='0.0.0.0', port=5000, debug=True)
