"use client";

import { FileText, ToggleLeft, Upload } from "lucide-react";
import { Button } from "@/components/ui/button";

export function DemoAgentPanel() {
  return (
    <div className="demo-agent-panel panel">
      <div>
        <h2>Live Demo Agent</h2>
        <p className="muted">Phase 1.5 target: run the CLI pipeline through a subprocess API with PDF, question, model, and chaos controls.</p>
      </div>
      <div className="demo-controls">
        <label className="field-label">
          PDF
          <button className="input-like" type="button"><FileText size={16} /> fixtures/long_climate_report.pdf</button>
        </label>
        <label className="field-label">
          Question
          <input value="What does the report say about climate risk?" readOnly />
        </label>
        <button className="input-like" type="button"><ToggleLeft size={16} /> Chaos off</button>
        <Button disabled><Upload size={16} /> Run live agent</Button>
      </div>
    </div>
  );
}
