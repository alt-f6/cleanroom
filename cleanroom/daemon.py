"""
CLEANROOM daemon — autonomous news-driven operation.

Polls Alpaca's news feed on an interval, runs every new article through the
full hardened pipeline (Perception -> AIRLOCK -> Strategy -> Controller),
and appends one JSON line per article to an audit log. This is what closes
Phase 3's DoD: new journal entries appear over time with no manual `run`
invocation.

Live vs. dry-run is a narrower distinction than it might look:
  - News (NewsClient) and market prices (StockHistoricalDataClient) are
    ALWAYS fetched live from Alpaca, in both modes. Both are read-only and
    carry no execution risk, and faking them would undermine the whole
    "genuinely autonomous on a live feed" claim.
  - --live controls ONLY whether an approved order actually reaches
    submit_order. By default (dry run) the daemon can be left running for
    hours without ever touching the paper account's daily order count or
    notional cap — safe to leave on overnight, safe to run again right
    before a demo without polluting the numbers you'd show on camera.
    Pass --live only for the take you intend to record.

Unverified assumption, flagged explicitly: the exact NewsClient/NewsRequest
field names and response shape below (article.id, .created_at, .headline,
.summary) are based on alpaca-py's public examples, not tested against a
live key from this environment (no network path to Alpaca here). Run one
short poll manually first and adjust field names if your installed
alpaca-py version differs.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from cleanroom.airlock import process
from cleanroom.controller import evaluate, execute
from cleanroom.perception import PerceptionError, perceive
from cleanroom.rate_limit import MAX_INFRA_RETRIES, is_infra_error, parse_retry_delay, throttle
from cleanroom.strategy import decide

STATE_FILE_DEFAULT = Path(".cleanroom_daemon_state.json")
AUDIT_LOG_DEFAULT = Path("audit.jsonl")
LOCK_FILE_DEFAULT = Path(".cleanroom_daemon.lock")

# Dedup window: must comfortably exceed one poll's maximum article count
# (NewsRequest limit=50), because the news API's `start` filter is treated
# as inclusive — every article sharing the boundary timestamp comes back on
# the next poll and only the id set keeps it from being reprocessed.
SEEN_IDS_MAX = 200


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except PermissionError:
        # Windows: the process exists but we can't signal it.
        return True
    except OSError:
        return False
    return True


def daemon_lock_holder(lock_path: Path = LOCK_FILE_DEFAULT) -> int | None:
    """Return the pid of a live daemon holding the lock, or None.
    A lock left behind by a dead process is removed (stale-lock recovery)."""
    if not lock_path.exists():
        return None
    try:
        pid = int(lock_path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        lock_path.unlink(missing_ok=True)
        return None
    if _pid_alive(pid):
        return pid
    lock_path.unlink(missing_ok=True)
    return None


def acquire_daemon_lock(lock_path: Path = LOCK_FILE_DEFAULT) -> None:
    holder = daemon_lock_holder(lock_path)
    if holder is not None:
        raise RuntimeError(
            f"another cleanroom daemon is already running (pid {holder}, lock {lock_path}). "
            "Stop it before starting a second instance — concurrent runs would interleave "
            "audit.jsonl writes and double-process the same articles."
        )
    lock_path.write_text(str(os.getpid()), encoding="utf-8")


def release_daemon_lock(lock_path: Path = LOCK_FILE_DEFAULT) -> None:
    try:
        pid = int(lock_path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return
    if pid == os.getpid():
        lock_path.unlink(missing_ok=True)


class _NoOpTradingClient:
    """Default (dry-run) execution sink. get_orders always returns an empty
    history — correct for dry-run, since nothing real has ever happened on
    this path — and submit_order records the attempt locally without ever
    calling the real broker. Swapped for a real TradingClient only when
    --live is passed."""

    def __init__(self):
        self.submitted_orders: list = []

    def get_orders(self, request):
        return []

    def submit_order(self, order_request):
        order = SimpleNamespace(id=f"dry-run-{len(self.submitted_orders):04d}")
        self.submitted_orders.append(order_request)
        return order


def _load_state(path: Path) -> dict:
    """Loads dedup state, tolerating a missing, corrupt, or malformed file.
    A corrupt file degrades to the safe first-run default (start from now,
    no backlog) rather than crashing the daemon or replaying history."""
    default = {"last_seen_at": None, "last_seen_ids": []}
    if not path.exists():
        return default
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return default
    if not isinstance(raw, dict):
        return default
    ids = raw.get("last_seen_ids")
    if not isinstance(ids, list):
        ids = []
    last_seen_at = raw.get("last_seen_at")
    return {
        "last_seen_at": last_seen_at if isinstance(last_seen_at, str) else None,
        "last_seen_ids": list(dict.fromkeys(ids))[-SEEN_IDS_MAX:],
    }


def _save_state(path: Path, state: dict) -> None:
    """Atomic write: a crash mid-save must never leave a truncated JSON file
    behind — the state file is the only thing standing between a restart
    and reprocessing (re-trading) recently fetched news."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, path)


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _advance_state(state: dict, article_id, created_at) -> None:
    """Records one article in the dedup state, in memory.

    - seen ids: appended, deduplicated, bounded to SEEN_IDS_MAX.
    - last_seen_at: strictly monotonic. It advances only when the new
      timestamp is strictly greater than the stored one; on an equal
      timestamp the cursor stays put and the id set alone disambiguates
      articles sharing that exact created_at. Unparseable or incomparable
      timestamps fall back to text comparison of the ISO strings — the
      cursor never moves backwards on an edge case.
    """
    if article_id is not None:
        seen = state.get("last_seen_ids", [])
        state["last_seen_ids"] = list(dict.fromkeys([*seen, article_id]))[-SEEN_IDS_MAX:]

    if created_at is None:
        return
    new_iso = created_at.isoformat() if hasattr(created_at, "isoformat") else str(created_at)
    prev_iso = state.get("last_seen_at")
    if prev_iso is None:
        state["last_seen_at"] = new_iso
        return
    try:
        newer = _parse_iso(new_iso) > _parse_iso(prev_iso)
    except (ValueError, TypeError):
        newer = new_iso > prev_iso
    if newer:
        state["last_seen_at"] = new_iso


