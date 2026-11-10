"""
Thriftly search agent: natural-language request -> graded eBay matches.

Pipeline for one run of a saved search:
  1. parse      raw query -> keywords, description, hard filters (query_parser)
                (first run only; saved to searches.parsed_params +
                query_embedding and reused on every re-run)
  2. fetch      eBay Browse for the keywords, price filter applied by eBay
  3. ingest     upsert listings + embed new ones (ingestion_pipeline)
  4. search     hybrid_search over the whole catalog with the filters
  5. grade      the model scores each new candidate 0-1 against the request
  6. broaden    too few good matches -> next rung of BROADEN_LADDER, repeat 2-5

Good matches (score >= RELEVANCE_THRESHOLD) go to search_matches; every step
is logged to agent_traces.tools.

  python src/agent.py "used patagonia fleece under \$60"
  python src/agent.py --rerun 12          # re-run saved search 12
"""

import contextlib
import io
import time
import traceback
from dataclasses import dataclass, field

import numpy as np
from pgvector.psycopg2 import register_vector
from psycopg2.extras import Json, RealDictCursor, execute_values
from pydantic import BaseModel, Field

import ingestion_pipeline
from db import connect
from ebay_client import fetch_listings
from embeddings import embed_pending, embed_query
from llm import generate_structured
from query_parser import ParsedQuery, parse_query
from search import search

MIN_GOOD_MATCHES = 3       # stop broadening once we have this many
RELEVANCE_THRESHOLD = 0.6  # grade needed to count as a match
FETCH_LIMIT = 50           # eBay results pulled per keyword/price combo
CANDIDATES = 20            # hybrid_search results graded per attempt

DEFAULT_EMAIL = "dev@thriftly.local"


# ---------------------------------------------------------------------------
# Broadening
# ---------------------------------------------------------------------------

def _broaden_keywords(plan, parsed):
    if parsed.broad_keywords.strip().lower() == plan["keywords"].strip().lower():
        return None
    return dict(plan, keywords=parsed.broad_keywords)


def _drop_condition(plan, parsed):
    if not plan["filters"]["conditions"]:
        return None
    return dict(plan, filters=dict(plan["filters"], conditions=None))


def _relax_price(plan, parsed):
    f = plan["filters"]
    if f["min_price_cents"] is None and f["max_price_cents"] is None:
        return None
    return dict(plan, filters=dict(
        f,
        min_price_cents=None if f["min_price_cents"] is None else int(f["min_price_cents"] * 0.75),
        max_price_cents=None if f["max_price_cents"] is None else int(f["max_price_cents"] * 1.25),
    ))


# Applied in order, each on top of the previous rung, skipping rungs that
# change nothing. Least intrusive first: a broader query keeps every hard
# constraint; relaxing price (by 25%) is the last resort.
BROADEN_LADDER = [
    ("broader keywords", _broaden_keywords),
    ("any condition", _drop_condition),
    ("price +/-25%", _relax_price),
]


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------

class Grade(BaseModel):
    id: int = Field(description="The candidate's id, copied exactly.")
    score: float = Field(description="Relevance from 0.0 to 1.0.")


class Grades(BaseModel):
    grades: list[Grade]


GRADE_SYSTEM = """You judge eBay listings against a shopper's request.

Score each candidate from 0.0 to 1.0 on whether it is the item they want:
- 1.0  exactly the kind of item asked for (brand/model/type/style match)
- 0.7  right item type; a detail differs or can't be told from the title
- 0.4  related but a different item (e.g. a vest when they asked for a jacket)
- 0.0  wrong item, accessory, part, lot/bundle of unrelated things, or
       "for parts / not working" unless they asked for that

Price and condition are already filtered; don't score on them.
Return one grade per candidate, using the ids given."""


def grade(raw_query, parsed, rows):
    """Score candidate listings 0-1 against the request. Returns {id: score};
    candidates the model leaves out get 0."""
    if not rows:
        return {}
    lines = "\n".join(
        f"id={r['id']} | {r['name']} | ${r['price_cents'] / 100:.2f} | {r['condition'] or '?'}"
        for r in rows
    )
    prompt = (f"Shopper's request: {raw_query}\n"
              f"What they want: {parsed.description}\n\n"
              f"Candidates:\n{lines}")
    result = generate_structured(prompt, Grades, system=GRADE_SYSTEM)
    wanted = {r["id"] for r in rows}
    scores = {g.id: min(max(g.score, 0.0), 1.0) for g in result.grades if g.id in wanted}
    return {i: scores.get(i, 0.0) for i in wanted}


