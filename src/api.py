"""
Thriftly web app: JSON API, the static frontend, and eBay's deletion webhook.

  uvicorn api:app --app-dir src --reload --port 8000
  open http://localhost:8000

Auth is a server-side session in an HttpOnly cookie (SameSite=Lax, so other
sites can't make the browser send it with a POST/PATCH/DELETE). Set
COOKIE_SECURE=true when serving over HTTPS.

Search runs (the agent takes 10-40 s) happen in the background: creating or
refreshing a search returns at once, and the client polls GET
/api/searches/{id} until `state` is no longer "queued" / "running".
"""

import os
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from psycopg2.extras import RealDictCursor
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

import auth
from db import pooled
from ebay_deletion_endpoint import challenge_response, handle_notification
from monitor import run_locked

load_dotenv()

COOKIE_NAME = "thriftly_session"
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
MAX_SEARCHES_PER_USER = 50
RUN_STALE_MINUTES = 10      # a run with no end after this long is treated as crashed
WEB_DIR = Path(__file__).resolve().parent / "web"

app = FastAPI(title="Thriftly", docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.exception_handler(auth.AuthError)
def _auth_error(request, exc):
    return JSONResponse({"detail": str(exc)}, status_code=400)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _query(sql, params=(), one=False):
    # autocommit, and no `with conn:` -- in psycopg2 that block opens a
    # transaction even in autocommit mode, costing two extra round trips
    with pooled() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, params)
        if cur.description is None:
            return cur.rowcount
        return cur.fetchone() if one else cur.fetchall()


def current_user(request: Request):
    user = auth.user_for_session(request.cookies.get(COOKIE_NAME))
    if user is None:
        raise HTTPException(401, "Sign in first.")
    return user


def _set_session_cookie(response, user_id):
    response.set_cookie(
        COOKIE_NAME, auth.create_session(user_id),
        max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax",
        secure=COOKIE_SECURE, path="/",
    )


def _run_in_background(search_id):
    """Background task: run the agent; errors are recorded in agent_traces."""
    try:
        if run_locked(search_id) is None:
            print(f"[api] search {search_id} already running elsewhere", file=sys.stderr)
    except Exception as exc:
        print(f"[api] search {search_id} failed: {type(exc).__name__}: {exc}", file=sys.stderr)


# One row per search with its latest run and match count. `state` folds the
# latest trace into: queued | running | ok | no_results | error.
_SEARCH_SELECT = f"""
SELECT s.id, s.raw_query, s.parsed_params, s.is_active AS watching,
       s.requery_interval_minutes, s.last_refreshed_at, s.created_at,
       (SELECT count(*) FROM search_matches m WHERE m.search_id = s.id) AS match_count,
       CASE
         WHEN t.id IS NULL THEN 'queued'
         WHEN t.ended_at IS NULL
              AND t.started_at > now() - interval '{RUN_STALE_MINUTES} minutes' THEN 'running'
         WHEN t.ended_at IS NULL THEN 'error'
         ELSE t.status
       END AS state,
       t.started_at AS last_run_started_at, t.broaden_count
FROM searches s
LEFT JOIN LATERAL (
    SELECT id, status, started_at, ended_at, broaden_count
    FROM agent_traces WHERE search_id = s.id ORDER BY id DESC LIMIT 1
) t ON TRUE
"""


def _owned_search(search_id, user):
    row = _query(_SEARCH_SELECT + " WHERE s.id = %s AND s.user_id = %s;",
                 (search_id, user["id"]), one=True)
    if row is None:
        raise HTTPException(404, "Search not found.")
    return row


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class Credentials(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=200)


@app.post("/api/auth/signup", status_code=201)
def signup(body: Credentials, response: Response):
    user = auth.create_user(body.email, body.password)
    _set_session_cookie(response, user["id"])
    return user


@app.post("/api/auth/login")
def login(body: Credentials, response: Response):
    user = auth.authenticate(body.email, body.password)
    _set_session_cookie(response, user["id"])
    return user


@app.post("/api/auth/logout", status_code=204)
def logout(request: Request, response: Response):
    auth.end_session(request.cookies.get(COOKIE_NAME))
    response.delete_cookie(COOKIE_NAME, path="/")


@app.get("/api/me")
def me(user=Depends(current_user)):
    return user


# ---------------------------------------------------------------------------
# Searches
# ---------------------------------------------------------------------------

class NewSearch(BaseModel):
    query: str = Field(min_length=3, max_length=300)
    watch: bool = False


