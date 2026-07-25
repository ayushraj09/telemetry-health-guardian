"""FastAPI wrapper for the demo agent CLI.

This intentionally shells out to ``python app.py`` for every run. The demo
agent's chaos module installs process-global monkeypatches, so importing and
running the pipeline inside this API process would leak chaos state across
requests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

APP_DIR = Path(__file__).resolve().parent
DEFAULT_PDF = APP_DIR / "fixtures" / "long_climate_report.pdf"
DEMO_AGENT_SERVICE_NAME = "demo-agent-app"

app = FastAPI(title="Demo Agent API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class RuleConfig(BaseModel):
    enabled: bool = True
    rate: float = 0.3
    ollamaModel: str | None = None
    numCtx: int | None = None


class RunRequest(BaseModel):
    question: str
    pdf_path: str | None = None
    chaos_mode: bool = False
    ollama_model: str | None = None
    rule_configs: dict[str, RuleConfig] | None = None
    audit_window: str | None = None


def _safe_pdf_path(pdf_path: str | None) -> Path:
    path = Path(pdf_path).expanduser() if pdf_path else DEFAULT_PDF
    if not path.is_absolute():
        path = APP_DIR / path
    path = path.resolve()
    if not path.exists():
        raise HTTPException(status_code=400, detail=f"PDF not found: {path}")
    if path.suffix.lower() != ".pdf":
        raise HTTPException(status_code=400, detail="Source file must be a PDF.")
    return path


def _apply_rule_configs_to_env(env: dict[str, str], rule_configs: dict[str, RuleConfig] | None) -> None:
    """Translate frontend rule_configs into CHAOS_* env vars for the subprocess."""
    if not rule_configs:
        return

    rule_env_map = {
        "R1": "CHAOS_R1_RATE",
        "R2": "CHAOS_R2_RATE",
        "R3": "CHAOS_R3_RATE",
        "R6": "CHAOS_R6_RATE",
        "R7": "CHAOS_R7_RATE",
    }

    for rule_id, cfg in rule_configs.items():
        rule_upper = rule_id.upper()
        env_key = rule_env_map.get(rule_upper)
        if env_key:
            # If disabled, set rate to 0 so chaos skips this rule
            env[env_key] = str(cfg.rate if cfg.enabled else 0.0)

        # R6-specific params
        if rule_upper == "R6" and cfg.enabled:
            if cfg.ollamaModel:
                env["CHAOS_R6_OLLAMA_MODEL"] = cfg.ollamaModel
            if cfg.numCtx is not None:
                env["CHAOS_R6_NUM_CTX"] = str(cfg.numCtx)


def _run_cli(
    question: str,
    pdf_path: Path,
    chaos_mode: bool,
    ollama_model: str | None,
    rule_configs: dict[str, RuleConfig] | None = None,
) -> dict[str, Any]:
    env = os.environ.copy()
    env["OTEL_SERVICE_NAME"] = DEMO_AGENT_SERVICE_NAME
    if chaos_mode:
        env["CHAOS_MODE"] = "1"
    else:
        env.pop("CHAOS_MODE", None)
    if ollama_model:
        env["CHAOS_R6_OLLAMA_MODEL"] = ollama_model

    # Apply per-rule config env vars
    _apply_rule_configs_to_env(env, rule_configs)

    completed = subprocess.run(
        [sys.executable, "app.py", "--question", question, "--pdf", str(pdf_path)],
        cwd=APP_DIR,
        env=env,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    if completed.returncode != 0:
        raise HTTPException(status_code=500, detail=completed.stderr.strip() or "Demo agent run failed.")

    # app.py prints JSON as the last thing on stdout, but libraries may emit
    # warnings/log lines before it. Extract the last JSON object.
    stdout = completed.stdout.strip()
    # Find the start of the last top-level JSON object
    last_brace = stdout.rfind("\n{")
    if last_brace != -1:
        stdout = stdout[last_brace + 1:]
    elif not stdout.startswith("{"):
        # Fallback: find the first '{' in the output
        first_brace = stdout.find("{")
        if first_brace != -1:
            stdout = stdout[first_brace:]

    try:
        return json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Demo agent returned non-JSON output: {completed.stdout[-500:]}",
        ) from exc


@app.post("/run")
async def run(body: RunRequest) -> dict[str, Any]:
    return _run_cli(
        body.question,
        _safe_pdf_path(body.pdf_path),
        body.chaos_mode,
        body.ollama_model,
        rule_configs=body.rule_configs,
    )


@app.post("/run/upload")
async def run_upload(
    question: str = Form(...),
    chaos_mode: bool = Form(False),
    ollama_model: str | None = Form(None),
    rule_configs: str | None = Form(None),
    audit_window: str | None = Form(None),
    uploaded_pdf: UploadFile = File(...),
) -> dict[str, Any]:
    if uploaded_pdf.content_type not in {"application/pdf", "application/octet-stream"}:
        raise HTTPException(status_code=400, detail="Upload must be a PDF.")

    # Parse rule_configs from JSON string (multipart forms can't nest objects)
    parsed_rule_configs: dict[str, RuleConfig] | None = None
    if rule_configs:
        try:
            raw = json.loads(rule_configs)
            parsed_rule_configs = {k: RuleConfig(**v) for k, v in raw.items()}
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp:
        temp.write(await uploaded_pdf.read())
        temp_path = Path(temp.name)
    try:
        return _run_cli(question, temp_path, chaos_mode, ollama_model, rule_configs=parsed_rule_configs)
    finally:
        temp_path.unlink(missing_ok=True)


@app.get("/ollama/models")
async def ollama_models() -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get("http://localhost:11434/api/tags")
            response.raise_for_status()
    except httpx.HTTPError:
        return []
    payload = response.json()
    return [model["name"] for model in payload.get("models", []) if "name" in model]
