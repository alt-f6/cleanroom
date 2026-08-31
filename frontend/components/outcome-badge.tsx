import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { outcomeTone, TONE_BADGE_CLASSES } from "@/lib/outcome";
import type { Outcome } from "@/lib/types";

export function OutcomeBadge({ outcome }: { outcome: Outcome }) {
  const tone = outcomeTone(outcome);
  return (
    <Badge variant="outline" className={cn("font-mono text-xs", TONE_BADGE_CLASSES[tone])}>
      {outcome}
    </Badge>
  );
}
