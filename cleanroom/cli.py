"""
CLEANROOM CLI — Phase 1 entry point.

cleanroom run --input <file> [--hardened|--no-hardened]
"""
from __future__ import annotations

import os
import typer
from dotenv import load_dotenv

load_dotenv()

from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient

app = typer.Typer()


def _clients() -> tuple[TradingClient, StockHistoricalDataClient]:
    api_key = os.environ["ALPACA_API_KEY"]
    secret = os.environ["ALPACA_SECRET_KEY"]
    trading_client = TradingClient(api_key, secret, paper=True)
    data_client = StockHistoricalDataClient(api_key, secret)
    return trading_client, data_client


@app.command()
def run(
    input: str = typer.Option(..., "--input", help="Path to the text file to process."),
    hardened: bool = typer.Option(True, "--hardened/--no-hardened",
                                    help="Use the isolated pipeline (default) or the naive baseline."),
):
    with open(input, "r", encoding="utf-8") as f:
        raw_text = f.read()

    trading_client, data_client = _clients()

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


if __name__ == "__main__":
    app()