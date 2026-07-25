import type { RuleId } from "./rule-meta";

const API_BASE = process.env.NEXT_PUBLIC_GUARDIAN_API_URL ?? "http://localhost:8000";
export const PRIMARY_SERVICE = "demo-agent-app";
export const DEFAULT_GUARDIAN_QUESTION = "What rules fired and what should I fix first?";

export const AUDIT_SERVICES = [PRIMARY_SERVICE];

export type RuleResult = {
  findings: Record<string, unknown>[];
  [key: string]: unknown;
};

export type AuditCycle = {
  service: string | null;
  findings: {
    service: string | null;
    r1: RuleResult;
    r2: RuleResult;
    r3: RuleResult;
    r6: RuleResult;
    r7?: RuleResult;
  };
  health_score: {
    score: number;
    raw_score: number;
    terms: Record<string, number>;
    r7_included: boolean;
  };
  narrative: string | null;
  narrative_error: string | null;
};

export type CitedSegment = {
  rule_id: string;
  claim: string;
  evidence: string[];
  severity_rank: number;
  detail: string;
};

export type ChatResponse = {
  service: string | null;
  question: string;
  answer: string;
  segments: CitedSegment[];
  rules_fired_but_uncited: RuleId[];
  probable_fixes: Partial<Record<RuleId, string>>;
};

export const AUDIT_WINDOWS = [
  { value: "5m", label: "5 min" },
  { value: "15m", label: "15 min" },
  { value: "30m", label: "30 min" },
  { value: "1h", label: "1 hour" },
  { value: "6h", label: "6 hours" },
  { value: "24h", label: "24 hours" },
];

export class GuardianApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "GuardianApiError";
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...init?.headers,
    },
    cache: "no-store",
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: string };
      detail = body.detail ?? detail;
    } catch {
      // Keep the HTTP status text if the backend returned non-JSON.
    }
    throw new GuardianApiError(response.status, detail);
  }
  return response.json() as Promise<T>;
}

export function checkHealth(): Promise<{ status: string }> {
  return request("/health");
}

export function getAuditReport(service: string): Promise<AuditCycle> {
  return request(`/audit/report/${encodeURIComponent(normalizeService(service))}`);
}

export function runAudit(service?: string | null, timeRange?: string | null): Promise<AuditCycle> {
  return request("/audit/run", {
    method: "POST",
    body: JSON.stringify({
      service: normalizeService(service),
      time_range: timeRange ?? null,
    }),
  });
}

export function askGuardian(question: string, service?: string | null, timeRange?: string | null): Promise<ChatResponse> {
  return request("/chat", {
    method: "POST",
    body: JSON.stringify({
      question,
      service: normalizeService(service),
      time_range: timeRange ?? null,
    }),
  });
}

export function serviceLabel(service: string | null | undefined): string {
  return normalizeService(service);
}

export function normalizeService(service: string | null | undefined): string {
  if (!service || service === "_all_" || service === "all") {
    return PRIMARY_SERVICE;
  }
  return service;
}

export type RuleResultKey = "r1" | "r2" | "r3" | "r6" | "r7";

export function ruleResultKey(ruleId: RuleId): RuleResultKey {
  return ruleId.toLowerCase() as RuleResultKey;
}

export function firedRuleIds(cycle: AuditCycle | undefined): RuleId[] {
  if (!cycle) {
    return [];
  }
  return (["R1", "R2", "R3", "R6", "R7"] as RuleId[]).filter((ruleId) => {
    const result = cycle.findings[ruleResultKey(ruleId)];
    return Array.isArray(result?.findings) && result.findings.length > 0;
  });
}

export function ruleFindingCount(cycle: AuditCycle | undefined, ruleId: RuleId): number {
  const result = cycle?.findings[ruleResultKey(ruleId)];
  return Array.isArray(result?.findings) ? result.findings.length : 0;
}
