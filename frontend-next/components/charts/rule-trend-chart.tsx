import type { RuleId } from "@/lib/rule-meta";
import type { AuditHistoryEntry } from "@/lib/store";

export function RuleTrendChart({ history, ruleId }: { history: AuditHistoryEntry[]; ruleId: RuleId }) {
  const recent = history.slice(-16);
  return (
    <div className="rule-trend">
      {recent.map((entry) => (
        <span className={entry.fired_rule_ids.includes(ruleId) ? "hot" : ""} key={`${entry.timestamp}-${ruleId}`} />
      ))}
    </div>
  );
}
