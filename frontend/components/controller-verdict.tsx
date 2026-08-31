import { cn } from "@/lib/utils";
import type { StageEvent } from "@/lib/types";

export function ControllerVerdict({ event }: { event: StageEvent }) {
  const isVeto = event.final === "VETO";
  const failingBlocks = (event.checks ?? []).filter(
    (c) => c.severity === "block" && !c.passed,
  );
  const otherChecks = (event.checks ?? []).filter(
    (c) => !(c.severity === "block" && !c.passed),
  );
  const hardBlock = failingBlocks.length > 0;

  return (
    <div
      className={cn(
        "rounded-md border-2 p-3 font-mono text-sm",
        !isVeto && "border-emerald-500/60 bg-emerald-950/20",
        isVeto && hardBlock && "border-red-500/70 bg-red-950/30",
        isVeto && !hardBlock && "border-amber-500/70 bg-amber-950/20",
      )}
    >
      <div className="flex items-center justify-between gap-2">
        <span
          className={cn(
            "font-bold tracking-wide",
            !isVeto && "text-emerald-400",
            isVeto && hardBlock && "text-red-400",
            isVeto && !hardBlock && "text-amber-400",
          )}
        >
          CONTROLLER: {event.final}
        </span>
      </div>
      {event.reason && (
        <p className="mt-1 text-zinc-300">{event.reason}</p>
      )}

      {failingBlocks.length > 0 && (
        <div className="mt-2 space-y-1">
          {failingBlocks.map((c, i) => (
            <div
              key={i}
              className="rounded border border-red-500/50 bg-red-950/40 px-2 py-1 text-xs"
            >
              <span className="font-bold text-red-400">BLOCK: {c.name}</span>
              {c.detail && <span className="text-zinc-400"> — {c.detail}</span>}
            </div>
          ))}
        </div>
      )}

      {otherChecks.length > 0 && (
        <details className="mt-2 text-xs text-zinc-400">
          <summary className="cursor-pointer select-none text-zinc-500 hover:text-zinc-300">
            {otherChecks.length} other check{otherChecks.length === 1 ? "" : "s"}
          </summary>
          <div className="mt-1 space-y-1">
            {otherChecks.map((c, i) => (
              <div key={i} className="flex items-center gap-2">
                <span className={c.passed ? "text-emerald-500" : "text-amber-500"}>
                  {c.passed ? "PASS" : "FLAG"}
                </span>
                <span>{c.name}</span>
                {c.detail && <span className="text-zinc-500">— {c.detail}</span>}
              </div>
            ))}
          </div>
        </details>
      )}
    </div>
  );
}