class SearchUpdate(BaseModel):
    watch: Optional[bool] = None
    requery_interval_minutes: Optional[int] = Field(default=None, ge=15, le=10080)


@app.get("/api/searches")
def list_searches(user=Depends(current_user)):
    return _query(_SEARCH_SELECT + " WHERE s.user_id = %s ORDER BY s.created_at DESC;",
                  (user["id"],))


@app.post("/api/searches", status_code=202)
def create_search(body: NewSearch, tasks: BackgroundTasks, user=Depends(current_user)):
    count = _query("SELECT count(*) AS n FROM searches WHERE user_id = %s;",
                   (user["id"],), one=True)["n"]
    if count >= MAX_SEARCHES_PER_USER:
        raise HTTPException(400, f"You can keep up to {MAX_SEARCHES_PER_USER} searches; "
                                 "delete some first.")
    row = _query(
        "INSERT INTO searches (user_id, raw_query, is_active) VALUES (%s, %s, %s) RETURNING id;",
        (user["id"], body.query.strip(), body.watch), one=True,
    )
    tasks.add_task(_run_in_background, row["id"])
    return _owned_search(row["id"], user)


@app.get("/api/searches/{search_id}")
def get_search(search_id: int, user=Depends(current_user)):
    search = _owned_search(search_id, user)
    search["matches"] = _query(
        """
        SELECT l.id, l.name, l.price_cents, l.condition, l.web_link, l.is_active,
               l.image_urls->>0 AS image_url, m.relevance_score, m.surfaced_at,
               (SELECT ph.price_cents FROM price_history ph
                WHERE ph.listing_id = l.id ORDER BY ph.observed_at, ph.id LIMIT 1)
                   AS first_price_cents
        FROM search_matches m JOIN listings l ON l.id = m.listing_id
        WHERE m.search_id = %s
        ORDER BY l.is_active DESC, m.relevance_score DESC, l.price_cents
        LIMIT 100;
        """,
        (search_id,),
    )
    return search


@app.patch("/api/searches/{search_id}")
def update_search(search_id: int, body: SearchUpdate, user=Depends(current_user)):
    _owned_search(search_id, user)
    if body.watch is not None:
        _query("UPDATE searches SET is_active = %s WHERE id = %s;", (body.watch, search_id))
    if body.requery_interval_minutes is not None:
        _query("UPDATE searches SET requery_interval_minutes = %s WHERE id = %s;",
               (body.requery_interval_minutes, search_id))
    return _owned_search(search_id, user)


@app.post("/api/searches/{search_id}/refresh", status_code=202)
def refresh_search(search_id: int, tasks: BackgroundTasks, user=Depends(current_user)):
    search = _owned_search(search_id, user)
    if search["state"] in ("queued", "running"):
        raise HTTPException(409, "This search is already running.")
    tasks.add_task(_run_in_background, search_id)
    return search


@app.delete("/api/searches/{search_id}", status_code=204)
def delete_search(search_id: int, user=Depends(current_user)):
    _owned_search(search_id, user)
    _query("DELETE FROM searches WHERE id = %s AND user_id = %s;", (search_id, user["id"]))


@app.get("/api/listings/{listing_id}/price-history")
def price_history(listing_id: int, user=Depends(current_user)):
    # only for listings one of the user's searches has surfaced
    visible = _query(
        "SELECT 1 FROM search_matches m JOIN searches s ON s.id = m.search_id "
        "WHERE m.listing_id = %s AND s.user_id = %s LIMIT 1;",
        (listing_id, user["id"]), one=True,
    )
    if visible is None:
        raise HTTPException(404, "Listing not found.")
    return _query(
        "SELECT price_cents, observed_at FROM price_history WHERE listing_id = %s "
        "ORDER BY observed_at, id;",
        (listing_id,),
    )


# ---------------------------------------------------------------------------
# eBay marketplace account deletion (same contract as ebay_deletion_endpoint)
# ---------------------------------------------------------------------------

@app.get("/ebay/deletion")
def ebay_deletion_challenge(challenge_code: str = ""):
    return {"challengeResponse": challenge_response(challenge_code)}


@app.post("/ebay/deletion")
async def ebay_deletion_notification(request: Request):
    raw = await request.body()  # verify against the exact bytes eBay sent
    await run_in_threadpool(handle_notification, raw, request.headers.get("x-ebay-signature"))
    return Response(status_code=200)


# Frontend last, so /api/* and /ebay/* routes win.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
