import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { Mode, StageEvent } from "@/lib/types";
import { StageCard } from "./stage-card";

interface AirlockColumnProps {
  mode: Mode;
  stages: StageEvent[];
  interrupted: boolean;
}

const COPY: Record<Mode, { title: string; subtitle: string }> = {
  unhardened: { title: "AIRLOCK: OFF", subtitle: "unhardened baseline" },
  hardened: { title: "AIRLOCK: ON", subtitle: "hardened pipeline" },
};

export function AirlockColumn({ mode, stages, interrupted }: AirlockColumnProps) {
  const copy = COPY[mode];

  return (
    <Card
      className={cn(
        "border-zinc-800 bg-zinc-950",
        mode === "hardened" ? "border-emerald-900/40" : "border-zinc-800",
      )}
    >
      <CardHeader className="flex flex-row items-center justify-between border-b border-zinc-800 pb-3">
        <div>
          <CardTitle className="font-mono text-base tracking-wide text-zinc-100">
            {copy.title}
          </CardTitle>
          <p className="font-mono text-xs text-zinc-500">{copy.subtitle}</p>
        </div>
        {interrupted && (
          <Badge variant="outline" className="border-zinc-600 text-zinc-400">
            stream interrupted
          </Badge>
        )}
      </CardHeader>
      <CardContent className="space-y-2 pt-3">
        {stages.length === 0 && !interrupted && (
          <p className="font-mono text-xs text-zinc-600">waiting for submission…</p>
        )}
        {stages.map((event, i) => (
          <StageCard key={`${event.stage}-${i}`} event={event} />
        ))}
      </CardContent>
    </Card>
  );
}
