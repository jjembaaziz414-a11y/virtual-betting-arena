import uuid
import requests
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

# MTN Sandbox Configuration (Replace with your actual keys once generated)
MTN_SUBSCRIPTION_KEY = "YOUR_SUBSCRIPTION_KEY"
MTN_API_USER = "YOUR_API_USER_UUID"
MTN_API_KEY = "YOUR_API_KEY"
MTN_TARGET_ENV = "sandbox"  # Change to "production" when live


def get_mtn_token():
  """Generates an OAuth token from MTN MoMo API"""
  token_url = (
      "https://sandbox.momodeveloper.mtn.com/collection/token/v1_0/token"
  )
  headers = {
      "Ocp-Apim-Subscription-Key": MTN_SUBSCRIPTION_KEY,
  }
  # MTN requires HTTP Basic Auth using apiUser as username and apiKey as password
  response = requests.post(
      token_url, headers=headers, auth=(MTN_API_USER, MTN_API_KEY)
  )
  if response.status_code == 200:
    return response.json().get("access_token")
  return None


@app.route("/")
def index():
  return render_template("index.html")


@app.route("/api/deposit", methods=["POST"])
def api_deposit():
  data = request.get_json()
  phone = data.get("phone")
  amount = data.get("amount")
  network = data.get("network")

  if not phone or not amount:
    return jsonify({"success": False, "error": "Phone and amount are required"}), 400

  if network == "MTN":
    token = get_mtn_token()
    if not token:
      return (
          jsonify({
              "success": False,
              "error": "Failed to authenticate with MTN MoMo",
          }),
          500,
      )

    reference_id = str(uuid.uuid4())
    deposit_url = (
        "https://sandbox.momodeveloper.mtn.com/collection/v1_0/requesttopay"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "X-Reference-Id": reference_id,
        "X-Target-Environment": MTN_TARGET_ENV,
        "Ocp-Apim-Subscription-Key": MTN_SUBSCRIPTION_KEY,
        "Content-Type": "application/json",
    }

    payload = {
        "amount": str(amount),
        "currency": "UGX",
        "externalId": str(uuid.uuid4()),
        "payer": {"partyIdType": "MSISDN", "partyId": phone},
        "payerMessage": "JJ Virtual Betting Deposit",
        "payeeNote": "Account Deposit",
    }

    res = requests.post(deposit_url, headers=headers, json=payload)
    if res.status_code in [200, 202]:
      return jsonify({
          "success": True,
          "message": f"USSD push sent successfully to {phone}!",
          "reference": reference_id,
      })
    else:
      return (
          jsonify({
              "success": False,
              "error": f"MTN Error: {res.text}",
          }),
          400,
      )

  elif network == "Airtel":
    # Placeholder for Airtel logic once your app status is approved
    return (
        jsonify({
            "success": False,
            "error": (
                "Airtel integration is pending developer portal approval."
            ),
        }),
        400,
    )

  return jsonify({"success": False, "error": "Invalid network selected"}), 400


if __name__ == "__main__":
  app.run(debug=True, port=5000)
