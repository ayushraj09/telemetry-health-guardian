"use client";

import { Wrench } from "lucide-react";
import { Accordion } from "@/components/ui/accordion";
import { Badge } from "@/components/ui/badge";
import type { ChatResponse } from "@/lib/api";
import { isRuleId, type RuleId } from "@/lib/rule-meta";

export function ProbableFixPanel({ fixes }: { fixes: ChatResponse["probable_fixes"] }) {
  const entries = Object.entries(fixes).flatMap(([ruleId, fix]): [RuleId, string][] =>
    isRuleId(ruleId) && fix ? [[ruleId, fix]] : [],
  );
  if (!entries.length) {
    return null;
  }

  return (
    <Accordion title={<span className="fix-title"><Wrench size={15} /> Probable fixes</span>}>
      <div className="fix-list">
        {entries.map(([ruleId, fix]) => (
          <div key={ruleId}>
            <Badge rule={ruleId}>{ruleId}</Badge>
            <p>{fix}</p>
          </div>
        ))}
      </div>
    </Accordion>
  );
}
