"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Play } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { AUDIT_WINDOWS, firedRuleIds, GuardianApiError, runAudit, serviceLabel } from "@/lib/api";
import { useGuardianStore } from "@/lib/store";

export function AuditRunButton({ service }: { service?: string | null }) {
  const selectedService = useGuardianStore((state) => state.selectedService);
  const auditWindow = useGuardianStore((state) => state.auditWindow);
  const setAuditWindow = useGuardianStore((state) => state.setAuditWindow);
  const addAuditHistory = useGuardianStore((state) => state.addAuditHistory);
  const queryClient = useQueryClient();
  const toast = useToast();
  const auditService = service ?? selectedService;

  const mutation = useMutation({
    mutationFn: () => runAudit(auditService, auditWindow),
    onSuccess: (cycle) => {
      addAuditHistory({
        timestamp: Date.now(),
        service: cycle.service,
        fired_rule_ids: firedRuleIds(cycle),
        score: cycle.health_score.score,
      });
      queryClient.invalidateQueries({ queryKey: ["audit"] });
      toast.push({ title: "Audit complete", detail: `${serviceLabel(cycle.service)} scored ${Math.round(cycle.health_score.score)}` });
    },
    onError: (error) => {
      const detail =
        error instanceof GuardianApiError && error.status === 502
          ? `SigNoz MCP failure: ${error.message}`
          : error instanceof Error
            ? error.message
            : "Audit run failed.";
      toast.push({ title: "Audit failed", detail, tone: "error" });
    },
  });

  return (
    <div className="audit-controls">
      <label className="field-label">
        Window
        <select className="select" value={auditWindow} onChange={(e) => setAuditWindow(e.target.value)}>
          {AUDIT_WINDOWS.map((w) => (
            <option key={w.value} value={w.value}>
              {w.label}
            </option>
          ))}
        </select>
      </label>
      <Button className={mutation.isPending ? "scan" : ""} disabled={mutation.isPending} onClick={() => mutation.mutate()} variant="primary">
        <Play size={16} />
        {mutation.isPending ? "Auditing..." : "Run audit"}
      </Button>
    </div>
  );
}
