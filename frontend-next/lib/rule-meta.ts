export type RuleId = "R1" | "R2" | "R3" | "R6" | "R7";

export type RuleMeta = {
  id: RuleId;
  label: string;
  description: string;
  color: string;
  docsAnchor: string;
};

export const RULES: RuleMeta[] = [
  {
    id: "R1",
    label: "Missing semantic fields",
    description: "GenAI spans are missing required OpenTelemetry semantic attributes.",
    color: "var(--rule-r1)",
    docsAnchor: "r1-missing-fields",
  },
  {
    id: "R2",
    label: "Cardinality risk",
    description: "Span attributes contain values likely to explode metric/cardinality cost.",
    color: "var(--rule-r2)",
    docsAnchor: "r2-cardinality-risk",
  },
  {
    id: "R3",
    label: "Orphaned spans",
    description: "Child spans reference missing parents, breaking trace explainability.",
    color: "var(--rule-r3)",
    docsAnchor: "r3-orphaned-spans",
  },
  {
    id: "R6",
    label: "Silent truncation",
    description: "Tool or model payloads shrink before reaching the agent context.",
    color: "var(--rule-r6)",
    docsAnchor: "r6-silent-truncation",
  },
  {
    id: "R7",
    label: "Cross-service breaks",
    description: "Trace context fails across service boundaries.",
    color: "var(--rule-r7)",
    docsAnchor: "r7-cross-service-breaks",
  },
];

export const RULE_META = Object.fromEntries(RULES.map((rule) => [rule.id, rule])) as Record<RuleId, RuleMeta>;

export function isRuleId(value: string): value is RuleId {
  return value in RULE_META;
}