def _append_audit(path: Path, event: dict) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, default=str) + "\n")


def _get_field(article, name: str, default=None):
    """Alpaca's news response shape varies: some SDK configurations return
    typed objects with attributes (article.headline), others return plain
    dicts (article["headline"]) — this tolerates either without the caller
    needing to know which."""
    if isinstance(article, dict):
        return article.get(name, default)
    return getattr(article, name, default)


def _fetch_new_articles(news_client, symbols: list[str], state: dict) -> list:
    """Fetches news since the last seen timestamp and filters out any
    article id already recorded at that exact timestamp boundary (two
    articles can share a created_at down to the second). Returns articles
    sorted oldest-first so state advances monotonically as they're
    processed one at a time.

    Confirmed shape (alpaca-py, non-raw_data mode): get_news() returns a
    NewsSet whose actual payload lives at response.data["news"] — a plain
    list of dicts with real datetime objects for created_at, not strings.
    """
    from alpaca.data.requests import NewsRequest

    kwargs: dict = {"symbols": ",".join(symbols), "limit": 50}
    if state.get("last_seen_at"):
        # Stored as an ISO string in state (JSON has no datetime type);
        # parse back to a real datetime before handing it to NewsRequest.
        kwargs["start"] = datetime.fromisoformat(state["last_seen_at"])

    response = news_client.get_news(NewsRequest(**kwargs))

    if hasattr(response, "data") and isinstance(response.data, dict):
        articles = response.data.get("news", [])
    elif isinstance(response, dict):
        articles = response.get("news", [])
    else:
        articles = getattr(response, "news", response)

    # Filter against BOTH the persisted seen-id window and ids already
    # accepted from this same batch — the API can return an article twice
    # in one response, and processing it twice would place two trades.
    seen_ids = set(state.get("last_seen_ids", []))
    fresh = []
    for a in articles:
        article_id = _get_field(a, "id")
        if article_id is not None and article_id in seen_ids:
            continue
        if article_id is not None:
            seen_ids.add(article_id)
        fresh.append(a)
    return sorted(fresh, key=lambda a: _get_field(a, "created_at"))


def _perceive_with_retry(raw_text: str, source_ref: str):
    """Same bounded-retry shape as bench.py's cached wrapper, minus the
    cache — a live feed sees genuinely new content each time, so there is
    nothing to cache. Raises PerceptionError (infra or real) after
    exhausting retries; caller logs and moves to the next article rather
    than crashing the daemon."""
    last_error: PerceptionError | None = None
    for attempt in range(MAX_INFRA_RETRIES + 1):
        throttle()
        try:
            return perceive(raw_text, source_ref=source_ref)
        except PerceptionError as e:
            msg = str(e)
            if is_infra_error(msg) and attempt < MAX_INFRA_RETRIES:
                time.sleep(parse_retry_delay(msg))
                last_error = e
                continue
            raise
    raise last_error  # pragma: no cover


def _process_article(article, trading_client, data_client, audit_path: Path) -> dict:
    """Runs one article through the full hardened pipeline and appends a
    structured event to the audit log. Never raises — every failure mode
    (infra error, Perception VETO, Strategy decline, Controller VETO,
    broker submission failure, or any unanticipated exception) is captured
    as an event rather than propagated, since one bad article must not
    take the daemon down. The outer safety net exists precisely for the
    failure modes the inner handlers didn't anticipate."""
    event: dict = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "news_id": _get_field(article, "id"),
        "news_created_at": _get_field(article, "created_at"),
        "headline": _get_field(article, "headline", "") or "",
    }
    try:
        return _process_article_unsafe(article, trading_client, data_client, audit_path, event)
    except Exception as e:
        event["outcome"] = "PIPELINE_ERROR"
        event["detail"] = str(e)
        try:
            _append_audit(audit_path, event)
        except Exception:
            pass  # even an unwritable audit log must not kill the daemon
        return event


