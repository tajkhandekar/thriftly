"""
Notification queue and email digests.

The monitor queues rows in `notifications`; send_pending() batches each
user's unsent rows into one digest email and marks them sent once delivered.

EMAIL_BACKEND picks delivery:
  console  (default) print the digest; nothing leaves the machine
  file     write .eml files to outbox/ (open them in any mail client)
  smtp     send for real via SMTP_HOST / SMTP_PORT / SMTP_USERNAME /
           SMTP_PASSWORD (STARTTLS) from EMAIL_FROM. Addresses on the
           reserved .local domain (dev/eval users) are skipped.

  python src/notify.py            # send everything pending
  python src/notify.py --preview  # print pending digests, mark nothing
"""

import os
import smtplib
import sys
from collections import defaultdict
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from dotenv import load_dotenv
from psycopg2.extras import RealDictCursor

from db import connect

load_dotenv()

EMAIL_BACKEND = os.getenv("EMAIL_BACKEND", "console")
EMAIL_FROM = os.getenv("EMAIL_FROM", "Thriftly <alerts@thriftly.local>")
OUTBOX_DIR = Path(__file__).resolve().parent.parent / "outbox"


# ---------------------------------------------------------------------------
# Queueing (called by the monitor)
# ---------------------------------------------------------------------------

def queue_new_matches(conn, search_id, listing_ids):
    """Queue a new_match notification per listing. Returns how many were new."""
    if not listing_ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO notifications (user_id, search_id, listing_id, kind, price_cents)
            SELECT s.user_id, s.id, l.id, 'new_match', l.price_cents
            FROM searches s JOIN listings l ON l.id = ANY(%s)
            WHERE s.id = %s
            ON CONFLICT ON CONSTRAINT uq_notification DO NOTHING;
            """,
            (list(listing_ids), search_id),
        )
        return cur.rowcount


def queue_price_drops(conn, search_id, since, min_drop_pct, exclude_ids=()):
    """Queue a price_drop notification for each listing this search already
    matched whose price fell by at least `min_drop_pct` percent in a change
    observed after `since`. Returns how many were new."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH history AS (
                SELECT ph.listing_id, ph.price_cents, ph.observed_at,
                       lag(ph.price_cents) OVER (
                           PARTITION BY ph.listing_id ORDER BY ph.observed_at, ph.id
                       ) AS prev_cents
                FROM price_history ph
                JOIN search_matches sm
                  ON sm.listing_id = ph.listing_id AND sm.search_id = %(search_id)s
                WHERE NOT (sm.listing_id = ANY(%(exclude)s))
            )
            INSERT INTO notifications
                (user_id, search_id, listing_id, kind, old_price_cents, price_cents)
            SELECT s.user_id, s.id, h.listing_id, 'price_drop', h.prev_cents, h.price_cents
            FROM history h
            JOIN searches s ON s.id = %(search_id)s
            JOIN listings l ON l.id = h.listing_id AND l.is_active
            WHERE h.observed_at > %(since)s
              AND h.prev_cents IS NOT NULL
              AND h.price_cents <= h.prev_cents * (1 - %(pct)s / 100.0)
            ON CONFLICT ON CONSTRAINT uq_notification DO NOTHING;
            """,
            {"search_id": search_id, "since": since, "pct": min_drop_pct,
             "exclude": list(exclude_ids)},
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# Digests
# ---------------------------------------------------------------------------

_PENDING = """
SELECT n.id, n.user_id, u.email, n.search_id, s.raw_query, n.kind,
       n.old_price_cents, n.price_cents, l.name, l.condition, l.web_link
