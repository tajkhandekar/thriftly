"""
eBay Marketplace Account Deletion notification endpoint.

The web app (api.py) serves this at /ebay/deletion using challenge_response()
and handle_notification(); running this file directly still starts the
standalone Flask version.

  GET  ?challenge_code=...  -> eBay's one-time validation handshake.
  POST (signed JSON)        -> a real deletion notification. VERIFY the signature
                              first, then purge that eBay user's data, reply 200.

Verification flow (per eBay's Notification API):
  1. Read the x-ebay-signature header, base64-decode it to JSON, pull out `kid`.
  2. GET the public key from the Notification API using `kid` + an app token.
     Cache it ~1 hour (eBay's guidance) so we don't refetch per notification.
  3. ECDSA/SHA1-verify the signature against the RAW request body.
"""

import base64
import json
import os
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from flask import Flask, jsonify, request
from dotenv import load_dotenv

# Reuse the authenticated GET helper from the Browse client.
from ebay_client import ebay_get
from db import connect
import hashlib

load_dotenv()

PUBLIC_KEY_URL = "https://api.ebay.com/commerce/notification/v1/public_key/"

app = Flask(__name__)

# kid -> {"pem": bytes, "expires_at": epoch}. eBay says cache ~1 hour.
_key_cache = {}


def challenge_response(challenge_code):
    """eBay's validation handshake: sha256(code + verification token + URL).
    EBAY_ENDPOINT_URL must be exactly the URL registered with eBay."""
    return hashlib.sha256(
        (challenge_code
         + os.environ["EBAY_VERIFICATION_TOKEN"]
         + os.environ["EBAY_ENDPOINT_URL"]).encode()
    ).hexdigest()


def handle_notification(raw_body, signature_header):
    """Verify a deletion notification and purge that seller's listings.
    Returns True if it was verified and processed. Always acknowledge with
    200 either way (eBay retries otherwise)."""
    if not verify_signature(raw_body, signature_header):
        print("Notification NOT verified — acknowledged but no data touched. "
              "See VERIFY FAIL above for why.")
        return False
    payload = json.loads(raw_body or b"{}")
    username = payload.get("notification", {}).get("data", {}).get("username")
    delete_seller_data(username)
    print("eBay deletion processed for:", username)
    return True


def get_public_key(kid):
    """Fetch eBay's PEM public key for a key id, cached for an hour."""
    hit = _key_cache.get(kid)
    if hit and time.time() < hit["expires_at"]:
        return hit["pem"]

    resp = ebay_get(PUBLIC_KEY_URL + kid)
    # Response includes {"key": "-----BEGIN PUBLIC KEY-----\n...", "algorithm": "ECDSA", "digest": "SHA1"}
    pem = resp.json()["key"].encode()
    _key_cache[kid] = {"pem": pem, "expires_at": time.time() + 3600}
    return pem


def verify_signature(raw_body, signature_header):
    """ECDSA/SHA1-verify the raw POST body against eBay's signature header.

    Logs the precise failure reason so a wrong field-name assumption is
    distinguishable from a signature that genuinely doesn't check out.
    """
    if not signature_header:
        print("VERIFY FAIL: no x-ebay-signature header present.")
        return False
    try:
        meta = json.loads(base64.b64decode(signature_header))
    except (ValueError, TypeError) as exc:
        print(f"VERIFY FAIL: header isn't base64-JSON ({exc}).")
        return False
    try:
        kid = meta["kid"]
        signature = base64.b64decode(meta["signature"])
    except (KeyError, ValueError) as exc:
        print(f"VERIFY FAIL: missing/unexpected field in header {list(meta)} ({exc}).")
        return False
    try:
        pub = serialization.load_pem_public_key(get_public_key(kid))
        pub.verify(signature, raw_body, ec.ECDSA(hashes.SHA1()))
        return True
    except InvalidSignature:
        print("VERIFY FAIL: signature did not match (key/digest/body mismatch).")
        return False
    except Exception as exc:
        print(f"VERIFY FAIL: error during key fetch/verify ({exc}).")
        return False


def delete_seller_data(username):
    """Remove every listing (and its child rows) belonging to a closed account."""
    if not username:
        return
    # price_history and search_matches rows go too via ON DELETE CASCADE.
    # `with conn` only commits/rolls back, so close the connection explicitly.
    conn = connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute("DELETE FROM listings WHERE seller_username = %s", (username,))
    finally:
        conn.close()

@app.route("/ebay/deletion", methods=["GET", "POST"])
def ebay_deletion():
    # --- Validation handshake (GET) — unsigned, no verification needed ---
    if request.method == "GET":
        code = request.args.get("challenge_code", "")
        return jsonify(challengeResponse=challenge_response(code)), 200
    # Verify against the RAW bytes eBay sent
    handle_notification(request.get_data(), request.headers.get("x-ebay-signature"))
    return "", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)