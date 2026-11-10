"""
Accounts and sessions.

Passwords are hashed with scrypt (stdlib, memory-hard) and stored as
  scrypt$<n>$<r>$<p>$<salt b64>$<hash b64>
Users created by the CLI/eval (hashed_password '!disabled') can't log in
until someone with database access sets a password:

  python src/auth.py set-password you@example.com
"""

import base64
import hashlib
import hmac
import os
import re
import secrets

from db import pooled

SESSION_DAYS = int(os.getenv("SESSION_DAYS", "30"))
MIN_PASSWORD_LENGTH = 8

_N, _R, _P = 2 ** 14, 8, 1
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# Hash of a random password: verifying against it keeps login timing the
# same whether or not the email exists.
_DUMMY_HASH = None


class AuthError(ValueError):
    """Bad input or credentials; the message is safe to show the user."""


def normalize_email(email):
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email) or len(email) > 254:
        raise AuthError("Enter a valid email address.")
    return email


def hash_password(password):
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AuthError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()
    return f"scrypt${_N}${_R}${_P}${b64(salt)}${b64(digest)}"


def verify_password(password, stored):
    """True if `password` matches `stored`. Disabled or malformed hashes
    never match."""
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt((password or "").encode(), salt=base64.b64decode(salt),
                                n=int(n), r=int(r), p=int(p), dklen=len(expected))
    except (ValueError, AttributeError):
        return False
    return hmac.compare_digest(actual, expected)


def _dummy_hash():
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    return _DUMMY_HASH


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def create_user(email, password):
    """Create an account. Returns {"id", "email"}."""
    email = normalize_email(email)
    hashed = hash_password(password)
    with pooled() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (email, hashed_password) VALUES (%s, %s) "
                "ON CONFLICT (email) DO NOTHING RETURNING id;",
                (email, hashed),
            )
            row = cur.fetchone()
        if row is None:
            raise AuthError("An account with that email already exists.")
        return {"id": row[0], "email": email}


def authenticate(email, password):
    """Return {"id", "email"} for valid credentials, else raise AuthError."""
    try:
        email = normalize_email(email)
    except AuthError:
        email = None
    row = None
    if email:
        with pooled() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT id, hashed_password FROM users WHERE email = %s;", (email,))
                row = cur.fetchone()
    if row is None:
        verify_password(password, _dummy_hash())  # same work as a real check
        raise AuthError("Wrong email or password.")
    if not verify_password(password, row[1]):
        raise AuthError("Wrong email or password.")
    return {"id": row[0], "email": email}


def set_password(email, password):
    """Set a user's password (e.g. to enable a CLI-created account)."""
    email = normalize_email(email)
    with pooled(autocommit=False) as conn:  # update + sign-out are atomic
        with conn, conn.cursor() as cur:
            cur.execute("UPDATE users SET hashed_password = %s WHERE email = %s;",
                        (hash_password(password), email))
            if cur.rowcount == 0:
                raise AuthError(f"No user with email {email}.")
            # a password change signs out every existing session
            cur.execute("DELETE FROM sessions WHERE user_id = "
                        "(SELECT id FROM users WHERE email = %s);", (email,))


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def _token_hash(token):
    return hashlib.sha256(token.encode()).digest()


def create_session(user_id):
    """Start a session; returns the token to put in the cookie."""
    token = secrets.token_urlsafe(32)
    with pooled() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions (token_hash, user_id, expires_at) "
                "VALUES (%s, %s, now() + make_interval(days => %s));",
                (_token_hash(token), user_id, SESSION_DAYS),
            )
        return token


def user_for_session(token):
    """{"id", "email"} for a live session token, else None."""
    if not token:
        return None
    with pooled() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT u.id, u.email FROM sessions s JOIN users u ON u.id = s.user_id "
                "WHERE s.token_hash = %s AND s.expires_at > now();",
                (_token_hash(token),),
            )
            row = cur.fetchone()
        return {"id": row[0], "email": row[1]} if row else None


def end_session(token):
    if not token:
        return
    with pooled() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE token_hash = %s;", (_token_hash(token),))
            # opportunistic cleanup of expired sessions
            cur.execute("DELETE FROM sessions WHERE expires_at < now();")


if __name__ == "__main__":
    import argparse
    import getpass

    parser = argparse.ArgumentParser(description="Account admin.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("set-password", help="set a user's password (prompts for it)")
    sp.add_argument("email")
    args = parser.parse_args()

    if args.cmd == "set-password":
        pw = getpass.getpass("New password: ")
        if pw != getpass.getpass("Repeat: "):
            raise SystemExit("Passwords don't match.")
        set_password(args.email, pw)
        print(f"Password set for {args.email}; existing sessions signed out.")
