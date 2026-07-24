"use client";

import { useQuery } from "@tanstack/react-query";
import { notFound, useParams } from "next/navigation";
import { HealthRing } from "@/components/charts/health-ring";
import { RuleTrendChart } from "@/components/charts/rule-trend-chart";
import { AuditRunButton } from "@/components/product/audit-run-button";
import { FindingRow } from "@/components/product/finding-row";
import { NarrativePanel } from "@/components/product/narrative-panel";
import { RuleLegend } from "@/components/product/rule-legend";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs } from "@/components/ui/tabs";
import { getAuditReport, ruleFindingCount, ruleResultKey, serviceLabel } from "@/lib/api";
import { RULES, type RuleId } from "@/lib/rule-meta";
import { useGuardianStore } from "@/lib/store";

export default function ServiceDetailPage() {
  const params = useParams<{ name: string }>();
  const service = decodeURIComponent(params.name);
  if (!service) {
    notFound();
  }
  const history = useGuardianStore((state) => state.auditHistory).filter((entry) =>
    service === "_all_" ? !entry.service : entry.service === service,
  );
  const report = useQuery({ queryKey: ["audit", service], queryFn: () => getAuditReport(service), retry: 0 });
  const cycle = report.data;
  const tabs = RULES.map((rule) => ({ id: rule.id, label: rule.id, count: ruleFindingCount(cycle, rule.id) }));

  return (
    <div className="content-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">Service detail</p>
          <h1>{serviceLabel(service)}</h1>
        </div>
        <AuditRunButton service={service} />
      </header>

      <section className="detail-hero panel">
        <HealthRing score={cycle?.health_score.score ?? 0} />
        <div>
          <span className="eyebrow">Latest audit</span>
          <strong className="mono">{cycle ? Math.round(cycle.health_score.score) : "pending"} / 100</strong>
          <p className="muted">Last run in this browser: {history.at(-1) ? new Date(history.at(-1)!.timestamp).toLocaleString() : "not triggered this session"}</p>
        </div>
      </section>

      <Card>
        <CardHeader><CardTitle>Findings by Rule</CardTitle></CardHeader>
        <CardContent>
          <Tabs<RuleId> initial="R1" tabs={tabs}>
            {(active) => {
              const rule = RULES.find((item) => item.id === active)!;
              const findings = cycle?.findings[ruleResultKey(active)]?.findings ?? [];
              return (
                <div className="tab-panel">
                  <div className="rule-context">
                    <RuleLegend compact />
                    <RuleTrendChart history={history} ruleId={active} />
                  </div>
                  <p className="muted">{rule.description}</p>
                  {findings.length ? findings.map((finding, index) => <FindingRow finding={finding} key={index} ruleId={active} />) : <div className="empty-state">No {active} findings in the latest audit.</div>}
                </div>
              );
            }}
          </Tabs>
        </CardContent>
      </Card>
      <NarrativePanel error={cycle?.narrative_error} narrative={cycle?.narrative} />
    </div>
  );
}
