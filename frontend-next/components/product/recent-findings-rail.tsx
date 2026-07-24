"use client";

import { Badge } from "@/components/ui/badge";
import { serviceLabel } from "@/lib/api";
import { useGuardianStore } from "@/lib/store";

export function RecentFindingsRail() {
  const history = useGuardianStore((state) => state.auditHistory);
  const setDraft = useGuardianStore((state) => state.setChatDraft);
  const recent = history.slice(-12).reverse();

  if (!recent.length) {
    return <div className="empty-rail panel">Run an audit to populate recent findings.</div>;
  }

  return (
    <div className="recent-rail">
      {recent.map((entry) => {
        const time = new Date(entry.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        return (
          <button
            className="recent-chip"
            key={entry.timestamp}
            onClick={() => setDraft(`Why did ${entry.fired_rule_ids.join(", ") || "no rules"} fire at ${time}?`)}
            type="button"
          >
            <span>{serviceLabel(entry.service)}</span>
            <strong className="mono">{Math.round(entry.score)}</strong>
            <span className="chip-rules">
              {entry.fired_rule_ids.length ? entry.fired_rule_ids.map((ruleId) => <Badge key={ruleId} rule={ruleId}>{ruleId}</Badge>) : <span className="clean-chip">clean</span>}
            </span>
            <span className="muted mono">{time}</span>
          </button>
        );
      })}
    </div>
  );
}
