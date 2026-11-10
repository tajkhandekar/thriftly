"""
Evaluate the search agent against eval/queries.jsonl.

  python src/run_eval.py --parse-only     # parser accuracy only (cheap, ~1 min)
  python src/run_eval.py                  # full agent runs (eBay + Gemini)
  python src/run_eval.py --only q01 q19   # a subset
  python src/run_eval.py --out eval/results/run.json

Per query it scores:
  parse      min/max price and condition set vs. the expected values
  results    number of matches surfaced, and how many times it broadened
  precision  share of matches whose title contains one of expect.keywords_any
             (a cheap proxy for relevance; a human pass is still needed)
  violations matches outside the expected price range (should be 0 unless
             the price rung of the broaden ladder fired)
Full runs are saved under eval@thriftly.local and deactivated afterwards so
they don't feed the monitoring job.
"""

import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from db import connect
from query_parser import parse_query

EVAL_FILE = Path(__file__).resolve().parent.parent / "eval" / "queries.jsonl"
EVAL_EMAIL = "eval@thriftly.local"


def load_cases(only=None):
    cases = [json.loads(line) for line in EVAL_FILE.read_text().splitlines() if line.strip()]
    return [c for c in cases if not only or c["id"] in only]


def score_parse(expect, parsed):
    got_cond = set(parsed.conditions or [])
    want_cond = set(expect["conditions"] or [])
    return {
        "price_ok": (parsed.min_price_cents == expect["min_price_cents"]
                     and parsed.max_price_cents == expect["max_price_cents"]),
        "cond_ok": got_cond == want_cond,
        "got": {"min": parsed.min_price_cents, "max": parsed.max_price_cents,
                "conditions": sorted(got_cond) or None, "keywords": parsed.keywords},
    }


def score_matches(expect, matches):
    words = [w.lower() for w in expect["keywords_any"]]

    def on_topic(name):
        name = name.lower().replace("'", "")
        return any(w.replace("'", "") in name for w in words)

    def in_range(cents):
        lo, hi = expect["min_price_cents"], expect["max_price_cents"]
        return (lo is None or cents >= lo) and (hi is None or cents <= hi)

    n = len(matches)
    return {
        "results": n,
        "precision": (sum(on_topic(m["name"]) for m in matches) / n) if n else None,
        "violations": sum(not in_range(m["price_cents"]) for m in matches),
    }


def eval_case(case, parse_only):
    started = time.monotonic()
    row = {"id": case["id"], "query": case["query"]}
    try:
        if parse_only:
            row.update(score_parse(case["expect"], parse_query(case["query"])))
        else:
            from agent import run_query  # heavy imports only for full runs

            result = run_query(case["query"], EVAL_EMAIL)
            row.update(score_parse(case["expect"], result.parsed))
            row.update(score_matches(case["expect"], result.matches))
            row.update(status=result.status, broaden_count=result.broaden_count,
                       search_id=result.search_id, trace_id=result.trace_id,
                       top=[m["name"][:70] for m in result.matches[:3]])
    except Exception as exc:  # one bad case shouldn't sink the run
        row.update(status="error", error=f"{type(exc).__name__}: {exc}")
    row["seconds"] = round(time.monotonic() - started, 1)
    return row


def deactivate_eval_searches():
    conn = connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE searches SET is_active = FALSE WHERE user_id = "
                "(SELECT id FROM users WHERE email = %s);", (EVAL_EMAIL,))
    finally:
        conn.close()


def summarize(rows, parse_only):
    ok = [r for r in rows if r.get("status") != "error"]
    pct = lambda xs: f"{100 * sum(xs) / len(xs):.0f}%" if xs else "n/a"
    lines = [
        f"cases: {len(rows)}   errors: {len(rows) - len(ok)}",
        f"parse  price correct: {pct([r['price_ok'] for r in ok])}   "
        f"condition correct: {pct([r['cond_ok'] for r in ok])}",
    ]
    if not parse_only:
        precisions = [r["precision"] for r in ok if r["precision"] is not None]
        secs = sorted(r["seconds"] for r in ok)
        lines += [
            f"agent  >=1 match: {pct([r['results'] >= 1 for r in ok])}   "
            f">=3 matches: {pct([r['results'] >= 3 for r in ok])}   "
            f"mean matches: {statistics.mean(r['results'] for r in ok):.1f}",
            f"       keyword precision: "
            f"{statistics.mean(precisions):.2f}" if precisions else "       keyword precision: n/a",
            f"       broadened: {pct([r['broaden_count'] > 0 for r in ok])}   "
            f"price violations: {sum(r['violations'] for r in ok)}",
            f"       latency p50 {statistics.median(secs):.0f}s   "
            f"max {secs[-1]:.0f}s" if secs else "",
        ]
    return "\n".join(lines)


def print_table(rows, parse_only):
    for r in rows:
        if r.get("status") == "error":
            print(f"{r['id']}  ERROR {r['error'][:100]}")
            continue
        flags = ("" if r["price_ok"] else " PRICE") + ("" if r["cond_ok"] else " COND")
        line = f"{r['id']}  parse{flags or ' ok'}"
        if not parse_only:
            prec = "  -" if r["precision"] is None else f"{r['precision']:.2f}"
            line += (f"  matches {r['results']:>2}  prec {prec}  broaden {r['broaden_count']}"
                     f"  viol {r['violations']}  {r['seconds']:>5.1f}s")
        print(f"{line}  | {r['query'][:50]}")
        if flags:
            print(f"      got {r['got']}")


def main():
    parser = argparse.ArgumentParser(description="Evaluate the Thriftly agent.")
    parser.add_argument("--parse-only", action="store_true")
    parser.add_argument("--only", nargs="+", metavar="ID")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", type=Path, help="write per-case JSON here")
    args = parser.parse_args()

    cases = load_cases(args.only)
    with ThreadPoolExecutor(args.workers) as pool:
        rows = list(pool.map(lambda c: eval_case(c, args.parse_only), cases))
    if not args.parse_only:
        deactivate_eval_searches()

    print_table(rows, args.parse_only)
    print("\n" + summarize(rows, args.parse_only))
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(rows, indent=2, default=str))
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
