"""
Stage 7 gate check, per Section 6: "does the full verification flow
(Section 8) run end-to-end without manual intervention?"

This drives the LIVE FastAPI backend over HTTP (not an in-process
TestClient -- guardian/tests/test_main.py already covers routing logic
with everything mocked; this script is the real thing, the same way
test_stage6.py drove a real MCP session instead of a mocked one).

Copy to repo root first (same as test_stage3.py/.../test_stage6.py), then:

    # 0. one-time setup, if not already done for Stage 6:
    python scripts/provision_dashboards.py
    # (alerts are provisioned automatically at guardian/main.py startup if
    #  GUARDIAN_ALERT_WEBHOOK_URL is set in .env -- see env.example)

    # 1. start the backend (separate terminal, from repo root):
    uvicorn guardian.main:app --reload --port 8000

    # 2. (optional) start the frontend too, to eyeball it (separate terminal):
    streamlit run frontend/app.py

    # 3. produce a clean baseline trace, then run this script:
    cd demo-agent-app
    python app.py --question "What does the Kestrel Basin report say about sea level rise and drought?" --pdf fixtures/long_climate_report.pdf
    cd ..
    python test_stage7.py --step baseline

    # 4. produce a chaos run (all four required rules), then check the drop:
    cd demo-agent-app
    CHAOS_MODE=1 CHAOS_R1_RATE=1.0 CHAOS_R2_RATE=1.0 CHAOS_R3_RATE=1.0 CHAOS_R6_RATE=1.0 CHAOS_SEED=42 \\
        python app.py --question "What does the Kestrel Basin report say about sea level rise and drought?" --pdf fixtures/long_climate_report.pdf
    cd ..
    python test_stage7.py --step chaos

Each `--step` run: triggers POST /audit/run, prints the resulting health
score + per-rule finding counts, fetches GET /audit/report/{service} to
confirm it matches what's cached, then POSTs the Section 8 step-4 question
("why did the research pipeline's score drop?") to /chat and checks the
answer's `rules_fired_but_uncited` list -- Section 8 step 4's literal gate
is "confirm the response names each rule that fired ... and correctly
distinguishes all injected faults from each other," and an empty
`rules_fired_but_uncited` list is exactly the mechanical proxy for that
(see narrative.py::validate_citations' own docstring on what this can and
can't prove -- a human read of the printed answer is still the real check).
"""

import argparse
import json
import sys

import requests


def _post(base_url: str, path: str, payload: dict) -> dict:
    resp = requests.post(f"{base_url}{path}", json=payload, timeout=120)
    resp.raise_for_status()
    return resp.json()


def _get(base_url: str, path: str) -> dict:
    resp = requests.get(f"{base_url}{path}", timeout=30)
    resp.raise_for_status()
    return resp.json()


def run(base_url: str, service: str | None, step: str) -> None:
    health = requests.get(f"{base_url}/health", timeout=10)
    health.raise_for_status()
    print(f"GET /health -> {health.json()}")

    print(f"\n--- POST /audit/run (service={service!r}, step={step}) ---")
    audit = _post(base_url, "/audit/run", {"service": service})
    print(f"Health score: {audit['health_score']['score']:.1f} (raw={audit['health_score']['raw_score']:.1f})")
    for term, value in audit["health_score"]["terms"].items():
        print(f"  {term}: {value:.2f}")
    for rule_id in ("r1", "r2", "r3", "r6", "r7"):
        if rule_id in audit["findings"]:
            count = len(audit["findings"][rule_id]["findings"])
            print(f"  {rule_id.upper()}: {count} findings")
    if audit.get("narrative"):
        print(f"\nNarrative:\n{audit['narrative']}")
    elif audit.get("narrative_error"):
        print(f"\nNarrative generation FAILED: {audit['narrative_error']}", file=sys.stderr)

    report_path = f"/audit/report/{service or 'all'}"
    print(f"\n--- GET {report_path} (confirms the cache matches the run above) ---")
    report = _get(base_url, report_path)
    assert report["health_score"]["score"] == audit["health_score"]["score"], (
        "GET /audit/report disagrees with the POST /audit/run that just populated it -- "
        "the cache isn't being read/written consistently."
    )
    print("Matches. Cache consistency OK.")

    print("\n--- POST /chat (Section 8 step 4's exact question) ---")
    chat = _post(
        base_url,
        "/chat",
        {"question": "Why did the research pipeline's score drop?", "service": service},
    )
    print(f"Answer:\n{chat['answer']}")
    uncited = chat.get("rules_fired_but_uncited") or []
    if uncited:
        print(
            f"\nWARNING: rules fired but not named in the chat answer: {uncited}. "
            "Per Section 4.3.2 this is required -- read the printed answer above "
            "and treat this as a signal to inspect/retry the narrative, not just "
            "a log line to ignore.",
            file=sys.stderr,
        )
    else:
        print("\nEvery rule that fired was named in the answer (mechanical citation check).")

    print(f"\n=== Stage 7 `{step}` step complete for service={service!r}. ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--service", default=None, help="omit for the combined all-services audit")
    parser.add_argument("--step", default="manual", choices=["baseline", "chaos", "manual"])
    args = parser.parse_args()

    try:
        run(args.base_url, args.service, args.step)
    except requests.RequestException as exc:
        print(f"Could not reach the Guardian API at {args.base_url}: {exc}", file=sys.stderr)
        print("Is `uvicorn guardian.main:app --port 8000` running in another terminal?", file=sys.stderr)
        sys.exit(1)
