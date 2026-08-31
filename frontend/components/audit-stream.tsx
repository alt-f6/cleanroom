"use client";

import { useMemo, useState } from "react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import type { EventRow } from "@/lib/use-audit-events";
import { EventCard } from "./event-card";

type Filter = "all" | "daemon" | "judge";

const FILTERS: { key: Filter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "daemon", label: "Live Feed" },
  { key: "judge", label: "Judge Attacks" },
];

function rowSource(row: EventRow): "daemon" | "judge" {
  return (row.single ?? row.hardened ?? row.unhardened)?.source ?? "daemon";
}

export function AuditStream({ rows }: { rows: EventRow[] }) {
  const [filter, setFilter] = useState<Filter>("all");

  const filtered = useMemo(() => {
    if (filter === "all") return rows;
    return rows.filter((r) => rowSource(r) === filter);
  }, [rows, filter]);

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h2 className="font-mono text-sm font-bold tracking-wide text-zinc-300">
          LIVE AUDIT STREAM
        </h2>
        <div className="flex gap-1">
          {FILTERS.map((f) => (
            <Button
              key={f.key}
              size="sm"
              variant={filter === f.key ? "secondary" : "ghost"}
              className={cn("font-mono text-xs", filter !== f.key && "text-zinc-500")}
              onClick={() => setFilter(f.key)}
            >
              {f.label}
            </Button>
          ))}
        </div>
      </div>

      <div className="max-h-[600px] space-y-2 overflow-y-auto pr-1">
        {filtered.length === 0 && (
          <p className="font-mono text-xs text-zinc-600">no events yet…</p>
        )}
        {filtered.map((row) => (
          <EventCard key={row.key} row={row} />
        ))}
      </div>
    </div>
  );
}
