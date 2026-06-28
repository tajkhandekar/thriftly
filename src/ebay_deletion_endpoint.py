"""
eBay Marketplace Account Deletion notification endpoint.

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

import psycopg2
import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from flask import Flask, jsonify, request
from dotenv import load_dotenv

# Reuse the app-token minter from the Browse client.
from ebay_client import get_token
import hashlib

load_dotenv()

VERIFICATION_TOKEN = os.environ["EBAY_VERIFICATION_TOKEN"]
ENDPOINT_URL = os.environ["EBAY_ENDPOINT_URL"]

PUBLIC_KEY_URL = "https://api.ebay.com/commerce/notification/v1/public_key/"

DB = dict(
    host=os.getenv("DB_HOST", "localhost"),
    dbname=os.getenv("DB_NAME", "thriftly"),
    user=os.getenv("DB_USER", os.getenv("USER")),
    password=os.getenv("DB_PASSWORD", ""),
    port=os.getenv("DB_PORT", "5432"),
)

app = Flask(__name__)

# kid -> {"pem": bytes, "expires_at": epoch}. eBay says cache ~1 hour.
_key_cache = {}


def get_public_key(kid):
    """Fetch eBay's PEM public key for a key id, cached for an hour."""
    hit = _key_cache.get(kid)
    if hit and time.time() < hit["expires_at"]:
        return hit["pem"]

    resp = requests.get(
        PUBLIC_KEY_URL + kid,
        headers={"Authorization": f"Bearer {get_token()}"},
        timeout=30,
    )
    resp.raise_for_status()
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
    with psycopg2.connect(**DB) as conn, conn.cursor() as cur:
        sub = "(SELECT id FROM listings WHERE seller_username = %s)"
        cur.execute(f"DELETE FROM price_history  WHERE listing_id IN {sub}", (username,))
        cur.execute(f"DELETE FROM search_matches WHERE listing_id IN {sub}", (username,))
        cur.execute("DELETE FROM listings WHERE seller_username = %s", (username,))

@app.route("/ebay/deletion", methods=["GET", "POST"])
def ebay_deletion():
    # --- Validation handshake (GET) — unsigned, no verification needed ---
    if request.method == "GET":
        challenge_code = request.args.get("challenge_code", "")
        digest = hashlib.sha256(
            (challenge_code + VERIFICATION_TOKEN + ENDPOINT_URL).encode()
        ).hexdigest()
        return jsonify(challengeResponse=digest), 200

    raw_body = request.get_data()

    # Verify against the RAW bytes eBay sent
    if verify_signature(raw_body, request.headers.get("x-ebay-signature")):
        payload = json.loads(raw_body or b"{}")
        username = payload.get("notification", {}).get("data", {}).get("username")
        delete_seller_data(username)
        print("eBay deletion processed for:", username)
    else:
        print("Notification NOT verified — acknowledged but no data touched. "
              "See VERIFY FAIL above for why.")
    return "", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)