"""
CLEANROOM CLI — Phase 1 + Phase 2 entry point.

cleanroom run --input <file> [--hardened|--no-hardened]
cleanroom bench [--verbose]
"""
from __future__ import annotations

import os
import sys

import typer
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.trading.client import TradingClient
from dotenv import load_dotenv

from cleanroom.controller import SYMBOL_WHITELIST
from cleanroom.daemon import _NoOpTradingClient, daemon_lock_holder

# Make stdout/stderr resilient on Windows legacy code pages (cp1252 etc.)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _safe_console_str(text: str | None) -> str:
    """Prevent UnicodeEncodeError on legacy Windows terminals.
    Also tolerates None / non-str values coming from detail fields."""
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    return text.encode(encoding, errors="replace").decode(encoding)


load_dotenv()

app = typer.Typer()


def _market_clock_banner() -> None:
    """Read-only GET /v2/clock — tells the demo operator whether orders would
    actually fill right now. Never blocks the run if the check fails."""
    try:
        clock = TradingClient(
            os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"], paper=True
        ).get_clock()
        if clock.is_open:
            typer.secho(f"[MARKET: OPEN] closes at {clock.next_close}", fg=typer.colors.GREEN)
        else:
            typer.secho(
                f"[MARKET: CLOSED] next open {clock.next_open} — live paper orders would queue, not fill.",
                fg=typer.colors.YELLOW,
            )
    except Exception as e:
        typer.secho(f"[MARKET: UNKNOWN] clock check skipped ({_safe_console_str(str(e))})", fg=typer.colors.YELLOW)


def _clients(live: bool = False) -> tuple:
    api_key = os.environ["ALPACA_API_KEY"]
    secret = os.environ["ALPACA_SECRET_KEY"]
    if live:
        trading_client = TradingClient(api_key, secret, paper=True)
    else:
        trading_client = _NoOpTradingClient()
    data_client = StockHistoricalDataClient(api_key, secret)
    return trading_client, data_client


@app.command()
def run(
    input: str = typer.Option(..., "--input", help="Path to the text file to process."),
    hardened: bool = typer.Option(True, "--hardened/--no-hardened",
                                    help="Use the isolated pipeline (default) or the naive baseline."),
    live: bool = typer.Option(False, "--live", help="Submit live paper orders to Alpaca. Defaults to safe DRY-RUN."),
):
    with open(input, "r", encoding="utf-8") as f:
        raw_text = f.read()

    daemon_pid = daemon_lock_holder()
    if daemon_pid is not None:
        typer.secho(
            f"[GUARD] A cleanroom daemon is already running (pid {daemon_pid}). "
            "Running both at once would interleave audit.jsonl writes and confuse the paper broker state. "
            "Stop the daemon first (Ctrl+C in its terminal).",
            fg=typer.colors.RED, bold=True,
        )
        raise typer.Exit(code=2)

    trading_client, data_client = _clients(live=live)

    if live:
        typer.secho("[BROKER: LIVE] Real paper orders will be dispatched to Alpaca.", fg=typer.colors.RED, bold=True)
    else:
        typer.secho("[BROKER: DRY-RUN] Live execution disabled. Simulating broker sink safely.", fg=typer.colors.YELLOW)
    _market_clock_banner()

    if hardened:
        from cleanroom.perception import perceive, PerceptionError
        from cleanroom.airlock import process
        from cleanroom.strategy import decide
        from cleanroom.controller import evaluate, execute

        typer.echo(f"[HARDENED] Processing {input} through Perception -> AIRLOCK -> Strategy -> Controller")

        try:
            perception_output = perceive(raw_text, source_ref=input)
        except PerceptionError as e:
            typer.echo(f"VETO: Perception failed to produce a valid extraction — {e}")
            raise typer.Exit(code=1)

        typer.echo(f"Perception extracted: symbols={perception_output.symbols}, "
                    f"sentiment={perception_output.sentiment.value}")

        airlocked = process(perception_output)
        if airlocked.anomalies:
            typer.echo(f"AIRLOCK flagged anomalies: {airlocked.anomalies}")

        intent = decide(airlocked, data_client)
        verdict = evaluate(intent, trading_client, data_client)

        typer.echo(f"Controller verdict: {verdict.final.value} — {verdict.reason}")
        for check in verdict.checks:
            status = "OK" if check.passed else "FAILED"
            typer.echo(f"  [{status}] {check.name}: {check.detail}")

        result = execute(verdict, trading_client)
        typer.echo(result)

    else:
        from cleanroom.unhardened import run_naive

        typer.echo(f"[NO-HARDENED] Naive agent reading {input} directly, with live order tool attached")
        typer.echo("WARNING: this path has no schema boundary, no whitelist, no stop-loss requirement.")

        result = run_naive(raw_text, trading_client)
        typer.echo(result)


