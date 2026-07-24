"use client";

import { useQueries } from "@tanstack/react-query";
import { AlertCircle, Gauge, GitBranch, Layers, RadioTower } from "lucide-react";
import { HealthScoreCard } from "@/components/product/health-score-card";
import { ServiceCard } from "@/components/product/service-card";
import { AuditRunButton } from "@/components/product/audit-run-button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { RingSkeleton, Skeleton } from "@/components/ui/skeleton";
import { AUDIT_SERVICES, getAuditReport, type AuditCycle } from "@/lib/api";
import { useGuardianStore } from "@/lib/store";

function StatTile({ label, value, icon: Icon }: { label: string; value: number; icon: typeof Gauge }) {
  return (
    <Card className="stat-tile">
      <Icon size={17} />
      <span>{label}</span>
      <strong className="mono">{Number.isFinite(value) ? value.toFixed(1) : "0.0"}</strong>
    </Card>
  );
}

export default function OverviewPage() {
  const history = useGuardianStore((state) => state.auditHistory);
  const reports = useQueries({
    queries: AUDIT_SERVICES.map((service) => ({
      queryKey: ["audit", service],
      queryFn: () => getAuditReport(service),
      retry: 0,
    })),
  });
  const fleetCycle = reports[0]?.data as AuditCycle | undefined;
  const fleetHistory = history.filter((entry) => !entry.service);
  const terms = fleetCycle?.health_score.terms ?? {};
  const loading = reports.some((report) => report.isLoading);
  const errors = reports.filter((report) => report.isError);

  return (
    <div className="content-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">Fleet telemetry trust</p>
          <h1>Audits that cite the exact rule, span, and attribute.</h1>
        </div>
        <AuditRunButton service="_all_" />
      </header>

      {errors.length ? (
        <div className="callout error">
          <AlertCircle size={17} />
          <span>Some reports are not available yet. Run an audit or wait for the backend scheduler’s first cycle.</span>
        </div>
      ) : null}

      <section className="overview-grid">
        {loading ? (
          <Card><CardContent><RingSkeleton /><Skeleton className="wide" /></CardContent></Card>
        ) : (
          <HealthScoreCard cycle={fleetCycle} history={fleetHistory} />
        )}
        <div className="stat-grid">
          <StatTile icon={Gauge} label="Missing-field %" value={terms.missing_field_rate_pct ?? 0} />
          <StatTile icon={RadioTower} label="Cardinality risk" value={terms.cardinality_risk_score ?? 0} />
          <StatTile icon={GitBranch} label="Orphaned-span %" value={terms.orphaned_span_rate_pct ?? 0} />
          <StatTile icon={Layers} label="Truncation %" value={terms.truncation_rate_pct ?? 0} />
        </div>
      </section>

      <Card>
        <CardHeader>
          <CardTitle>Services</CardTitle>
        </CardHeader>
        <CardContent className="service-grid">
          {AUDIT_SERVICES.map((service, index) => (
            <ServiceCard
              cycle={reports[index]?.data as AuditCycle | undefined}
              history={history.filter((entry) => (service === "_all_" ? !entry.service : entry.service === service))}
              key={service}
              service={service}
            />
          ))}
        </CardContent>
      </Card>
    </div>
  );
}
