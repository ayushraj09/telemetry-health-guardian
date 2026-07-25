"use client";

import { useState, useRef } from "react";
import { FileText, Upload, X } from "lucide-react";
import { Button } from "@/components/ui/button";
import { AutosizingTextarea } from "@/components/ui/autosizing-textarea";
import { useToast } from "@/components/ui/toast";

const DEMO_API_BASE = process.env.NEXT_PUBLIC_DEMO_AGENT_API_URL ?? "http://localhost:8001";
const DEFAULT_PDF_NAME = "fixtures/long_climate_report.pdf";

export function DemoAgentPanel() {
  const [question, setQuestion] = useState("What does the Kestrel Basin report say about sea level rise and drought?");
  const [chaosMode, setChaosMode] = useState(false);
  const [uploadedFile, setUploadedFile] = useState<File | null>(null);
  const [isRunning, setIsRunning] = useState(false);
  const [result, setResult] = useState<Record<string, unknown> | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const toast = useToast();

  const activePdf = uploadedFile ? uploadedFile.name : DEFAULT_PDF_NAME;

  async function handleRun() {
    setIsRunning(true);
    setResult(null);
    try {
      let response: Response;
      if (uploadedFile) {
        const formData = new FormData();
        formData.append("question", question);
        formData.append("chaos_mode", String(chaosMode));
        formData.append("uploaded_pdf", uploadedFile);
        response = await fetch(`${DEMO_API_BASE}/run/upload`, {
          method: "POST",
          body: formData,
        });
      } else {
        response = await fetch(`${DEMO_API_BASE}/run`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ question, chaos_mode: chaosMode }),
        });
      }
      if (!response.ok) {
        const body = await response.json().catch(() => ({ detail: response.statusText }));
        throw new Error(body.detail ?? "Run failed");
      }
      const data = await response.json();
      setResult(data);
      toast.push({ title: "Agent run complete", detail: "Results displayed below." });
    } catch (err) {
      toast.push({
        title: "Agent run failed",
        detail: err instanceof Error ? err.message : "Unknown error",
        tone: "error",
      });
    } finally {
      setIsRunning(false);
    }
  }

  return (
    <div className="demo-agent-panel panel">
      <div>
        <h2>Live Demo Agent</h2>
        <p className="muted">Run the CLI pipeline through a subprocess API with PDF, question, and chaos controls.</p>
      </div>
      <div className="demo-controls">
        <label className="field-label">
          PDF Source
          <div className="pdf-selector">
            <button className="input-like" type="button" onClick={() => fileInputRef.current?.click()}>
              <FileText size={16} /> {activePdf}
            </button>
            {uploadedFile && (
              <button className="btn-icon-sm" type="button" title="Remove uploaded PDF" onClick={() => setUploadedFile(null)}>
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
          <span className="muted hint">Click to upload a custom PDF, or use the default fixture.</span>
        </label>
        <label className="field-label">
          Question
          <AutosizingTextarea className="question-input" value={question} onChange={(e) => setQuestion(e.target.value)} />
        </label>
        <label className="field-label checkbox-row">
          <input type="checkbox" checked={chaosMode} onChange={(e) => setChaosMode(e.target.checked)} />
          Chaos mode
        </label>
        <Button disabled={isRunning || !question.trim()} onClick={handleRun} variant="primary">
          <Upload size={16} /> {isRunning ? "Running..." : "Run live agent"}
        </Button>
      </div>
      {result && (
        <details className="agent-result" open>
          <summary>Agent Output</summary>
          <pre className="mono result-pre">{JSON.stringify(result, null, 2)}</pre>
        </details>
      )}
    </div>
  );
}
