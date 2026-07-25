"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Zap } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { firedRuleIds, runAudit, serviceLabel } from "@/lib/api";
import { runDemoAgent } from "@/lib/demo-agent-client";
import { useGuardianStore } from "@/lib/store";

const DEMO_SERVICE = "demo-agent-app";
const DEFAULT_QUESTION = "What does the Kestrel Basin report say about sea level rise and drought?";

export function ChaosRunButton({ mode }: { mode: "baseline" | "chaos" }) {
  const addAuditHistory = useGuardianStore((state) => state.addAuditHistory);
  const queryClient = useQueryClient();
  const toast = useToast();

  const mutation = useMutation({
    mutationFn: async () => {
      await runDemoAgent({ question: DEFAULT_QUESTION, chaos_mode: mode === "chaos" });
      return runAudit(DEMO_SERVICE, "5m");
    },
    onSuccess: (cycle) => {
      const fired = firedRuleIds(cycle);
      addAuditHistory({
        timestamp: Date.now(),
        service: cycle.service,
        fired_rule_ids: fired,
        score: cycle.health_score.score,
      });
      queryClient.invalidateQueries({ queryKey: ["audit"] });
      toast.push({
        title: mode === "chaos" ? "Chaos run complete" : "Baseline run complete",
        detail: `${serviceLabel(cycle.service)} scored ${Math.round(cycle.health_score.score)}${
          fired.length ? ` — fired ${fired.join(", ")}` : " — clean"
        }`,
      });
    },
    onError: (error) => {
      toast.push({
        title: mode === "chaos" ? "Chaos run failed" : "Baseline run failed",
        detail: error instanceof Error ? error.message : "Could not reach the demo agent or Guardian API.",
        tone: "error",
      });
    },
  });

  return (
    <Button
      className={mutation.isPending ? "scan" : ""}
      disabled={mutation.isPending}
      onClick={() => mutation.mutate()}
      variant={mode === "chaos" ? "danger" : "secondary"}
    >
      <Zap size={16} />
      {mutation.isPending ? "Running..." : mode === "chaos" ? "Run with chaos" : "Run baseline"}
    </Button>
  );
}