def _process_article_unsafe(article, trading_client, data_client, audit_path: Path, event: dict) -> dict:
    headline = event["headline"]
    summary = _get_field(article, "summary", "") or ""
    raw_text = f"{headline}\n\n{summary}".strip()
    source_ref = f"alpaca-news:{_get_field(article, 'id', 'unknown')}"

    try:
        perception_output = _perceive_with_retry(raw_text, source_ref)
    except PerceptionError as e:
        event["outcome"] = "INFRA_ERROR" if is_infra_error(str(e)) else "PERCEPTION_VETO"
        event["detail"] = str(e)
        _append_audit(audit_path, event)
        return event

    airlocked = process(perception_output)
    event["symbols_extracted"] = perception_output.symbols
    event["sentiment"] = perception_output.sentiment.value
    if airlocked.anomalies:
        event["airlock_anomalies"] = airlocked.anomalies

    try:
        intent = decide(airlocked, data_client)
    except Exception as e:
        event["outcome"] = "STRATEGY_VETO"
        event["detail"] = str(e)
        _append_audit(audit_path, event)
        return event

    verdict = evaluate(intent, trading_client, data_client, airlocked=airlocked)

    try:
        result_str = execute(verdict, trading_client)
    except Exception as e:
        # evaluate() is fail-closed internally, but submit_order itself can
        # still fail after a PASS — record it rather than crash the loop.
        event["outcome"] = "EXECUTION_ERROR"
        event["reason"] = verdict.reason
        event["detail"] = str(e)
        _append_audit(audit_path, event)
        return event

    event["outcome"] = verdict.final.value
    event["reason"] = verdict.reason
    event["execution_result"] = result_str
    _append_audit(audit_path, event)
    return event


def run_daemon(
    interval_seconds: int,
    live: bool,
    symbols: list[str],
    audit_path: Path = AUDIT_LOG_DEFAULT,
    state_path: Path = STATE_FILE_DEFAULT,
    log: Callable[[str], None] = print,
) -> None:
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.historical.news import NewsClient
    from alpaca.trading.client import TradingClient

    acquire_daemon_lock()

    api_key = os.environ["ALPACA_API_KEY"]
    secret = os.environ["ALPACA_SECRET_KEY"]

    # Always real — read-only, no execution risk regardless of --live.
    data_client = StockHistoricalDataClient(api_key, secret)
    news_client = NewsClient(api_key, secret)

    trading_client = TradingClient(api_key, secret, paper=True) if live else _NoOpTradingClient()

    state = _load_state(state_path)
    if state.get("last_seen_at") is None:
        # First run, no prior state: start the clock at "now" rather than
        # fetching an unfiltered backlog of up to 50 historical articles —
        # otherwise the first poll would burn a large chunk of the daily
        # RPM budget on news that has nothing to do with a live demo.
        state["last_seen_at"] = datetime.now(timezone.utc).isoformat()
        _save_state(state_path, state)
        log("first run — starting from now, not backfilling historical articles")

    mode = "LIVE (orders reach the real paper broker)" if live else "DRY-RUN (no order ever reaches the broker)"
    log(f"CLEANROOM daemon starting — mode={mode}, interval={interval_seconds}s, symbols={symbols}")
    log(f"Audit log: {audit_path}  |  State file: {state_path}")

    try:
        while True:
            try:
                fresh = _fetch_new_articles(news_client, symbols, state)
            except Exception as e:
                log(f"[news fetch failed, will retry next cycle] {e}")
                fresh = []

            if not fresh:
                now_str = datetime.now(timezone.utc).strftime("%H:%M:%S")
                log(f"[{now_str} UTC] polled, no new articles since {state.get('last_seen_at')}")

            for article in fresh:
                # Mark the article seen and persist BEFORE running the
                # pipeline: if the daemon dies mid-article, a restart skips
                # it (at-most-once trade execution) instead of re-running
                # it against the broker — a duplicate trade is strictly
                # worse than one missing audit line.
                _advance_state(
                    state, _get_field(article, "id"), _get_field(article, "created_at")
                )
                _save_state(state_path, state)

                event = _process_article(article, trading_client, data_client, audit_path)

                log(f"[{event.get('news_created_at')}] {event['outcome']:<16} "
                    f"{event.get('headline', '')[:70]}")

            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        log("")
        log("daemon stopped (Ctrl+C) — audit log and state are saved through the last processed article")
    finally:
        release_daemon_lock()