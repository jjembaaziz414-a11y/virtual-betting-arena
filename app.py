from flask import Flask, jsonify, render_template, request
import uuid

app = Flask(__name__)

# Demo-only in-memory state. This version does not process real-money payments.
users_db = {}
house_profit = 0.0

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/deposit", methods=["POST"])
def api_deposit():
    data = request.json or {}
    device_id = data.get("device_id")
    phone = data.get("phone")
    try:
        amount = float(data.get("amount", 0))
    except (TypeError, ValueError):
        amount = 0

    if not device_id or amount < 500:
        return jsonify({"success": False, "message": "Invalid demo deposit parameters."})

    if device_id not in users_db:
        users_db[device_id] = {"balance": 0.0, "phone": phone}

    external_ref = f"DEP-{uuid.uuid4().hex[:8].upper()}"
    return jsonify({
        "success": True,
        "message": (
            f"Demo deposit request received for {phone} for UGX "
            f"{amount:,.0f}. Reference: {external_ref}"
        ),
        "ref": external_ref,
    })

@app.route("/api/webhook/mtn", methods=["POST"])
def mtn_webhook():
    return jsonify({"status": "received"}), 200

@app.route("/api/webhook/airtel", methods=["POST"])
def airtel_webhook():
    return jsonify({"status": "received"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
