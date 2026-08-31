import { cn } from "@/lib/utils";
import type { ConnectionStatus } from "@/lib/api";

const STATUS_COPY: Record<ConnectionStatus, string> = {
  connecting: "Connecting…",
  live: "Connected",
  reconnecting: "Reconnecting…",
};

const STATUS_DOT: Record<ConnectionStatus, string> = {
  connecting: "bg-zinc-500 animate-pulse",
  live: "bg-emerald-500",
  reconnecting: "bg-amber-500 animate-pulse",
};

interface StatusBarProps {
  status: ConnectionStatus;
  totalEvents: number;
  totalAttacks: number;
}

export function StatusBar({ status, totalEvents, totalAttacks }: StatusBarProps) {
  return (
    <div className="flex items-center justify-between rounded-md border border-zinc-800 bg-zinc-900/40 px-3 py-1.5 font-mono text-xs text-zinc-400">
      <div className="flex items-center gap-2">
        <span className={cn("size-2 rounded-full", STATUS_DOT[status])} />
        <span>{STATUS_COPY[status]}</span>
      </div>
      <div className="flex gap-4 text-zinc-500">
        <span>{totalEvents} events</span>
        <span>{totalAttacks} attacks</span>
      </div>
    </div>
  );
}
