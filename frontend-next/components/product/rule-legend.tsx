import { Badge } from "@/components/ui/badge";
import { RULES } from "@/lib/rule-meta";

export function RuleLegend({ compact = false }: { compact?: boolean }) {
  return (
    <div className={compact ? "rule-legend compact" : "rule-legend"}>
      {RULES.map((rule) => (
        <div className="rule-legend-row" key={rule.id}>
          <Badge rule={rule.id}>{rule.id}</Badge>
          <div>
            <strong>{rule.label}</strong>
            <p className="muted">{rule.description}</p>
          </div>
        </div>
      ))}
    </div>
  );
}
