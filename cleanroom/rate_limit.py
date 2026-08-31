"""
Shared Gemini rate-limit handling.

Both bench.py (batch corpus runs) and daemon.py (live polling) call the
Gemini API and must never pace independently against the same per-project
quota — if they did, one process's calls would count against the other's
budget with no coordination, guaranteeing more 429s than either would hit
alone. This module is the single place that knows the model's real RPM
limit and how to back off on a 429/5xx; both callers import it rather than
keeping their own copies.
"""
from __future__ import annotations

import re
import time
from typing import Callable, TypeVar

T = TypeVar("T")

# Observed free-tier limit for gemini-3.5-flash-lite: 15 requests per minute
# (GenerateRequestsPerMinutePerProjectPerModel-FreeTier). Minimum safe spacing
# is 60/15 = 4.0s; padded to 4.5s for jitter/clock-skew margin. This value is
# specific to this model's actual observed quota — if MODEL_NAME changes,
# re-check the real RPM limit before assuming this number still applies.
GEMINI_CALL_DELAY_SECONDS = 4.5

# A 429/5xx is a fact about Google's infrastructure at the moment of the
# call, not a fact about the input that triggered it. Callers must not
# treat an infra failure as a deterministic outcome (e.g. bench.py must not
# count it as "blocked"; daemon.py must not count it as "no trade" in the
# audit log) — they should retry it, and if retries exhaust, mark the
# result as excluded/uncertain rather than guessing.
_INFRA_ERROR_MARKERS = (
    "RESOURCE_EXHAUSTED",
    "ServerError",
    "DeadlineExceeded",
    "UNAVAILABLE",
    "Connection",
    "timeout",
)
MAX_INFRA_RETRIES = 2
DEFAULT_INFRA_RETRY_DELAY = 65.0  # safely past a 60s RPM window if the
                                   # error didn't tell us how long to wait

_last_call_at: float = 0.0


def throttle() -> None:
    """Blocks until at least GEMINI_CALL_DELAY_SECONDS have passed since
    the last call anywhere in this process. Call this immediately before
    every live Gemini request, in bench.py and daemon.py alike."""
    global _last_call_at
    elapsed = time.monotonic() - _last_call_at
    if elapsed < GEMINI_CALL_DELAY_SECONDS:
        time.sleep(GEMINI_CALL_DELAY_SECONDS - elapsed)
    _last_call_at = time.monotonic()


def is_infra_error(message: str) -> bool:
    return any(marker in message for marker in _INFRA_ERROR_MARKERS)


def parse_retry_delay(message: str) -> float:
    """Extracts Google's own suggested retryDelay from the error body when
    present (e.g. "'retryDelay': '53s'"), so a retry waits exactly as long
    as asked rather than guessing."""
    match = re.search(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)", message)
    if match:
        return float(match.group(1)) + 1.0  # small safety margin
    return DEFAULT_INFRA_RETRY_DELAY