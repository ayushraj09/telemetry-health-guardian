"use client";

import { motion } from "motion/react";
import { RULES, type RuleId } from "@/lib/rule-meta";

export function RuleDistributionBar({ counts }: { counts: Record<RuleId, number> }) {
  const total = Object.values(counts).reduce((sum, count) => sum + count, 0);
  if (!total) {
    return <div className="distribution empty" />;
  }
  return (
    <div className="distribution" aria-label={`${total} findings`}>
      {RULES.map((rule, index) => {
        const count = counts[rule.id] ?? 0;
        if (!count) {
          return null;
        }
        return (
          <motion.span
            key={rule.id}
            style={{ width: `${(count / total) * 100}%`, background: rule.color }}
            initial={{ scaleX: 0 }}
            animate={{ scaleX: 1 }}
            transition={{ delay: index * 0.05, duration: 0.4, ease: "easeOut" }}
          />
        );
      })}
    </div>
  );
}
