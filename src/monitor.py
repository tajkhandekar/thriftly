"""
Monitoring job: re-run saved searches on their interval and notify users.

One tick (monitor_once):
  1. pick active searches whose interval has elapsed since they last
     refreshed or were last attempted, oldest first, up to --max-searches
  2. for each one, take a per-search advisory lock (so overlapping ticks or
     several workers never run the same search twice), then re-run it with
     the agent (saved plan + embedding, cached grades)
  3. queue notifications: matches new in this run, and price drops of at
     least --min-drop-pct on listings it had already matched. A search's
     first run queues nothing; its first results aren't news.
  4. optionally deactivate listings no fetch has seen for 7 days
  5. send pending digests (see notify.py for EMAIL_BACKEND)

  python src/monitor.py                     # one tick (e.g. from cron)
  python src/monitor.py --loop --every 300  # keep ticking every 5 minutes
"""

import argparse
import sys
import time
import traceback

from agent import run_search
from db import connect
from ingestion_pipeline import deactivate_stale
from notify import queue_new_matches, queue_price_drops, send_pending

LOCK_CLASS = 7_412_027      # advisory-lock namespace for searches
DEFAULT_MAX_SEARCHES = 10   # per tick; each re-run may call the model
DEFAULT_MIN_DROP_PCT = 5

_DUE = """
SELECT id, last_refreshed_at
FROM searches
WHERE is_active
  AND (greatest(last_refreshed_at, last_attempted_at) IS NULL
       OR greatest(last_refreshed_at, last_attempted_at)
          + make_interval(mins => requery_interval_minutes) <= now())
ORDER BY greatest(last_refreshed_at, last_attempted_at) NULLS FIRST, id
LIMIT %s;
"""


def due_searches(conn, limit):
    with conn.cursor() as cur:
        cur.execute(_DUE, (limit,))
        return cur.fetchall()


def _refresh_one(conn, search_id, last_refreshed_at, min_drop_pct):
    """Re-run one search (lock already held) and queue its notifications.
    Returns (new_matches, price_drops) queued."""
    with conn.cursor() as cur:
        cur.execute("UPDATE searches SET last_attempted_at = now() WHERE id = %s;",
                    (search_id,))

    result = run_search(search_id)

    if last_refreshed_at is None:
        return 0, 0  # first run: the initial results aren't news
    new = queue_new_matches(conn, search_id, result.new_match_ids)
    drops = queue_price_drops(conn, search_id, last_refreshed_at, min_drop_pct,
                              exclude_ids=result.new_match_ids)
    return new, drops


def monitor_once(max_searches=DEFAULT_MAX_SEARCHES, min_drop_pct=DEFAULT_MIN_DROP_PCT,
                 deactivate=False, send=True):
    """Run one monitoring tick. Returns a summary dict."""
    summary = {"due": 0, "refreshed": 0, "skipped_locked": 0, "failed": 0,
               "new_matches": 0, "price_drops": 0, "deactivated": 0, "emails": None}
    conn = connect()
    conn.autocommit = True  # advisory locks are session-level; each statement commits
    try:
        due = due_searches(conn, max_searches)
        summary["due"] = len(due)
        for search_id, last_refreshed_at in due:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_try_advisory_lock(%s, %s);", (LOCK_CLASS, search_id))
                if not cur.fetchone()[0]:
                    summary["skipped_locked"] += 1  # another worker has it
                    continue
            try:
                new, drops = _refresh_one(conn, search_id, last_refreshed_at, min_drop_pct)
                summary["refreshed"] += 1
                summary["new_matches"] += new
                summary["price_drops"] += drops
            except Exception as exc:
                # agent_traces has the details; keep going with other searches
                summary["failed"] += 1
                print(f"[monitor] search {search_id} failed: {type(exc).__name__}: {exc}",
                      file=sys.stderr)
                traceback.print_exc(limit=3, file=sys.stderr)
            finally:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(%s, %s);", (LOCK_CLASS, search_id))
    finally:
        conn.close()

    if deactivate:
        summary["deactivated"] = deactivate_stale()
    if send:
        summary["emails"] = send_pending()
    return summary


def main():
    parser = argparse.ArgumentParser(description="Re-run due searches and send alerts.")
    parser.add_argument("--max-searches", type=int, default=DEFAULT_MAX_SEARCHES,
                        help="searches to refresh per tick (each may call the model)")
    parser.add_argument("--min-drop-pct", type=float, default=DEFAULT_MIN_DROP_PCT,
                        help="smallest price drop worth an alert, in percent")
    parser.add_argument("--deactivate-stale", action="store_true",
                        help="also mark listings unseen for 7 days inactive")
    parser.add_argument("--no-send", action="store_true",
                        help="queue notifications but don't send digests")
    parser.add_argument("--loop", action="store_true", help="keep ticking")
    parser.add_argument("--every", type=int, default=300, metavar="SECONDS",
                        help="seconds between ticks with --loop (default 300)")
    args = parser.parse_args()

    while True:
        started = time.monotonic()
        summary = monitor_once(args.max_searches, args.min_drop_pct,
                               args.deactivate_stale, send=not args.no_send)
        print(f"[monitor] {time.strftime('%Y-%m-%d %H:%M:%S')} {summary}", flush=True)
        if not args.loop:
            break
        try:
            time.sleep(max(0, args.every - (time.monotonic() - started)))
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
