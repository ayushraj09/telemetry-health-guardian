"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Bell,
  BellRing,
  ChevronDown,
  FileText,
  Loader2,
  Play,
  Upload,
  X,
  Zap,
} from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useToast } from "@/components/ui/toast";
import { RULES, type RuleId } from "@/lib/rule-meta";

const DEMO_API_BASE =
  process.env.NEXT_PUBLIC_DEMO_AGENT_API_URL ?? "http://localhost:8001";
const GUARDIAN_API_BASE =
  process.env.NEXT_PUBLIC_GUARDIAN_API_URL ?? "http://localhost:8000";

const DEFAULT_PDF_NAME = "fixtures/long_climate_report.pdf";
const DEFAULT_QUESTION = "What does the report say about climate risk?";

const AUDIT_WINDOW_OPTIONS = [
  { value: "5m", label: "5 minutes" },
  { value: "15m", label: "15 minutes" },
  { value: "30m", label: "30 minutes" },
  { value: "1h", label: "1 hour" },
];

type RuleConfig = {
  enabled: boolean;
  rate: number;
  // R6-specific
  ollamaModel?: string;
  numCtx?: number;
};

type AlertEvent = {
  id: string;
  status: "firing" | "resolved";
  alert_name: string;
  severity: string;
  summary: string;
  description: string;
  rule_id: string;
  service: string;
  starts_at: string;
  ends_at: string;
  received_at: number;
  labels: Record<string, string>;
};

function defaultRuleConfigs(): Record<RuleId, RuleConfig> {
  return {
    R1: { enabled: true, rate: 0.3 },
    R2: { enabled: true, rate: 1.0 },
    R3: { enabled: true, rate: 0.3 },
    R6: { enabled: true, rate: 1.0, ollamaModel: "llama3.2", numCtx: 2048 },
    R7: { enabled: true, rate: 0.5 },
  };
}

