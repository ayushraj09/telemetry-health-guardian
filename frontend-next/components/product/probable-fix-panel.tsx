"use client";

import { Wrench } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { ChatResponse } from "@/lib/api";
import { isRuleId, RULE_META, type RuleId } from "@/lib/rule-meta";

type FixPanelProps = {
  fixes: ChatResponse["probable_fixes"];
  /** Optional ordering to match severity_rank from segments */
  ruleOrder?: string[];
};

export function ProbableFixPanel({ fixes, ruleOrder }: FixPanelProps) {
  const entries = Object.entries(fixes).flatMap(([ruleId, fix]): [RuleId, string][] =>
    isRuleId(ruleId) && fix ? [[ruleId, fix]] : [],
  );

  if (!entries.length) {
    return null;
  }

  // Sort by ruleOrder if provided (matches severity_rank from segments)
  const sorted = ruleOrder
    ? [...entries].sort(
        (a, b) => (ruleOrder.indexOf(a[0]) === -1 ? 99 : ruleOrder.indexOf(a[0])) -
                  (ruleOrder.indexOf(b[0]) === -1 ? 99 : ruleOrder.indexOf(b[0])),
      )
    : entries;

  return (
    <aside className="fix-panel">
      <div className="fix-panel-header">
        <Wrench size={16} />
        Probable Fixes
      </div>
      <div className="fix-panel-list">
        {sorted.map(([ruleId, fix]) => (
          <div key={ruleId} className="fix-panel-row">
            <Badge rule={ruleId}>{ruleId}</Badge>
            <div>
              <strong className="text-title">{RULE_META[ruleId].label}</strong>
              <p>{fix}</p>
            </div>
          </div>
        ))}
      </div>
    </aside>
  );
}
