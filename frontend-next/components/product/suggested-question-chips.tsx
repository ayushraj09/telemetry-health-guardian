"use client";

import { Badge } from "@/components/ui/badge";
import { firedRuleIds, type AuditCycle } from "@/lib/api";
import { useGuardianStore } from "@/lib/store";

export function SuggestedQuestionChips({ cycle }: { cycle?: AuditCycle }) {
  const setDraft = useGuardianStore((state) => state.setChatDraft);
  const rules = firedRuleIds(cycle);
  const suggestions = rules.length
    ? rules.map((ruleId) => ({ ruleId, text: `What caused ${ruleId} and what should I fix first?` }))
    : [{ ruleId: undefined, text: "Is the current telemetry trustworthy?" }];

  return (
    <div className="suggested-chips">
      {suggestions.map((suggestion) => (
        <button key={suggestion.text} onClick={() => setDraft(suggestion.text)} type="button">
          {suggestion.ruleId ? <Badge rule={suggestion.ruleId}>{suggestion.ruleId}</Badge> : null}
          {suggestion.text}
        </button>
      ))}
    </div>
  );
}
