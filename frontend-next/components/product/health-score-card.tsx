"use client";

import { Activity } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { HealthRing, ScoreDelta } from "@/components/charts/health-ring";
import type { AuditCycle } from "@/lib/api";
import type { AuditHistoryEntry } from "@/lib/store";

export function HealthScoreCard({ cycle, history }: { cycle?: AuditCycle; history: AuditHistoryEntry[] }) {
  const previous = history.length > 1 ? history[history.length - 2]?.score : undefined;
  const score = cycle?.health_score.score ?? history.at(-1)?.score ?? 0;

  return (
    <Card className="health-score-card">
      <CardHeader>
        <div>
          <CardTitle>Telemetry Trust</CardTitle>
          <p className="muted">Health Score reflects instrumentation trust, not agent quality.</p>
        </div>
        <Activity color="var(--accent-guardian)" />
      </CardHeader>
      <CardContent className="health-score-content">
        <HealthRing score={score} />
        <div className="score-copy">
          <span className="eyebrow">Current audit</span>
          <strong className="mono">{Math.round(score)} / 100</strong>
          <ScoreDelta current={score} previous={previous} />
          <p className="muted">Raw score {Math.round(cycle?.health_score.raw_score ?? score)}. R7 {cycle?.health_score.r7_included ? "included" : "pending"}.</p>
        </div>
      </CardContent>
    </Card>
  );
}