# ---------------------------------------------------------------------------
# Tracing
# ---------------------------------------------------------------------------

@dataclass
class Trace:
    steps: list = field(default_factory=list)

    @contextlib.contextmanager
    def step(self, tool, **inputs):
        """Time a step and record its inputs and whatever the body puts in
        the yielded `output` dict."""
        output = {}
        started = time.monotonic()
        try:
            yield output
        finally:
            self.steps.append({
                "step": len(self.steps) + 1,
                "tool": tool,
                "input": inputs,
                "output": output,
                "ms": round((time.monotonic() - started) * 1000),
            })


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def get_or_create_user(email):
    """Return the users.id for `email`, creating a login-disabled user if
    needed (real sign-up and passwords arrive with the API in Phase 4)."""
    conn = connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (email, hashed_password) VALUES (%s, '!disabled') "
                "ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email RETURNING id;",
                (email,),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def create_search(raw_query, email=DEFAULT_EMAIL):
    """Save a new search for the user and return its id (not yet run)."""
    user_id = get_or_create_user(email)
    conn = connect()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO searches (user_id, raw_query) VALUES (%s, %s) RETURNING id;",
                (user_id, raw_query),
            )
            return cur.fetchone()[0]
    finally:
        conn.close()


def _load_search(conn, search_id):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, raw_query, parsed_params, query_embedding FROM searches WHERE id = %s;",
            (search_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise ValueError(f"no search with id {search_id}")
    return row


def _start_trace(conn, search_id):
    # status has no 'running' value: a row with ended_at NULL is in flight or
    # crashed, and is finalized in _finish_trace.
    with conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO agent_traces (search_id, status, error_message) "
            "VALUES (%s, 'error', 'running') RETURNING id;",
            (search_id,),
        )
        return cur.fetchone()[0]


def _finish_trace(conn, trace_id, status, result_count, broaden_count, steps, error=None):
    with conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE agent_traces SET status = %s, error_message = %s, result_count = %s, "
            "broaden_count = %s, tools = %s, ended_at = now() WHERE id = %s;",
            (status, error, result_count, broaden_count, Json(steps), trace_id),
        )


def _save_matches(conn, search_id, scores):
    if not scores:
        return
    with conn, conn.cursor() as cur:
        execute_values(
            cur,
            "INSERT INTO search_matches (search_id, listing_id, relevance_score) VALUES %s "
            "ON CONFLICT (search_id, listing_id) "
            "DO UPDATE SET relevance_score = EXCLUDED.relevance_score;",
            [(search_id, lid, score) for lid, score in scores.items()],
        )


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

@dataclass
class AgentResult:
    search_id: int
    trace_id: int
    status: str
    parsed: ParsedQuery
    matches: list      # listing dicts + "relevance_score", best first
    broaden_count: int
    steps: list


def _quietly(fn, *args, **kwargs):
    """Call a pipeline function without its progress prints."""
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


