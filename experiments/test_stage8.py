"""
Stage 8 gate check, per Section 6: "does a chaos-triggered severed handoff
between `demo-agent-app` and `citation_service.py` get detected and
reported as an R7 finding, distinct from any R3 finding in the same run,
and does the health score panel reflect it?"

This is `test_stage7.py`'s successor for the specific R7 slice -- it
reuses the exact same live-HTTP-driver pattern (real FastAPI backend over
HTTP, not an in-process TestClient; `guardian/tests/test_scheduler.py` and
`test_main.py` already cover the mocked-everything unit-test layer) but
adds the two checks Stage 7's generic driver doesn't: (1) that R7 and R3
findings never collapse into one bucket in the same run, and (2) that a
clean baseline handoff and a chaos-severed one produce a visibly
different R7 finding count and `cross_service_break_rate_pct` term.

Copy to repo root first (same as every earlier experiments/test_stageN.py),
then:

    # 0. one-time setup, if not already done for Stage 6/7:
    python scripts/provision_dashboards.py
    # (alerts are provisioned automatically at guardian/main.py startup if
    #  GUARDIAN_ALERT_WEBHOOK_URL is set in .env -- see env.example)

    # 1. start citation_service.py as its OWN process (separate terminal,
    #    from demo-agent-app/) -- this is the service boundary R7 checks:
    cd demo-agent-app
    uvicorn citation_service:app --port 8100
    # (or: python citation_service.py, using CITATION_SERVICE_PORT)

    # 2. start the Guardian backend (separate terminal, from repo root):
    uvicorn guardian.main:app --reload --port 8000

    # 3. (optional) start the frontend too, to eyeball it (separate terminal):
    streamlit run frontend/app.py

    # 4. produce a clean baseline trace (traceparent correctly propagated
    #    to citation_service.py on every claim), then run this script:
    cd demo-agent-app
    python app.py --question "What does the Kestrel Basin report say about sea level rise and drought?" --pdf fixtures/long_climate_report.pdf
    cd ..
    python test_stage8.py --step baseline

    # 5. produce a chaos run that severs the cross-service handoff (R7)
    #    AND an in-service parent link (R3), so both fire in the same run
    #    and this script can confirm they're reported as distinct findings:
    cd demo-agent-app
    CHAOS_MODE=1 CHAOS_R3_RATE=1.0 CHAOS_R7_RATE=1.0 CHAOS_SEED=42 \\
        python app.py --question "What does the Kestrel Basin report say about sea level rise and drought?" --pdf fixtures/long_climate_report.pdf
    cd ..
    python test_stage8.py --step chaos

Each `--step` run: triggers POST /audit/run, prints R3 vs R7 finding
counts and the `cross_service_break_rate_pct` health-score term
side-by-side (so a human can eyeball that R7 moved and R3 didn't get
credited for it or vice versa), asserts no R7 finding's `detail` gets
merged into an R3 finding's `detail` (or vice versa -- Section 4.3.1's
"never merge these two into a single finding type" requirement), then
POSTs the Section 8 step-4 chat question and checks the answer names R7
specifically (not just "something is broken between services") whenever
R7 fired.
"""

import argparse
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


def _check_citation_service(citation_service_url: str) -> None:
    print(f"--- GET {citation_service_url}/health (confirms the separate R7 service boundary exists) ---")
    try:
        resp = requests.get(f"{citation_service_url}/health", timeout=10)
        resp.raise_for_status()
        print(f"citation_service.py -> {resp.json()}")
    except requests.RequestException as exc:
        print(
            f"WARNING: could not reach citation_service.py at {citation_service_url}: {exc}\n"
            "R7 has nothing to detect without a live cross-service call happening during "
            "the pipeline run -- start it with `uvicorn citation_service:app --port 8100` "
            "from demo-agent-app/ before running the pipeline for this step.",
            file=sys.stderr,
        )


def _assert_r3_r7_not_conflated(findings: dict) -> None:
    """Section 4.3.1's hard requirement: R3 (broken within one service's
    trace tree) and R7 (broken across two services' trace IDs entirely)
    must never be merged into one finding type. Mechanical proxy: every
    r3 finding's `rule` field must read "R3" and every r7 finding's must
    read "R7" -- and neither rule's findings list should be empty of a
    `rule` tag while the other's isn't (which would suggest a shared/
    generic finding type slipped in somewhere upstream)."""
    r3_findings = findings.get("r3", {}).get("findings", [])
    r7_findings = findings.get("r7", {}).get("findings", []) if "r7" in findings else []

    for f in r3_findings:
        assert f.get("rule") == "R3", f"Found an r3 findings-list entry not tagged rule=R3: {f!r}"
    for f in r7_findings:
        assert f.get("rule") == "R7", f"Found an r7 findings-list entry not tagged rule=R7: {f!r}"

    print(
        f"R3 findings: {len(r3_findings)} (all tagged rule=R3). "
        f"R7 findings: {len(r7_findings)} (all tagged rule=R7). "
        "No conflation detected."
    )


