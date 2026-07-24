import { Fragment } from "react";
import { Badge } from "@/components/ui/badge";
import { isRuleId } from "@/lib/rule-meta";

export function highlightRules(text: string) {
  return text.split(/\b(R1|R2|R3|R6|R7)\b/g).map((part, index) => {
    if (isRuleId(part)) {
      return <Badge key={`${part}-${index}`} rule={part}>{part}</Badge>;
    }
    return <Fragment key={`${part}-${index}`}>{part}</Fragment>;
  });
}
