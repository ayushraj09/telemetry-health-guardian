"""Guardian rule engine (Section 4.3.1).

Each rule module exposes:
  - a pure `evaluate(...)` function: the exact detection logic, unit-testable
    with plain data, no MCP/network involved.
  - `fetch_*` / `run(...)`: the MCP-backed adapter that pulls real data via
    `guardian.mcp_client.SignozMCPClient` and calls `evaluate`.

R5 is intentionally not implemented anywhere in this package -- no stub,
no placeholder -- per Section 4.3.1.
"""