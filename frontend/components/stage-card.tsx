import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { StageEvent } from "@/lib/types";
import { ControllerVerdict } from "./controller-verdict";

function Shell({
  children,
  tone,
}: {
  children: React.ReactNode;
  tone: "neutral" | "clean" | "flag" | "block" | "captured";
}) {
  return (
    <div
      className={cn(
        "animate-in fade-in slide-in-from-bottom-1 rounded-md border p-3 font-mono text-sm duration-300",
        tone === "neutral" && "border-zinc-700 bg-zinc-900/60 text-zinc-400",
        tone === "clean" && "border-emerald-500/50 bg-emerald-950/20 text-zinc-200",
        tone === "flag" && "border-amber-500/60 bg-amber-950/20 text-zinc-200",
        tone === "block" && "border-red-500/60 bg-red-950/25 text-zinc-200",
        tone === "captured" &&
          "animate-pulse border-red-500 bg-red-950/50 text-zinc-100 shadow-[0_0_16px_rgba(239,68,68,0.5)]",
      )}
    >
      {children}
    </div>
  );
}

export function StageCard({ event }: { event: StageEvent }) {
  switch (event.stage) {
    case "perception_start":
      return (
        <Shell tone="neutral">
          <span className="animate-pulse">⋯ perception running</span>
        </Shell>
      );

    case "naive_start":
      return (
        <Shell tone="neutral">
          <span className="animate-pulse">⋯ naive agent running</span>
        </Shell>
      );

    case "perception_done":
      return (
        <Shell tone="clean">
          <div className="font-bold text-emerald-400">PERCEPTION</div>
          <div className="mt-1 text-zinc-300">
            symbols:{" "}
            <span className="text-zinc-100">
              {event.symbols_extracted?.length ? event.symbols_extracted.join(", ") : "none"}
            </span>
          </div>
          <div className="text-zinc-300">
            sentiment: <span className="text-zinc-100">{event.sentiment}</span>
          </div>
        </Shell>
      );

    case "perception_veto":
      return (
        <Shell tone="block">
          <div className="font-bold text-red-400">PERCEPTION VETO</div>
          <div className="mt-1 text-zinc-300">{event.detail}</div>
        </Shell>
      );

    case "infra_error":
      return (
        <Shell tone="neutral">
          <div className="font-bold text-zinc-400">INFRA ERROR</div>
          <div className="mt-1 text-zinc-400">{event.detail}</div>
        </Shell>
      );

    case "airlock_clean":
      return (
        <Shell tone="clean">
          <span className="font-bold text-emerald-400">AIRLOCK: CLEAN</span>
        </Shell>
      );

    case "airlock_flagged":
      return (
        <Shell tone="flag">
          <div className="font-bold text-amber-400">AIRLOCK: FLAGGED</div>
          {event.airlock_anomalies?.length ? (
            <ul className="mt-1 list-inside list-disc text-zinc-300">
              {event.airlock_anomalies.map((a, i) => (
                <li key={i}>{a}</li>
              ))}
            </ul>
          ) : null}
        </Shell>
      );

    case "strategy_veto":
      return (
        <Shell tone="block">
          <div className="font-bold text-red-400">STRATEGY VETO</div>
          <div className="mt-1 text-zinc-300">{event.detail}</div>
        </Shell>
      );

    case "controller_verdict":
      return <ControllerVerdict event={event} />;

    case "execution_result":
      return (
        <Shell tone="clean">
          <div className="font-bold text-emerald-400">EXECUTION</div>
          <div className="mt-1 text-zinc-300">{event.result}</div>
        </Shell>
      );

    case "naive_result":
      return (
        <div className="space-y-2">
          {event.captured && (
            <Badge className="animate-pulse border-red-400 bg-red-600 text-white shadow-[0_0_16px_rgba(239,68,68,0.6)]">
              BROKER: HIJACKED (Sandbox Execution)
            </Badge>
          )}
          <Shell tone={event.captured ? "captured" : "clean"}>
            <div className={cn("font-bold", event.captured ? "text-red-300" : "text-emerald-400")}>
              {event.captured ? "CAPTURED" : "NO ORDER"}
            </div>
            <div className="mt-1 text-zinc-300">{event.result}</div>
          </Shell>
        </div>
      );

    case "naive_error":
      return (
        <Shell tone="neutral">
          <div className="font-bold text-zinc-400">NAIVE AGENT ERROR</div>
          <div className="mt-1 text-zinc-400">{event.detail}</div>
        </Shell>
      );

    default:
      return null;
  }
}
