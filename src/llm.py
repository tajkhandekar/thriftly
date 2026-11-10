"""
Shared Gemini client and a structured-output helper.

AGENT_MODEL (env) picks the generation model; embeddings use their own model
in embeddings.py.

- AGENT_RPM (env, default 5 = Gemini free tier) caps generate calls per
  minute across all threads, so parallel work queues instead of hitting 429s.
  Raise it once the project has billing enabled.
- Each call times out after REQUEST_TIMEOUT_S. 429s wait the retry delay
  Gemini sends; 5xx back off exponentially. A busy model fails clearly
  instead of hanging.
"""

import os
import re
import sys
import threading
import time
from collections import deque

from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

load_dotenv()

AGENT_MODEL = os.getenv("AGENT_MODEL", "gemini-3.5-flash")
AGENT_RPM = int(os.getenv("AGENT_RPM", "5"))
REQUEST_TIMEOUT_S = 30
MAX_ATTEMPTS = 5
_TRANSIENT = {429, 500, 502, 503, 504}
MAX_RETRY_WAIT_S = 60  # a longer RetryInfo delay means a daily quota: fail instead

_call_times = deque()
_rate_lock = threading.Lock()


def _wait_for_rate_slot():
    """Block until a call fits within AGENT_RPM over the last 60 seconds."""
    while True:
        with _rate_lock:
            now = time.monotonic()
            while _call_times and now - _call_times[0] >= 60:
                _call_times.popleft()
            if len(_call_times) < AGENT_RPM:
                _call_times.append(now)
                return
            wait = 60 - (now - _call_times[0])
        time.sleep(wait + 0.05)


def _retry_delay(exc, attempt):
    """Seconds to wait before retrying: Gemini's RetryInfo for 429s when
    present, otherwise exponential backoff."""
    details = exc.details.get("error", {}).get("details", []) if isinstance(exc.details, dict) else []
    for d in details:
        if d.get("@type", "").endswith("RetryInfo"):
            match = re.match(r"([\d.]+)s", d.get("retryDelay", ""))
            if match:
                return float(match.group(1)) + 1
    return 2 ** attempt

_client = None


def get_client():
    """Create the Gemini client on first use (reads GEMINI_API_KEY)."""
    global _client
    if _client is None:
        _client = genai.Client()
    return _client


def call_with_retries(fn, rate_limited=True, label=AGENT_MODEL):
    """Run one Gemini request `fn()` with the shared retry policy (and the
    AGENT_RPM limiter for generate calls)."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if rate_limited:
            _wait_for_rate_slot()
        try:
            return fn()
        except errors.APIError as exc:
            if exc.code not in _TRANSIENT or attempt == MAX_ATTEMPTS:
                raise
            delay = _retry_delay(exc, attempt)
            if delay > MAX_RETRY_WAIT_S:
                raise
            print(f"[llm] {exc.code} from {label}, retry {attempt}/{MAX_ATTEMPTS - 1} "
                  f"in {delay:.0f}s", file=sys.stderr)
            time.sleep(delay)


def http_options():
    return types.HttpOptions(timeout=REQUEST_TIMEOUT_S * 1000)


def generate_structured(prompt, schema, system=None, temperature=0.0):
    """Ask the agent model for JSON matching the pydantic `schema` and
    return the parsed model instance. Raises ValueError if the reply
    doesn't parse."""
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        response_mime_type="application/json",
        response_schema=schema,
        http_options=http_options(),
    )
    response = call_with_retries(lambda: get_client().models.generate_content(
        model=AGENT_MODEL, contents=prompt, config=config,
    ))
    if response.parsed is None:
        raise ValueError(f"model returned no parsable {schema.__name__}: {response.text!r}")
    return response.parsed