FROM notifications n
JOIN users u    ON u.id = n.user_id
JOIN searches s ON s.id = n.search_id
JOIN listings l ON l.id = n.listing_id
WHERE n.sent_at IS NULL
ORDER BY n.user_id, n.search_id, n.kind, n.created_at;
"""


def _money(cents):
    return f"${cents / 100:,.2f}"


def build_digest(email, rows):
    """One plain-text email covering all of a user's pending rows."""
    by_search = defaultdict(list)
    for r in rows:
        by_search[(r["search_id"], r["raw_query"])].append(r)

    n_new = sum(r["kind"] == "new_match" for r in rows)
    n_drop = len(rows) - n_new
    parts = []
    if n_new:
        parts.append(f"{n_new} new match{'es' if n_new != 1 else ''}")
    if n_drop:
        parts.append(f"{n_drop} price drop{'s' if n_drop != 1 else ''}")

    lines = []
    for (_, raw_query), items in by_search.items():
        lines.append(f'Your search: "{raw_query}"')
        new = [r for r in items if r["kind"] == "new_match"]
        drops = [r for r in items if r["kind"] == "price_drop"]
        if new:
            lines.append("  New matches:")
            for r in new:
                condition = f" ({r['condition']})" if r["condition"] else ""
                lines.append(f"  - {_money(r['price_cents'])}  {r['name']}{condition}")
                lines.append(f"    {r['web_link']}")
        if drops:
            lines.append("  Price drops:")
            for r in drops:
                lines.append(f"  - {_money(r['old_price_cents'])} -> {_money(r['price_cents'])}"
                             f"  {r['name']}")
                lines.append(f"    {r['web_link']}")
        lines.append("")

    msg = EmailMessage()
    msg["From"] = EMAIL_FROM
    msg["To"] = email
    msg["Subject"] = "Thriftly: " + ", ".join(parts)
    msg.set_content("\n".join(lines).rstrip() + "\n\n- Thriftly\n")
    return msg


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------

def _deliver(msg, backend):
    """Deliver one message. Returns False if it was deliberately skipped."""
    if backend == "console":
        print(f"----- email to {msg['To']} -----\nSubject: {msg['Subject']}\n\n"
              f"{msg.get_content()}")
        return True
    if backend == "file":
        OUTBOX_DIR.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        (OUTBOX_DIR / f"{stamp}-{msg['To'].replace('@', '_at_')}.eml").write_bytes(bytes(msg))
        return True
    if backend == "smtp":
        if msg["To"].endswith(".local"):
            return False  # reserved domain: dev/eval users, undeliverable
        host = os.environ["SMTP_HOST"]
        with smtplib.SMTP(host, int(os.getenv("SMTP_PORT", "587")), timeout=30) as smtp:
            smtp.starttls()
            if os.getenv("SMTP_USERNAME"):
                smtp.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
            smtp.send_message(msg)
        return True
    raise ValueError(f"unknown EMAIL_BACKEND {backend!r} (console, file or smtp)")


def send_pending(backend=None, preview=False):
    """Send one digest per user with unsent notifications and mark those rows
    sent. A failure for one user leaves their rows pending for the next run.
    Returns {"sent": users, "skipped": users, "failed": users, "rows": rows}."""
    backend = backend or EMAIL_BACKEND
    stats = {"sent": 0, "skipped": 0, "failed": 0, "rows": 0}
    conn = connect()
    try:
        with conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(_PENDING)
            pending = cur.fetchall()

        by_user = defaultdict(list)
        for r in pending:
            by_user[(r["user_id"], r["email"])].append(r)

        for (user_id, email), rows in by_user.items():
            msg = build_digest(email, rows)
            if preview:
                _deliver(msg, "console")
                continue
            try:
                delivered = _deliver(msg, backend)
            except Exception as exc:
                print(f"[notify] delivery to {email} failed: {exc}", file=sys.stderr)
                stats["failed"] += 1
                continue
            if not delivered:
                stats["skipped"] += 1
                continue
            with conn, conn.cursor() as cur:
                cur.execute("UPDATE notifications SET sent_at = now() WHERE id = ANY(%s);",
                            ([r["id"] for r in rows],))
            stats["sent"] += 1
            stats["rows"] += len(rows)
        return stats
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Send pending notification digests.")
    parser.add_argument("--preview", action="store_true",
                        help="print pending digests without sending or marking them")
    parser.add_argument("--backend", choices=["console", "file", "smtp"])
    args = parser.parse_args()
    print(send_pending(backend=args.backend, preview=args.preview))
