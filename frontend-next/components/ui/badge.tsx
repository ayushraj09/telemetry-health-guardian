import { HTMLAttributes } from "react";
import { clsx } from "clsx";
import { RULE_META, type RuleId } from "@/lib/rule-meta";

type BadgeProps = HTMLAttributes<HTMLSpanElement> & {
  rule?: RuleId;
};

export function Badge({ className, rule, style, ...props }: BadgeProps) {
  const color = rule ? RULE_META[rule].color : "var(--accent-guardian)";
  return (
    <span
      className={clsx("badge mono", className)}
      style={{ "--badge-color": color, ...style } as React.CSSProperties}
      {...props}
    />
  );
}
