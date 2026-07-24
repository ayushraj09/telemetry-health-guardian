"use client";

import { useQuery } from "@tanstack/react-query";
import { checkHealth } from "@/lib/api";

export function ApiStatusPill() {
  const health = useQuery({ queryKey: ["health"], queryFn: checkHealth, refetchInterval: 10_000, retry: 0 });
  const ok = health.data?.status === "ok";
  return <span className={ok ? "api-pill ok" : "api-pill down"}>{ok ? "API online" : "API unavailable"}</span>;
}
