import Link from "next/link";
import { HealthRing } from "@/components/charts/health-ring";
import { ScoreSparkline } from "@/components/charts/score-sparkline";
import { Badge } from "@/components/ui/badge";
import { firedRuleIds, serviceLabel, type AuditCycle } from "@/lib/api";
import type { AuditHistoryEntry } from "@/lib/store";

export function ServiceCard({ service, cycle, history }: { service: string; cycle?: AuditCycle; history: AuditHistoryEntry[] }) {
  const fired = firedRuleIds(cycle);
  return (
    <Link className="service-card panel" href={`/service/${encodeURIComponent(service)}`}>
      <div>
        <strong>{serviceLabel(service)}</strong>
        <p className="muted">{cycle ? "Latest audit loaded" : "Waiting for first audit"}</p>
      </div>
      <HealthRing score={cycle?.health_score.score ?? 0} size={92} label="Score" />
      <ScoreSparkline history={history} />
      <div className="rule-strip">
        {fired.length ? fired.map((ruleId) => <Badge key={ruleId} rule={ruleId}>{ruleId}</Badge>) : <span className="clean-chip">clean</span>}
      </div>
    </Link>
  );
}
