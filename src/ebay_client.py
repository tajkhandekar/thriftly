"""
eBay Browse API client.

Returns search results with shape {"itemSummaries": [...]}.

Set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET from your eBay Application Keys page.
"""

import base64
import os
import time

import requests


# Cached application token: {"token": str, "expires_at": epoch seconds}.
_token_cache = {}
_EXPIRY_MARGIN_SECONDS = 60  # refresh a bit early so we never send a stale token


def get_token():
    """Return an Application access token, minting a new one only when the
    cached token is missing or about to expire (eBay tokens last ~2 hours)."""
    if _token_cache and time.time() < _token_cache["expires_at"]:
        return _token_cache["token"]

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
    body = resp.json()
    _token_cache["token"] = body["access_token"]
    _token_cache["expires_at"] = (
        time.time() + body.get("expires_in", 7200) - _EXPIRY_MARGIN_SECONDS
    )
    return _token_cache["token"]


def ebay_get(url, headers=None, **kwargs):
    """GET an eBay API with the app token. If eBay rejects the cached token
    (401: revoked or rotated early), drop it, mint a fresh one, and retry once.
    Raises for any non-2xx final response."""
    for attempt in range(2):
        resp = requests.get(
            url,
            headers={**(headers or {}), "Authorization": f"Bearer {get_token()}"},
            timeout=30,
            **kwargs,
        )
        if resp.status_code != 401 or attempt:
            break
        _token_cache.clear()
    resp.raise_for_status()
    return resp


def fetch_listings(query, limit=50, min_price_cents=None, max_price_cents=None):
    """Search eBay and return the raw Browse response (same shape as the mock).
    Price bounds are applied by eBay, so the `limit` results are all in range."""
    params = {"q": query, "limit": limit}
    if min_price_cents is not None or max_price_cents is not None:
        lo = "" if min_price_cents is None else f"{min_price_cents / 100:.2f}"
        hi = "" if max_price_cents is None else f"{max_price_cents / 100:.2f}"
        params["filter"] = f"price:[{lo}..{hi}],priceCurrency:USD"
    resp = ebay_get(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        headers={"X-EBAY-C-MARKETPLACE-ID": "EBAY_US"},
        params=params,
    )
    data = resp.json()
    data.setdefault("itemSummaries", [])  # empty search omits the key
    return data