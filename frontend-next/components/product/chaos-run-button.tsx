"use client";

import { Zap } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { useGuardianStore } from "@/lib/store";

export function ChaosRunButton({ mode }: { mode: "baseline" | "chaos" }) {
  const addAuditHistory = useGuardianStore((state) => state.addAuditHistory);
  const toast = useToast();

  return (
    <Button
      onClick={() => {
        addAuditHistory({
          timestamp: Date.now(),
          service: null,
          fired_rule_ids: mode === "chaos" ? ["R1", "R2", "R3", "R6"] : [],
          score: mode === "chaos" ? 46 : 94,
        });
        toast.push({
          title: mode === "chaos" ? "Chaos replay added" : "Baseline replay added",
          detail: "This records the pre-seeded demo moment in session history.",
        });
      }}
      variant={mode === "chaos" ? "danger" : "secondary"}
    >
      <Zap size={16} />
      {mode === "chaos" ? "Run with chaos" : "Run baseline"}
    </Button>
  );
}
