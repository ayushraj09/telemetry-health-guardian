"""Guardian rule engine (Section 4.3.1).

Each rule module exposes:
  - a pure `evaluate(...)` function: the exact detection logic, unit-testable
    with plain data, no MCP/network involved.
  - `fetch_*` / `run(...)`: the MCP-backed adapter that pulls real data via
    `guardian.mcp_client.SignozMCPClient` and calls `evaluate`.

Implemented so far (Stage 3: R1, R2; Stage 4: R3, R6):
  - r1_missing_fields.py -- required GenAI attribute presence + span-naming
  - r2_cardinality.py    -- raw content leaking into indexed attributes
  - r3_orphaned_spans.py -- broken parent links within one service's traces
  - r6_silent_truncation.py -- tool-payload / model-output truncation

r7_cross_service_breaks.py does not exist yet -- Section 6 says not to
create it until Stage 7 is reached, only if time/scope allows.

R5 is intentionally not implemented anywhere in this package -- no stub,
no placeholder -- per Section 4.3.1.
"""