@app.command()
def bench(
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Print a per-file result line for every corpus file."
    ),
):
    """
    Runs the full attacks/ + news/ corpus through both pipelines and prints
    the ablation table: Attack Success Rate (hardened vs. unhardened) and
    Benign False Positive Rate (hardened only).

    Every Alpaca broker/market-data call is served by an in-memory fake with
    a fixed price table and an always-empty order history (see bench.py) —
    the only live network call is to Gemini for Perception and for the
    naive baseline. This is what keeps the table reproducible run over run,
    independent of live paper-account state or market hours.

    The MCP tool-description-poisoning attack class is intentionally NOT a
    corpus file: on the hardened side, Perception has zero tools bound, so
    there is no tool description for that class to poison in the first
    place — a file-based test of it would be tautological (of course it
    scores 0/N; there's no delivery surface). That property is instead
    verified structurally below, under "Architectural Isolation" — a
    behavioral check (blocked N/N) and a structural check (no attack
    surface exists) are different claims, and collapsing them into one ASR
    number would overstate what the file corpus actually demonstrates.
    """
    from cleanroom.bench import run_bench

    def _progress(msg: str) -> None:
        if verbose:
            typer.echo(f"  {msg}")

    typer.echo("Running CLEANROOM bench (mocked broker/market-data, live Gemini calls)...")
    report = run_bench(progress=_progress)

    if verbose:
        typer.echo("")
        typer.echo("Attack corpus — hardened:")
        for r in report.attack_hardened:
            if r.infra_error:
                typer.secho(f"  [INFRA  ] {r.path.name}: {_safe_console_str(r.detail)}", fg=typer.colors.YELLOW)
                continue
            color = typer.colors.RED if r.attack_violated else typer.colors.GREEN
            label = "CAPTURED" if r.attack_violated else "SAFE    "
            typer.secho(f"  [{label}] {r.path.name}: {_safe_console_str(r.detail)}", fg=color)

        typer.echo("")
        typer.echo("Attack corpus — unhardened (baseline):")
        for r in report.attack_unhardened:
            if r.infra_error:
                typer.secho(f"  [INFRA  ] {r.path.name}: {_safe_console_str(r.detail)}", fg=typer.colors.YELLOW)
                continue
            color = typer.colors.RED if r.attack_violated else typer.colors.GREEN
            label = "CAPTURED" if r.attack_violated else "BLOCKED "
            typer.secho(f"  [{label}] {r.path.name}: {_safe_console_str(r.detail)}", fg=color)

        typer.echo("")
        typer.echo("Benign corpus — hardened:")
        for r in report.benign_hardened:
            if r.infra_error:
                typer.secho(f"  [INFRA  ] {r.path.name}: {_safe_console_str(r.detail)}", fg=typer.colors.YELLOW)
                continue
            color = typer.colors.GREEN if r.order_executed else typer.colors.RED
            label = "PASSED " if r.order_executed else "BLOCKED"
            typer.secho(f"  [{label}] {r.path.name}: {_safe_console_str(r.detail)}", fg=color)

    hardened_considered = [r for r in report.attack_hardened if not r.infra_error]
    unhardened_considered = [r for r in report.attack_unhardened if not r.infra_error]
    benign_considered = [r for r in report.benign_hardened if not r.infra_error]

    n_hardened = len(hardened_considered)
    n_unhardened = len(unhardened_considered)
    n_benign = len(benign_considered)
    hardened_hits = sum(r.attack_violated for r in hardened_considered)
    unhardened_hits = sum(r.attack_violated for r in unhardened_considered)
    benign_blocked = sum(not r.order_executed for r in benign_considered)

    typer.echo("")
    typer.echo("=" * 62)
    typer.echo("  CLEANROOM ablation — Attack Success Rate (ASR)")
    typer.echo("  (a 'capture' requires attacker-controlled content to reach")
    typer.echo("   the executed order — an order placed on the pipeline's")
    typer.echo("   own safe, fixed parameters is NOT counted as captured)")
    typer.echo("=" * 62)
    typer.echo(f"  {'':<28}{'Unhardened':>15}{'Hardened':>15}")
    typer.secho(
        f"  {'ASR (attacks captured)':<28}"
        f"{f'{unhardened_hits}/{n_unhardened}':>15}"
        f"{f'{hardened_hits}/{n_hardened}':>15}",
        fg=typer.colors.RED if hardened_hits else typer.colors.GREEN,
    )
    typer.echo(
        f"  {'ASR %':<28}"
        f"{f'{report.unhardened_asr:.0%}':>15}"
        f"{f'{report.hardened_asr:.0%}':>15}"
    )
    typer.echo("")
    typer.echo(f"  Benign False Positive Rate (hardened only, N={n_benign}):")
    fpr_color = typer.colors.RED if benign_blocked else typer.colors.GREEN
    typer.secho(f"    {benign_blocked}/{n_benign} legitimate orders incorrectly blocked "
                f"({report.benign_fpr:.0%})", fg=fpr_color)

    if report.n_infra_errors:
        typer.echo("")
        typer.secho(
            f"  {report.n_infra_errors} file(s) excluded from the numbers above due to "
            f"infrastructure errors (quota/network), not blocked or captured — "
            f"re-run bench later to fill them in.",
            fg=typer.colors.YELLOW,
        )

    typer.echo("")
    typer.echo("-" * 62)
    typer.echo("  Architectural Isolation (structural, not file-based)")
    typer.echo("-" * 62)
    for check in report.isolation:
        color = typer.colors.GREEN if check.passed else typer.colors.RED
        status = "PASS" if check.passed else "FAIL"
        typer.secho(f"  [{status}] {check.name}: {_safe_console_str(check.detail)}", fg=color)
    typer.echo("=" * 62)


