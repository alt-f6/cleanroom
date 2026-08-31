import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import type { AuditEvent } from "@/lib/types";
import type { EventRow } from "@/lib/use-audit-events";
import { OutcomeBadge } from "./outcome-badge";

function formatTime(timestamp: string): string {
  try {
    return new Date(timestamp).toLocaleTimeString();
  } catch {
    return timestamp;
  }
}

function EventDetails({ event }: { event: AuditEvent }) {
  return (
    <div className="space-y-1">
      <div className="flex flex-wrap items-center gap-2">
        <OutcomeBadge outcome={event.outcome} />
        <span className="font-mono text-xs text-zinc-500">{formatTime(event.timestamp)}</span>
        {event.source === "judge" && (
          <span className="font-mono text-[10px] uppercase tracking-wide text-zinc-600">judge</span>
        )}
      </div>
      <p className="font-mono text-sm text-zinc-200">{event.headline}</p>
      {(event.symbols_extracted?.length || event.sentiment) && (
        <p className="font-mono text-xs text-zinc-500">
          {event.symbols_extracted?.length ? event.symbols_extracted.join(", ") : "no symbols"}
          {event.sentiment ? ` · ${event.sentiment}` : ""}
        </p>
      )}
      {event.reason && <p className="font-mono text-xs text-zinc-400">{event.reason}</p>}
      {event.detail && <p className="font-mono text-xs text-zinc-500">{event.detail}</p>}
      {event.execution_result && (
        <p className="font-mono text-xs text-zinc-400">{event.execution_result}</p>
      )}
      {event.airlock_anomalies?.length ? (
        <ul className="list-inside list-disc font-mono text-xs text-amber-400/80">
          {event.airlock_anomalies.map((a, i) => (
            <li key={i}>{a}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export function EventCard({ row }: { row: EventRow }) {
  if (row.single) {
    return (
      <Card className="border-zinc-800 bg-zinc-950/60 py-3">
        <CardContent className="px-4">
          <EventDetails event={row.single} />
        </CardContent>
      </Card>
    );
  }

  const headline = row.hardened?.headline ?? row.unhardened?.headline ?? "";

  return (
    <Card className="border-zinc-700 bg-zinc-950/60 py-3">
      <CardContent className="space-y-3 px-4">
        <div className="flex items-center justify-between">
          <p className="font-mono text-xs uppercase tracking-wide text-zinc-500">
            judge attack · {row.trace_id}
          </p>
          <p className="font-mono text-sm text-zinc-200">{headline}</p>
        </div>
        <div className="grid grid-cols-1 gap-3 border-t border-zinc-800 pt-3 sm:grid-cols-2">
          <div>
            <p className={cn("mb-1 font-mono text-[10px] uppercase tracking-wide text-zinc-600")}>
              unhardened
            </p>
            {row.unhardened ? (
              <EventDetails event={row.unhardened} />
            ) : (
              <p className="animate-pulse font-mono text-xs text-zinc-600">waiting…</p>
            )}
          </div>
          <div>
            <p className="mb-1 font-mono text-[10px] uppercase tracking-wide text-zinc-600">
              hardened
            </p>
            {row.hardened ? (
              <EventDetails event={row.hardened} />
            ) : (
              <p className="animate-pulse font-mono text-xs text-zinc-600">waiting…</p>
            )}
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
