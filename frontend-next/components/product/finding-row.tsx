"use client";

import { Copy } from "lucide-react";
import { Accordion } from "@/components/ui/accordion";
import { Badge } from "@/components/ui/badge";
import type { RuleId } from "@/lib/rule-meta";

function bestIdentifier(finding: Record<string, unknown>) {
  return String(finding.span_name ?? finding.attribute ?? finding.span_id ?? finding.trace_id ?? "unknown span");
}

function detailText(finding: Record<string, unknown>) {
  return String(finding.detail ?? finding.reason ?? finding.message ?? "Rule fired on this telemetry record.");
}

export function FindingRow({ finding, ruleId }: { finding: Record<string, unknown>; ruleId: RuleId }) {
  const identifier = bestIdentifier(finding);
  return (
    <div className="finding-row">
      <Badge rule={ruleId}>{ruleId}</Badge>
      <button className="copy-id mono" onClick={() => navigator.clipboard?.writeText(identifier)} type="button">
        {identifier}
        <Copy size={13} />
      </button>
      <p>{detailText(finding)}</p>
      <Accordion title="Raw finding JSON">
        <pre>{JSON.stringify(finding, null, 2)}</pre>
      </Accordion>
    </div>
  );
}
