"use client";

import { useEffect, useRef, useState } from "react";
import {
  FileText,
  Loader2,
  Play,
  X,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { AutosizingTextarea } from "@/components/ui/autosizing-textarea";
import { useToast } from "@/components/ui/toast";
import { listOllamaModels } from "@/lib/demo-agent-client";
import { RULES, type RuleId } from "@/lib/rule-meta";

const DEMO_API_BASE =
  process.env.NEXT_PUBLIC_DEMO_AGENT_API_URL ?? "http://localhost:8001";

const DEFAULT_PDF_NAME = "fixtures/long_climate_report.pdf";
const DEFAULT_QUESTION = "What does the Kestrel Basin report say about sea level rise and drought?";

type RuleConfig = {
  enabled: boolean;
  rate: number;
  // R6-specific
  ollamaModel?: string;
};

function defaultRuleConfigs(): Record<RuleId, RuleConfig> {
  return {
    R1: { enabled: true, rate: 0.3 },
    R2: { enabled: true, rate: 1.0 },
    R3: { enabled: true, rate: 0.3 },
    R6: { enabled: true, rate: 1.0, ollamaModel: "llama3.2" },
    R7: { enabled: true, rate: 0.5 },
  };
}

function getAgentReport(result: Record<string, unknown>): string {
  return typeof result.report === "string" ? result.report : JSON.stringify(result, null, 2);
}

let retainedAgentResponse:
  | {
      question: string;
      result: Record<string, unknown>;
      view: "rendered" | "raw";
    }
  | null = null;

export default function DemoAgentPage() {
  // --- PDF state ---
  const [uploadedFile, setUploadedFile] = useState<File | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const activePdf = uploadedFile ? uploadedFile.name : DEFAULT_PDF_NAME;

  // --- Question ---
  const [question, setQuestion] = useState(retainedAgentResponse?.question ?? DEFAULT_QUESTION);

  // --- Chaos ---
  const [chaosMode, setChaosMode] = useState(true);

  // --- Ollama discovery ---
  const [ollamaModels, setOllamaModels] = useState<string[]>([]);
  const [ollamaLoading, setOllamaLoading] = useState(true);

  // --- Rule configs ---
  const [ruleConfigs, setRuleConfigs] = useState<Record<RuleId, RuleConfig>>(
    defaultRuleConfigs
  );

  // --- Execution state ---
  const [isRunning, setIsRunning] = useState(false);
  const [agentResult, setAgentResult] = useState<Record<string, unknown> | null>(
    retainedAgentResponse?.result ?? null
  );
  const [executionError, setExecutionError] = useState<string | null>(null);
  const [agentResponseView, setAgentResponseViewState] = useState<"rendered" | "raw">(
    retainedAgentResponse?.view ?? "rendered"
  );

  const toast = useToast();

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

  function setAgentResponseView(view: "rendered" | "raw") {
    setAgentResponseViewState(view);
    if (retainedAgentResponse) {
      retainedAgentResponse = { ...retainedAgentResponse, view };
    }
  }

  function handleQuestionChange(value: string) {
    setQuestion(value);
    if (retainedAgentResponse && value !== retainedAgentResponse.question) {
      retainedAgentResponse = null;
      setAgentResult(null);
      setExecutionError(null);
      setAgentResponseViewState("rendered");
    }
  }

  useEffect(() => {
    let cancelled = false;

    async function refreshOllamaModels() {
      setOllamaLoading(true);
      try {
        const models = await listOllamaModels();
        if (cancelled) {
          return;
        }
        setOllamaModels(models);
      } finally {
        if (!cancelled) {
          setOllamaLoading(false);
        }
      }
    }

    void refreshOllamaModels();

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!ollamaModels.length) {
      return;
    }

    const currentModel = ruleConfigs.R6.ollamaModel ?? "";
    if (!currentModel || !ollamaModels.includes(currentModel)) {
      updateR6Model(ollamaModels[0]);
    }
  }, [ollamaModels, ruleConfigs.R6.ollamaModel]);

  // --- Run Live Agent ---
  async function handleRunAgent() {
    setIsRunning(true);
    setAgentResult(null);
    setExecutionError(null);
    setAgentResponseViewState("rendered");
    retainedAgentResponse = null;

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
          }),
        });
      }

      if (!response.ok) {
        const body = await response.json().catch(() => ({ detail: response.statusText }));
        throw new Error(body.detail ?? "Agent run failed");
      }

      const data = await response.json();
      setAgentResult(data);
      retainedAgentResponse = { question, result: data, view: "rendered" };
      toast.push({ title: "Agent run complete", detail: "Results displayed below." });
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Unknown error";
      setExecutionError(msg);
      toast.push({ title: "Agent run failed", detail: msg, tone: "error" });
    } finally {
      setIsRunning(false);
    }
  }

  return (
    <div className="content-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">Demo Agent</p>
          <h1>Run the AI agent.</h1>
        </div>
      </header>

      <div className="demo-agent-layout">
        <Card className="demo-agent-workspace">
          <div className="demo-agent-workspace-grid">
            <section className="demo-agent-inputs" aria-label="Agent inputs">
              <div className="demo-agent-section">
                <h2 className="card-title">PDF Source</h2>
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
                <p className="text-detail">
                  Click to upload a custom PDF, or use the default fixture.
                </p>
              </div>

              <div className="demo-agent-section">
                <h2 className="card-title">Question</h2>
                <AutosizingTextarea
                  className="question-input"
                  value={question}
                  onChange={(e) => handleQuestionChange(e.target.value)}
                  placeholder="Ask the agent a question..."
                />
              </div>
            </section>

            <section
              className={`demo-agent-chaos r6-card-shell ${!ollamaModels.length && !ollamaLoading ? "is-offline" : ""}`}
              aria-label="Chaos mode and rule configuration"
            >
              <div className="chaos-config-header">
                <div>
                  <h2 className="card-title">Chaos Mode</h2>
                  <p className="text-detail">Rule configuration</p>
                </div>
                <label className="toggle-row">
                  <input
                    type="checkbox"
                    checked={chaosMode}
                    onChange={(e) => setChaosMode(e.target.checked)}
                  />
                  <span>{chaosMode ? "Enabled" : "Disabled"}</span>
                </label>
              </div>

              <div className="rule-config-grid">
                {RULES.map((rule) => {
                  const cfg = ruleConfigs[rule.id];
                  const r6Offline = rule.id === "R6" && !ollamaModels.length && !ollamaLoading;
                  return (
                    <div
                      key={rule.id}
                      className={`rule-config-item rule-config-item-${rule.id.toLowerCase()} ${r6Offline ? "is-offline" : ""}`}
                    >
                      <div className="rule-config-header">
                        <label className="toggle-row">
                          <input
                            type="checkbox"
                            checked={cfg.enabled}
                            onChange={() => toggleRule(rule.id)}
                            disabled={!chaosMode || r6Offline}
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
                        <div className={`rule-config-params ${r6Offline ? "is-offline" : ""}`}>
                          <label className="param-field">
                            <span className="param-label">Rate</span>
                            <input
                              type="number"
                              className="input narrow"
                              min={0}
                              max={1}
                              step={0.1}
                              value={cfg.rate}
                              disabled={r6Offline}
                              onChange={(e) =>
                                updateRuleRate(rule.id, parseFloat(e.target.value) || 0)
                              }
                            />
                          </label>
                          {rule.id === "R6" && (
                            <div
                              className={`r6-stack ${ollamaModels.length ? "online" : "offline"}`}
                            >
                              <p className="text-detail r6-help">
                                {ollamaLoading
                                  ? "Checking Ollama..."
                                  : ollamaModels.length
                                    ? "Ollama is available."
                                    : "Ollama is not running or no models are installed."}
                              </p>
                              {!ollamaModels.length && !ollamaLoading && (
                                <div className="r6-commands">
                                  <div className="command-pill mono">ollama serve</div>
                                  <div className="command-pill mono">ollama pull llama3.2</div>
                                </div>
                              )}
                              {ollamaModels.length > 0 && (
                                <label className="param-field r6-model-field">
                                  <span className="param-label">Model</span>
                                  <select
                                    className="select narrow"
                                    value={cfg.ollamaModel ?? ollamaModels[0]}
                                    onChange={(e) => updateR6Model(e.target.value)}
                                  >
                                    {ollamaModels.map((model) => (
                                      <option key={model} value={model}>
                                        {model}
                                      </option>
                                    ))}
                                  </select>
                                </label>
                              )}
                            </div>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </section>
          </div>

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
        </Card>

        {isRunning && (
          <Card>
            <CardContent>
              <div className="execution-status">
                <Loader2 size={20} className="spin" />
                <span>Agent is running...</span>
              </div>
            </CardContent>
          </Card>
        )}

        {executionError && (
          <div className="callout error">
            <span>{executionError}</span>
          </div>
        )}

        {agentResult && (
          <Card>
            <CardHeader>
              <CardTitle>Agent Response</CardTitle>
              <div className="segmented-control" aria-label="Agent response view">
                <button
                  type="button"
                  className={agentResponseView === "rendered" ? "active" : ""}
                  onClick={() => setAgentResponseView("rendered")}
                >
                  Rendered
                </button>
                <button
                  type="button"
                  className={agentResponseView === "raw" ? "active" : ""}
                  onClick={() => setAgentResponseView("raw")}
                >
                  Raw
                </button>
              </div>
            </CardHeader>
            <CardContent>
              {agentResponseView === "raw" ? (
                <pre className="mono result-pre">
                  {JSON.stringify(agentResult, null, 2)}
                </pre>
              ) : (
                <div className="agent-report-text">
                  {getAgentReport(agentResult)}
                </div>
              )}
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}