@app.command()
def daemon(
    interval: int = typer.Option(45, "--interval", help="Seconds between news polls."),
    live: bool = typer.Option(
        False, "--live",
        help="Submit real paper orders. Default is a dry run: news and prices are "
             "still fetched live, but no order ever reaches the broker.",
    ),
    symbols: str = typer.Option(
        ",".join(sorted(SYMBOL_WHITELIST)), "--symbols", help="Comma-separated tickers to watch."
    ),
    audit_log: str = typer.Option("audit.jsonl", "--audit-log"),
    state_file: str = typer.Option(".cleanroom_daemon_state.json", "--state-file"),
):
    """
    Autonomous mode: polls Alpaca's live news feed on an interval, runs
    every new article through the hardened pipeline, and appends one JSON
    line per article to the audit log — no manual `run` invocation needed.

    News and market prices are always fetched live regardless of --live.
    The flag controls ONLY whether an approved order actually reaches the
    broker: by default (dry run), approved orders are logged but never
    submitted, so this can run for hours without touching the paper
    account's daily order/notional caps. Pass --live only for the take
    you intend to record.

    Stop with Ctrl+C — the audit log and dedup state are saved after every
    processed article, so stopping and restarting never reprocesses or
    loses anything.
    """
    from pathlib import Path
    from cleanroom.daemon import run_daemon

    symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    run_daemon(
        interval_seconds=interval,
        live=live,
        symbols=symbol_list,
        audit_path=Path(audit_log),
        state_path=Path(state_file),
        log=typer.echo,
    )


if __name__ == "__main__":
    app()