function timeAgo(ts: number): string {
  const seconds = Math.floor(Date.now() / 1000 - ts);
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

export default function DemoAgentPage() {
  // --- PDF state ---
  const [uploadedFile, setUploadedFile] = useState<File | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const activePdf = uploadedFile ? uploadedFile.name : DEFAULT_PDF_NAME;

  // --- Question ---
  const [question, setQuestion] = useState(DEFAULT_QUESTION);

  // --- Audit window ---
  const [auditWindow, setAuditWindow] = useState("5m");

  // --- Chaos ---
  const [chaosMode, setChaosMode] = useState(true);

  // --- Rule configs ---
  const [ruleConfigs, setRuleConfigs] = useState<Record<RuleId, RuleConfig>>(
    defaultRuleConfigs
  );

  // --- Execution state ---
  const [isRunning, setIsRunning] = useState(false);
  const [agentResult, setAgentResult] = useState<Record<string, unknown> | null>(null);
  const [auditResult, setAuditResult] = useState<Record<string, unknown> | null>(null);
  const [executionError, setExecutionError] = useState<string | null>(null);

  // --- Notifications ---
  const [alerts, setAlerts] = useState<AlertEvent[]>([]);
  const [notifOpen, setNotifOpen] = useState(false);
  const [sseConnected, setSseConnected] = useState(false);
  const [hasNewAlert, setHasNewAlert] = useState(false);
  const [notifPermission, setNotifPermission] = useState<NotificationPermission>("default");
  const eventSourceRef = useRef<EventSource | null>(null);

  const toast = useToast();

  // --- Request browser notification permission ---
  useEffect(() => {
    if (typeof Notification !== "undefined") {
      setNotifPermission(Notification.permission);
    }
  }, []);

  function requestNotifPermission() {
    if (typeof Notification !== "undefined" && Notification.permission === "default") {
      Notification.requestPermission().then((perm) => {
        setNotifPermission(perm);
      });
    }
  }

  // --- SSE for alerts ---
  useEffect(() => {
    // Fetch existing alerts
    fetch(`${GUARDIAN_API_BASE}/alerts`)
      .then((res) => res.json())
      .then((data: AlertEvent[]) => {
        if (Array.isArray(data)) setAlerts(data.slice(0, 50));
      })
      .catch(() => {});

    const es = new EventSource(`${GUARDIAN_API_BASE}/alerts/stream`);
    eventSourceRef.current = es;

    es.addEventListener("connected", () => setSseConnected(true));

    es.addEventListener("alert", (event) => {
      try {
        const alert: AlertEvent = JSON.parse(event.data);
        setAlerts((prev) => [alert, ...prev].slice(0, 50));
        setHasNewAlert(true);

        toast.push({
          title: `${alert.status === "resolved" ? "\u2713 Resolved" : "\u26A0 Alert"}: ${alert.alert_name}`,
          detail: alert.summary || alert.description,
          tone: alert.status === "resolved" ? "ok" : "error",
        });

        // Browser notification
        if (typeof Notification !== "undefined" && Notification.permission === "granted") {
          new Notification(
            `${alert.status === "resolved" ? "Resolved" : "Alert"}: ${alert.alert_name}`,
            {
              body: `[${alert.rule_id?.toUpperCase()}] ${alert.severity} — ${alert.summary || alert.description}`,
              tag: alert.id,
            }
          );
        }
      } catch {}
    });

    es.addEventListener("ping", () => {});
    es.onerror = () => setSseConnected(false);

    return () => {
      es.close();
      eventSourceRef.current = null;
    };
  }, [toast]);

  // --- Rule config helpers ---
  function toggleRule(ruleId: RuleId) {
    setRuleConfigs((prev) => ({
      ...prev,
      [ruleId]: { ...prev[ruleId], enabled: !prev[ruleId].enabled },
    }));
  }

  function updateRuleRate(ruleId: RuleId, rate: number) {
    setRuleConfigs((prev) => ({
      ...prev,
      [ruleId]: { ...prev[ruleId], rate },
    }));
  }

  function updateR6Model(model: string) {
    setRuleConfigs((prev) => ({
      ...prev,
      R6: { ...prev.R6, ollamaModel: model },
    }));
  }

  function updateR6NumCtx(numCtx: number) {
    setRuleConfigs((prev) => ({
      ...prev,
      R6: { ...prev.R6, numCtx },
    }));
  }

  // --- Run Live Agent ---
  async function handleRunAgent() {
    setIsRunning(true);
    setAgentResult(null);
    setAuditResult(null);
    setExecutionError(null);

    try {
      // Build chaos env config
      const enabledRules = (Object.entries(ruleConfigs) as [RuleId, RuleConfig][]).filter(
        ([, cfg]) => cfg.enabled
      );

      let response: Response;
      if (uploadedFile) {
        const formData = new FormData();
        formData.append("question", question);
        formData.append("chaos_mode", String(chaosMode && enabledRules.length > 0));
        formData.append("uploaded_pdf", uploadedFile);
        if (chaosMode) {
          formData.append("rule_configs", JSON.stringify(
            Object.fromEntries(
              enabledRules.map(([id, cfg]) => [id, cfg])
            )
          ));
        }
        formData.append("audit_window", auditWindow);
        response = await fetch(`${DEMO_API_BASE}/run/upload`, {
          method: "POST",
          body: formData,
        });
      } else {
        response = await fetch(`${DEMO_API_BASE}/run`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            question,
            chaos_mode: chaosMode && enabledRules.length > 0,
            rule_configs: chaosMode
              ? Object.fromEntries(enabledRules.map(([id, cfg]) => [id, cfg]))
              : undefined,
            audit_window: auditWindow,
          }),
        });
      }

      if (!response.ok) {
        const body = await response.json().catch(() => ({ detail: response.statusText }));
        throw new Error(body.detail ?? "Agent run failed");
      }

      const data = await response.json();
      setAgentResult(data);
      toast.push({ title: "Agent run complete", detail: "Results displayed below." });

      // Now trigger an audit with the selected window
      try {
        const auditRes = await fetch(`${GUARDIAN_API_BASE}/audit/run`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ service: null, time_range: auditWindow }),
        });
        if (auditRes.ok) {
          const auditData = await auditRes.json();
          setAuditResult(auditData);
        }
      } catch {
        // Audit is best-effort
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Unknown error";
      setExecutionError(msg);
      toast.push({ title: "Agent run failed", detail: msg, tone: "error" });
    } finally {
      setIsRunning(false);
    }
  }

  const firingCount = alerts.filter((a) => a.status === "firing").length;

  return (
    <div className="content-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">Demo Agent</p>
          <h1>Run the AI agent pipeline end-to-end.</h1>
        </div>
      </header>

      {/* Controls Grid */}
      <div className="demo-agent-grid">
        {/* Left column: inputs */}
        <div className="demo-agent-controls">
          {/* PDF Selector */}
          <Card>
            <CardHeader>
              <CardTitle>PDF Source</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="pdf-selector-row">
                <button
                  className="input-like"
                  type="button"
                  onClick={() => fileInputRef.current?.click()}
                >
                  <FileText size={16} /> {activePdf}
                </button>
                {uploadedFile && (
                  <button
                    className="btn-icon-sm"
                    type="button"
                    title="Remove uploaded PDF"
                    onClick={() => setUploadedFile(null)}
                  >
                    <X size={14} />
                  </button>
                )}
              </div>
              <input
                ref={fileInputRef}
                type="file"
                accept=".pdf"
                className="sr-only"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) setUploadedFile(file);
                  e.target.value = "";
                }}
              />
              <p className="text-detail" style={{ marginTop: 8 }}>
                Click to upload a custom PDF, or use the default fixture.
              </p>
            </CardContent>
          </Card>

          {/* Question */}
          <Card>
            <CardHeader>
              <CardTitle>Question</CardTitle>
            </CardHeader>
            <CardContent>
              <input
                className="input"
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                placeholder="Ask the agent a question..."
              />
            </CardContent>
          </Card>

          {/* Audit Window */}
          <Card>
            <CardHeader>
              <CardTitle>Audit Window</CardTitle>
            </CardHeader>
            <CardContent>
              <select
                className="select"
                value={auditWindow}
                onChange={(e) => setAuditWindow(e.target.value)}
              >
                {AUDIT_WINDOW_OPTIONS.map((opt) => (
                  <option key={opt.value} value={opt.value}>
                    {opt.label}
                  </option>
                ))}
              </select>
              <p className="text-detail" style={{ marginTop: 8 }}>
                Time window for the Guardian audit after the agent run.
              </p>
            </CardContent>
          </Card>

          {/* Chaos Toggle */}
          <Card>
            <CardHeader>
              <CardTitle>Chaos Mode</CardTitle>
            </CardHeader>
            <CardContent>
              <label className="toggle-row">
                <input
                  type="checkbox"
                  checked={chaosMode}
                  onChange={(e) => setChaosMode(e.target.checked)}
                />
                <span>
                  {chaosMode ? "Enabled" : "Disabled"} — inject telemetry faults
                </span>
              </label>
            </CardContent>
          </Card>

          {/* Rule Configuration */}
          <Card>
            <CardHeader>
              <CardTitle>Rule Configuration</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="rule-config-list">
                {RULES.map((rule) => {
                  const cfg = ruleConfigs[rule.id];
                  return (
                    <div key={rule.id} className="rule-config-item">
                      <div className="rule-config-header">
                        <label className="toggle-row">
                          <input
                            type="checkbox"
                            checked={cfg.enabled}
                            onChange={() => toggleRule(rule.id)}
                            disabled={!chaosMode}
                          />
                          <span
                            className="rule-badge"
                            style={{ background: rule.color }}
                          >
                            {rule.id}
                          </span>
                          <span className="rule-config-label">{rule.label}</span>
                        </label>
                      </div>
                      {cfg.enabled && chaosMode && (
                        <div className="rule-config-params">
                          <label className="param-field">
                            <span className="param-label">Rate</span>
                            <input
                              type="number"
                              className="input narrow"
                              min={0}
                              max={1}
                              step={0.1}
                              value={cfg.rate}
                              onChange={(e) =>
                                updateRuleRate(rule.id, parseFloat(e.target.value) || 0)
                              }
                            />
                          </label>
                          {rule.id === "R6" && (
                            <>
                              <label className="param-field">
                                <span className="param-label">Ollama Model</span>
                                <input
                                  type="text"
                                  className="input narrow"
                                  value={cfg.ollamaModel ?? "llama3.2"}
                                  onChange={(e) => updateR6Model(e.target.value)}
                                />
                              </label>
                              <label className="param-field">
                                <span className="param-label">num_ctx</span>
                                <input
                                  type="number"
                                  className="input narrow"
                                  min={256}
                                  step={256}
                                  value={cfg.numCtx ?? 2048}
                                  onChange={(e) =>
                                    updateR6NumCtx(parseInt(e.target.value) || 2048)
                                  }
                                />
                              </label>
                            </>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </CardContent>
          </Card>

          {/* Run Button */}
          <Button
            disabled={isRunning || !question.trim()}
            onClick={handleRunAgent}
            variant="primary"
            className="run-agent-btn"
          >
            {isRunning ? (
              <>
                <Loader2 size={16} className="spin" /> Running...
              </>
            ) : (
              <>
                <Play size={16} /> Run Live Agent
              </>
            )}
          </Button>
        </div>

        {/* Right column: results + notifications */}
        <div className="demo-agent-results">
          {/* Execution Status */}
          {isRunning && (
            <Card>
              <CardContent>
                <div className="execution-status">
                  <Loader2 size={20} className="spin" />
                  <span>Agent is running... This may take up to 3 minutes.</span>
                </div>
              </CardContent>
            </Card>
          )}

          {executionError && (
            <div className="callout error">
              <span>{executionError}</span>
            </div>
          )}

          {/* Agent Response */}
          {agentResult && (
            <Card>
              <CardHeader>
                <CardTitle>Agent Response</CardTitle>
              </CardHeader>
              <CardContent>
                <pre className="mono result-pre">
                  {JSON.stringify(agentResult, null, 2)}
                </pre>
              </CardContent>
            </Card>
          )}

          {/* Guardian Findings */}
          {auditResult && (
            <Card>
              <CardHeader>
                <CardTitle>Guardian Findings</CardTitle>
              </CardHeader>
              <CardContent>
                <pre className="mono result-pre">
                  {JSON.stringify(auditResult, null, 2)}
                </pre>
              </CardContent>
            </Card>
          )}

          {/* Webhook Notifications */}
          <Card>
            <CardHeader>
              <CardTitle>
                <span className="notif-header-row">
                  {firingCount > 0 ? (
                    <BellRing size={18} />
                  ) : (
                    <Bell size={18} />
                  )}
                  Live Alerts
                  {firingCount > 0 && (
                    <span className="alert-count-inline">{firingCount}</span>
                  )}
                  <span
                    className={`sse-dot ${sseConnected ? "connected" : "disconnected"}`}
                  />
                </span>
              </CardTitle>
              {notifPermission !== "granted" && (
                <button
                  type="button"
                  className="button button-secondary"
                  onClick={requestNotifPermission}
                  style={{ fontSize: "0.75rem" }}
                >
                  Enable browser notifications
                </button>
              )}
            </CardHeader>
            <CardContent>
              <div className="alert-history-list">
                {alerts.length === 0 ? (
                  <div className="empty-state">
                    No alerts yet. Alerts will appear here when rules fire during
                    or after an agent run.
                  </div>
                ) : (
                  alerts.map((alert) => (
                    <div
                      key={alert.id}
                      className={`alert-history-item ${alert.status}`}
                    >
                      <div className="alert-history-header">
                        <span className={`alert-status-dot ${alert.status}`} />
                        <span className="alert-history-name">
                          {alert.alert_name}
                        </span>
                        {alert.rule_id && (
                          <span
                            className="rule-badge"
                            style={{
                              background: RULES.find(
                                (r) =>
                                  r.id === alert.rule_id?.toUpperCase()
                              )?.color ?? "var(--text-muted)",
                            }}
                          >
                            {alert.rule_id.toUpperCase()}
                          </span>
                        )}
                        <span className="alert-severity-tag">
                          {alert.severity}
                        </span>
                        <span className="text-detail">
                          {timeAgo(alert.received_at)}
                        </span>
                      </div>
                      {(alert.summary || alert.description) && (
                        <p className="text-detail" style={{ marginTop: 4 }}>
                          {alert.summary || alert.description}
                        </p>
                      )}
                    </div>
                  ))
                )}
              </div>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}
