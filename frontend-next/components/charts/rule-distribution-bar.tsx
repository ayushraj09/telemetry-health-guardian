import { RULES, type RuleId } from "@/lib/rule-meta";

export function RuleDistributionBar({ counts }: { counts: Record<RuleId, number> }) {
  const total = Object.values(counts).reduce((sum, count) => sum + count, 0);
  if (!total) {
    return <div className="distribution empty" />;
  }
  return (
    <div className="distribution" aria-label={`${total} findings`}>
      {RULES.map((rule) => {
        const count = counts[rule.id] ?? 0;
        if (!count) {
          return null;
        }
        return <span key={rule.id} style={{ width: `${(count / total) * 100}%`, background: rule.color }} />;
      })}
    </div>
  );
}
