import uuid
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# Simple in-memory user and house ledger
users_db = {}  # device_id -> {"balance": 0.0, "phone": ""}
house_profit = 0.0

MAIN_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <title>Virtual Betting Arena</title>
    <style>
        body { background: #0d1117; color: #c9d1d9; font-family: Arial, sans-serif; text-align: center; margin: 0; padding: 20px; }
        .card { background: #161b22; border: 1px solid #30363d; border-radius: 8px; max-width: 400px; margin: 20px auto; padding: 20px; }
        .input { width: 90%; padding: 10px; margin: 8px 0; background: #0d1117; border: 1px solid #30363d; color: #fff; border-radius: 4px; }
        .btn { background: #238636; color: white; border: none; padding: 10px 20px; border-radius: 4px; cursor: pointer; font-weight: bold; width: 100%; margin-top: 10px; }
        .btn:hover { background: #2ea043; }
    </style>
</head>
<body>
    <h1>Virtual Betting Arena</h1>
    
    <div class="card">
        <h3>Account Balance</h3>
        <p id="balance" style="font-size: 24px; color: #58a6ff;">UGX 0.00</p>
    </div>

    <div class="card">
        <h3>Instant Mobile Money Deposit</h3>
        <input id="phone" class="input" type="text" placeholder="Phone (e.g. 256771234567)">
        <select id="network" class="input">
            <option value="mtn">MTN MoMo</option>
            <option value="airtel">Airtel Money</option>
        </select>
        <input id="amount" class="input" type="number" placeholder="Amount (Min 500)">
        <button class="btn" onclick="requestDeposit()">Deposit via USSD Push</button>
    </div>

    <script>
        const deviceId = 'DEV-' + Math.random().toString(36).substring(2, 9);
        
        async function requestDeposit() {
            const phone = document.getElementById('phone').value;
            const network = document.getElementById('network').value;
            const amount = parseFloat(document.getElementById('amount').value);

            if (!phone || !amount || amount < 500) {
                alert('Please enter a valid phone number and amount.');
                return;
            }

            const response = await fetch('/api/deposit', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({device_id: deviceId, phone: phone, network: network, amount: amount})
            });
            const data = await response.json();
            alert(data.message);
        }
    </script>
</body>
</html>
"""


@app.route("/")
def index():
  return render_template("index.html")


@app.route("/api/deposit", methods=["POST"])
def api_deposit():
  data = request.json or {}
  device_id = data.get("device_id")
  phone = data.get("phone")
  amount = float(data.get("amount", 0))

  if not device_id or amount < 500:
    return jsonify(
        {"success": False, "message": "Invalid deposit parameters."}
    )

  if device_id not in users_db:
    users_db[device_id] = {"balance": 0.0, "phone": phone}

  external_ref = f"DEP-{uuid.uuid4().hex[:8].upper()}"

  return jsonify({
      "success": True,
      "message": (
          f"USSD Push sent to {phone} for UGX {amount:,.0f}. Reference:"
          f" {external_ref}"
      ),
      "ref": external_ref,
  })


@app.route("/api/webhook/mtn", methods=["POST"])
def mtn_webhook():
  data = request.json or {}
  # Handle verified MTN deposit confirmations here
  return jsonify({"status": "received"}), 200


@app.route("/api/webhook/airtel", methods=["POST"])
def airtel_webhook():
  data = request.json or {}
  # Handle verified Airtel deposit confirmations here
  return jsonify({"status": "received"}), 200


if __name__ == "__main__":
  app.run(host="0.0.0.0", port=5000, debug=True)