def run_search(search_id):
    """Run (or re-run) saved search `search_id` end to end. Always records an
    agent_traces row; re-raises unexpected errors after logging them."""
    conn = connect()
    register_vector(conn)
    trace = Trace()
    trace_id = _start_trace(conn, search_id)
    broaden_count = 0
    try:
        search_row = _load_search(conn, search_id)
        raw_query = search_row["raw_query"]

        # 1. parse once; re-runs reuse the saved plan and embedding
        if search_row["parsed_params"] is None:
            with trace.step("parse_query", raw_query=raw_query) as out:
                parsed = parse_query(raw_query)
                out.update(parsed.model_dump())
            with trace.step("embed_query", text=parsed.description):
                query_embedding = np.array(embed_query(parsed.description))
            with conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE searches SET parsed_params = %s, query_embedding = %s WHERE id = %s;",
                    (Json(parsed.model_dump()), query_embedding, search_id),
                )
        else:
            parsed = ParsedQuery.model_validate(search_row["parsed_params"])
            query_embedding = search_row["query_embedding"]

        plan = {"keywords": parsed.keywords, "filters": parsed.filters()}
        ladder = iter(BROADEN_LADDER)
        fetched = set()          # (keywords, min, max) already pulled from eBay
        scores, rows_by_id = {}, {}

        while True:
            f = plan["filters"]

            # 2-3. fetch + ingest, unless this exact eBay query already ran
            fetch_key = (plan["keywords"], f["min_price_cents"], f["max_price_cents"])
            if fetch_key not in fetched:
                fetched.add(fetch_key)
                with trace.step("fetch_ebay", keywords=plan["keywords"],
                                min_price_cents=f["min_price_cents"],
                                max_price_cents=f["max_price_cents"]) as out:
                    items = ingestion_pipeline.parse_items(fetch_listings(
                        plan["keywords"], limit=FETCH_LIMIT,
                        min_price_cents=f["min_price_cents"],
                        max_price_cents=f["max_price_cents"],
                    ))
                    out["items"] = len(_quietly(ingestion_pipeline.save, items))
                with trace.step("embed_listings") as out:
                    out["embedded"] = embed_pending()

            # 4. hybrid search over the whole catalog
            with trace.step("hybrid_search", keywords=plan["keywords"], filters=f) as out:
                rows = search(plan["keywords"], CANDIDATES, f, query_embedding=query_embedding)
                out["candidates"] = len(rows)

            # 5. grade only candidates we haven't scored yet
            new = [r for r in rows if r["id"] not in scores]
            with trace.step("grade", candidates=len(new)) as out:
                new_scores = grade(raw_query, parsed, new)
                out["good"] = sum(s >= RELEVANCE_THRESHOLD for s in new_scores.values())
            scores.update(new_scores)
            rows_by_id.update({r["id"]: r for r in new})

            good = {i: s for i, s in scores.items() if s >= RELEVANCE_THRESHOLD}
            if len(good) >= MIN_GOOD_MATCHES:
                break

            # 6. broaden: next rung that actually changes the plan
            for name, rung in ladder:
                broader = rung(plan, parsed)
                if broader is not None:
                    plan = broader
                    broaden_count += 1
                    with trace.step("broaden", rung=name) as out:
                        out.update(keywords=plan["keywords"], filters=plan["filters"])
                    break
            else:
                break  # ladder exhausted

        _save_matches(conn, search_id, good)
        with conn, conn.cursor() as cur:
            cur.execute("UPDATE searches SET last_refreshed_at = now() WHERE id = %s;",
                        (search_id,))

        heavy = {"listing_embedding", "listing_tsv"}
        matches = sorted(
            ({**{k: v for k, v in rows_by_id[i].items() if k not in heavy},
              "relevance_score": s} for i, s in good.items()),
            key=lambda r: r["relevance_score"], reverse=True,
        )
        status = "ok" if matches else "no_results"
        _finish_trace(conn, trace_id, status, len(matches), broaden_count, trace.steps)
        return AgentResult(search_id, trace_id, status, parsed, matches,
                           broaden_count, trace.steps)

    except Exception as exc:
        conn.rollback()
        _finish_trace(conn, trace_id, "error", 0, broaden_count, trace.steps,
                      error=f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}")
        raise
    finally:
        conn.close()


def run_query(raw_query, email=DEFAULT_EMAIL):
    """Create a search for `raw_query` and run it."""
    return run_search(create_search(raw_query, email))


def _print_result(result):
    p = result.parsed
    print(f"search {result.search_id}  trace {result.trace_id}  status={result.status}  "
          f"broadened {result.broaden_count}x")
    print(f"  keywords: {p.keywords!r}  filters: {p.filters()}")
    for s in result.steps:
        detail = {k: v for k, v in s["output"].items() if k in
                  ("items", "embedded", "candidates", "good", "rung", "keywords")}
        print(f"  {s['step']:>2}. {s['tool']:<15} {s['ms']:>6} ms  {detail or ''}")
    print(f"\n{len(result.matches)} match(es):")
    for r in result.matches:
        print(f"  {r['relevance_score']:.2f}  ${r['price_cents'] / 100:>8.2f}  "
              f"{(r['condition'] or '')[:22]:<22}  {r['name'][:60]}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the Thriftly search agent.")
    parser.add_argument("query", nargs="*")
    parser.add_argument("--email", default=DEFAULT_EMAIL, help="owner of the new search")
    parser.add_argument("--rerun", type=int, metavar="SEARCH_ID",
                        help="re-run an existing saved search instead")
    args = parser.parse_args()
    if args.rerun is None and not args.query:
        parser.error("give a query or --rerun SEARCH_ID")

    _print_result(run_search(args.rerun) if args.rerun is not None
                  else run_query(" ".join(args.query), args.email))
