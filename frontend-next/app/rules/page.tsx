"use client";

import { useMemo, useState } from "react";
import { useQueries } from "@tanstack/react-query";
import { RuleDistributionBar } from "@/components/charts/rule-distribution-bar";
import { RuleLegend } from "@/components/product/rule-legend";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { AUDIT_SERVICES, getAuditReport, ruleFindingCount, serviceLabel } from "@/lib/api";
import { RULES, type RuleId } from "@/lib/rule-meta";

export default function RulesPage() {
  const [filter, setFilter] = useState<RuleId | "all">("all");
  const reports = useQueries({
    queries: AUDIT_SERVICES.map((service) => ({
      queryKey: ["audit", service],
      queryFn: () => getAuditReport(service),
      retry: 0,
    })),
  });
  const rows = useMemo(
    () =>
      reports.map((report, index) => {
        const cycle = report.data;
        const counts = Object.fromEntries(RULES.map((rule) => [rule.id, ruleFindingCount(cycle, rule.id)])) as Record<RuleId, number>;
        return {
          service: AUDIT_SERVICES[index],
          score: cycle?.health_score.score ?? 0,
          counts,
          total: Object.values(counts).reduce((sum, value) => sum + value, 0),
        };
      }),
    [reports],
  );

  return (
    <div className="content-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">Rule Explorer</p>
          <h1>Findings sliced across services.</h1>
        </div>
        <select className="select narrow" value={filter} onChange={(event) => setFilter(event.target.value as RuleId | "all")}>
          <option value="all">All rules</option>
          {RULES.map((rule) => <option key={rule.id} value={rule.id}>{rule.id} {rule.label}</option>)}
        </select>
      </header>
      <Card>
        <CardHeader><CardTitle>Rule reference</CardTitle></CardHeader>
        <CardContent><RuleLegend compact /></CardContent>
      </Card>
      <Card>
        <CardHeader><CardTitle>Cross-service findings</CardTitle></CardHeader>
        <CardContent className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Service</th>
                <th>Score</th>
                <th>Distribution</th>
                {RULES.map((rule) => <th key={rule.id}>{rule.id}</th>)}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const visible = filter === "all" ? row.total : row.counts[filter];
                if (!visible && filter !== "all") {
                  return null;
                }
                return (
                  <tr key={row.service}>
                    <td>{serviceLabel(row.service)}</td>
                    <td className="mono">{Math.round(row.score)}</td>
                    <td><RuleDistributionBar counts={row.counts} /></td>
                    {RULES.map((rule) => (
                      <td key={rule.id}>{row.counts[rule.id] ? <Badge rule={rule.id}>{row.counts[rule.id]}</Badge> : <span className="muted mono">0</span>}</td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </CardContent>
      </Card>
    </div>
  );
}
