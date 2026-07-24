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
from pydantic import BaseModel

APP_DIR = Path(__file__).resolve().parent
DEFAULT_PDF = APP_DIR / "fixtures" / "long_climate_report.pdf"

app = FastAPI(title="Demo Agent API")


class RunRequest(BaseModel):
    question: str
    pdf_path: str | None = None
    chaos_mode: bool = False
    ollama_model: str | None = None


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


def _run_cli(question: str, pdf_path: Path, chaos_mode: bool, ollama_model: str | None) -> dict[str, Any]:
    env = os.environ.copy()
    if chaos_mode:
        env["CHAOS_MODE"] = "1"
    else:
        env.pop("CHAOS_MODE", None)
    if ollama_model:
        env["CHAOS_R6_OLLAMA_MODEL"] = ollama_model

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
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Demo agent returned non-JSON output: {completed.stdout[-500:]}") from exc


@app.post("/run")
async def run(body: RunRequest) -> dict[str, Any]:
    return _run_cli(body.question, _safe_pdf_path(body.pdf_path), body.chaos_mode, body.ollama_model)


@app.post("/run/upload")
async def run_upload(
    question: str = Form(...),
    chaos_mode: bool = Form(False),
    ollama_model: str | None = Form(None),
    uploaded_pdf: UploadFile = File(...),
) -> dict[str, Any]:
    if uploaded_pdf.content_type not in {"application/pdf", "application/octet-stream"}:
        raise HTTPException(status_code=400, detail="Upload must be a PDF.")
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp:
        temp.write(await uploaded_pdf.read())
        temp_path = Path(temp.name)
    try:
        return _run_cli(question, temp_path, chaos_mode, ollama_model)
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
