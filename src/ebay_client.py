"""
eBay Browse API client.

Returns search results with shapeL {"itemSummaries": [...]}.

Set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET from your eBay Application Keys page.
"""

import base64
import os

import requests


def get_token():
    """Mint an Application access token (client-credentials grant)."""
    creds = f"{os.environ['EBAY_CLIENT_ID']}:{os.environ['EBAY_CLIENT_SECRET']}"
    basic = base64.b64encode(creds.encode()).decode()
    resp = requests.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {basic}"},
        data={
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_listings(query, limit=50):
    """Search eBay and return the raw Browse response (same shape as the mock)."""
    resp = requests.get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        headers={
            "Authorization": f"Bearer {get_token()}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
        },
        params={"q": query, "limit": limit},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    data.setdefault("itemSummaries", [])  # empty search omits the key
    return data