"use client";

import { useAuditEvents } from "@/lib/use-audit-events";
import { HackConsole } from "./hack-console";
import { StatusBar } from "./status-bar";
import { AuditStream } from "./audit-stream";

export function Dashboard() {
  const { rows, totalEvents, totalAttacks, status } = useAuditEvents();

  return (
    <div className="space-y-6">
      <StatusBar status={status} totalEvents={totalEvents} totalAttacks={totalAttacks} />
      <HackConsole />
      <AuditStream rows={rows} />
    </div>
  );
}