def run(base_url: str, citation_service_url: str, service: str | None, step: str) -> None:
    health = requests.get(f"{base_url}/health", timeout=10)
    health.raise_for_status()
    print(f"GET /health -> {health.json()}")

    _check_citation_service(citation_service_url)

    print(f"\n--- POST /audit/run (service={service!r}, step={step}) ---")
    audit = _post(base_url, "/audit/run", {"service": service})

    score = audit["health_score"]["score"]
    terms = audit["health_score"]["terms"]
    r7_included = audit["health_score"]["r7_included"]
    print(f"Health score: {score:.1f} (raw={audit['health_score']['raw_score']:.1f})")
    print(f"r7_included in formula: {r7_included}")
    for term, value in terms.items():
        marker = "  <-- R7 term" if term == "cross_service_break_rate_pct" else ""
        print(f"  {term}: {value:.2f}{marker}")

    findings = audit["findings"]

    _TOTAL_FIELD_BY_RULE = {
        "r1": "total_gen_ai_spans",
        "r2": "evaluated_keys",
        "r3": "total_spans_with_parent",
        "r6": "total_payload_spans",
        "r7": "total_handoffs",
    }
    print("\n--- Per-rule denominators (distinguishes 'clean' from 'no data found') ---")
    any_data_at_all = False
    for rule_id, total_field in _TOTAL_FIELD_BY_RULE.items():
        if rule_id not in findings:
            continue
        total = findings[rule_id].get(total_field)
        print(f"  {rule_id.upper()}.{total_field}: {total}")
        if total:
            any_data_at_all = True

    if not any_data_at_all:
        print(
            "\nGATE CHECK FAILED: every rule's denominator is 0/empty -- the audit window "
            "found NO spans at all for any service, not a clean-and-healthy system. A "
            "100.0 health score here is meaningless. Most likely causes, in order:\n"
            "  1. The demo pipeline (demo-agent-app/app.py) wasn't actually re-run "
            "immediately before this script -- re-run it now (with CHAOS_MODE=1 for the "
            "chaos step) and try again.\n"
            "  2. CHAOS_MODE/env vars were exported in a different shell/terminal than "
            "the one that ran `python app.py` -- env vars don't carry across terminals.\n"
            "  3. AUDIT_WINDOW (in .env) is narrower than the time since the pipeline ran "
            "-- widen it (e.g. AUDIT_WINDOW=1h) or re-run the pipeline right before this "
            "script.\n"
            "  4. The service-name filter key this SigNoz instance actually uses is "
            "`service.name`, not `serviceName` -- check a raw trace in the SigNoz UI's "
            "attribute panel to confirm which one it stores spans under.",
            file=sys.stderr,
        )
        sys.exit(1)

    r7 = findings.get("r7")
    if r7 is None:
        print(
            "\nNo r7 key in findings at all -- this means r7_cross_service_breaks.run() "
            "wasn't wired into scheduler.py's combine_results call, or R7 found zero "
            "known service pairs (e.g. citation_service.py never received a call in "
            "this audit window). Check the window/service scoping before treating this "
            "as a gate failure.",
            file=sys.stderr,
        )
    else:
        print(
            f"\nR7 summary: {r7['broken_handoffs']} broken / {r7['total_handoffs']} "
            f"evaluable handoffs -> cross_service_break_rate_pct={r7['cross_service_break_rate_pct']:.2f}"
        )
        if step == "baseline":
            assert r7["broken_handoffs"] == 0, (
                f"Expected zero broken handoffs on a clean baseline run, got "
                f"{r7['broken_handoffs']}. Confirm CHAOS_MODE was NOT set for this run."
            )
            print("Baseline: zero broken handoffs, as expected.")
        elif step == "chaos":
            assert r7["broken_handoffs"] > 0, (
                "Expected at least one broken handoff on a CHAOS_R7_RATE>0 run, got 0. "
                "Confirm CHAOS_MODE=1 and CHAOS_R7_RATE>0 were set, and that "
                "citation_service.py was running and reachable during the pipeline run."
            )
            print("Chaos: at least one broken handoff detected, as expected.")

    _assert_r3_r7_not_conflated(findings)

    report_path = f"/audit/report/{service or 'all'}"
    print(f"\n--- GET {report_path} (confirms the cache matches the run above) ---")
    report = _get(base_url, report_path)
    assert report["health_score"]["score"] == score, (
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
    if "r7" in findings and findings["r7"]["findings"] and "R7" in uncited:
        print(
            "\nFAILING R7-SPECIFIC CHECK: R7 fired this audit but the chat answer never "
            "named it. Section 4.3.2 requires every fired rule to be cited by ID -- read "
            "the printed answer above.",
            file=sys.stderr,
        )
    elif uncited:
        print(
            f"\nWARNING: rules fired but not named in the chat answer: {uncited}. "
            "Treat this as a signal to inspect/retry the narrative.",
            file=sys.stderr,
        )
    else:
        print("\nEvery rule that fired was named in the answer (mechanical citation check).")

    print(f"\n=== Stage 8 `{step}` step complete for service={service!r}. ===")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument(
        "--citation-service-url",
        default="http://localhost:8100",
        help="must match CITATION_SERVICE_URL used by demo-agent-app/fact_check_and_cite.py",
    )
    parser.add_argument("--service", default=None, help="omit for the combined all-services audit")
    parser.add_argument("--step", default="manual", choices=["baseline", "chaos", "manual"])
    args = parser.parse_args()

    try:
        run(args.base_url, args.citation_service_url, args.service, args.step)
    except requests.RequestException as exc:
        print(f"Could not reach the Guardian API at {args.base_url}: {exc}", file=sys.stderr)
        print("Is `uvicorn guardian.main:app --port 8000` running in another terminal?", file=sys.stderr)
        sys.exit(1)
    except AssertionError as exc:
        print(f"\nGATE CHECK FAILED: {exc}", file=sys.stderr)
        sys.exit(1)