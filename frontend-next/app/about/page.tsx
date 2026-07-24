import { ArchitectureDiagram } from "@/components/product/architecture-diagram";
import { ChaosRunButton } from "@/components/product/chaos-run-button";
import { DemoAgentPanel } from "@/components/product/demo-agent-panel";
import { RuleLegend } from "@/components/product/rule-legend";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export default function AboutPage() {
  return (
    <div className="content-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">How it works</p>
          <h1>Guardian audits whether telemetry can be trusted.</h1>
        </div>
        <div className="button-row">
          <ChaosRunButton mode="baseline" />
          <ChaosRunButton mode="chaos" />
        </div>
      </header>
      <ArchitectureDiagram />
      <DemoAgentPanel />
      <Card>
        <CardHeader><CardTitle>Rules</CardTitle></CardHeader>
        <CardContent><RuleLegend /></CardContent>
      </Card>
    </div>
  );
}
