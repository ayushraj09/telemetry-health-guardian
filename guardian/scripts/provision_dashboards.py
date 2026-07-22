"""Stage 6 dashboard provisioning (Section 4.4), via MCP.

Run once (idempotency note below) from the repo root:

    python scripts/provision_dashboards.py --dry-run   # print the payload, no MCP call
    python scripts/provision_dashboards.py              # actually create it

Widget schema (CONFIRMED, 2026-07-22): earlier revisions of this script
guessed at the widget `query` envelope and got it wrong twice against a
live server. The actual shape is defined by the SigNoz MCP server's own
Go validator, `panelvalidator.Panel` / `panelvalidator.Query`
(github.com/SigNoz/signoz-mcp-server/pkg/dashboard/panelbuilder/panel_validator.go,
tag v0.4.1) -- this is the exact code `signoz_create_dashboard` runs
payloads through server-side, so it's ground truth, not a template
guess. Key points that differ from what earlier revisions assumed:
  - `query.builder` is `{"queryData": [...], "queryFormulas": [...]}` --
    the field is `queryData`, not `queries`.
  - Each `queryData[]` entry is a FLAT `BuilderQuery` object
    (`queryName`, `dataSource`, `aggregateOperator`, `aggregateAttribute`,
    `timeAggregation`, `spaceAggregation`, `groupBy`, `filters`, ...) --
    there is no `{"type": ..., "spec": {...}}` wrapper.
  - `query.promql` is a flat list of `{"name", "query", "disabled"}`
    dicts, sibling to `queryType`, not nested under any composite
    wrapper.
  - Each widget (a `Panel`) needs `opacity`/`nullZeroValues`/
    `selectedLogFields`/`selectedTracesFields` alongside `id`/
    `panelTypes`/`title`/`description`/`query`, matching what
    `panelvalidator.CreateDefaultPanel` produces.

Idempotency: `signoz_create_dashboard` has no documented upsert-by-title
behavior, so re-running this creates a second dashboard. Check
`signoz_list_dashboards` for an existing "Telemetry Health Guardian"
dashboard before re-running, or delete the old one via
`signoz_delete_dashboard` first.

Grid layout constants (`GRID_COLUMNS`, `WIDGET_W`, `WIDGET_H`) are taken
from the SigNoz MCP server's own Go `dashboardbuilder` package defaults
(`pkg/dashboard/dashboardbuilder`, GridColumns=12, DefaultWidgetWidth=6,
DefaultWidgetHeight=6) -- two widgets per row.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from guardian.mcp_client import SignozMCPClient  # noqa: E402

GRID_COLUMNS = 12
WIDGET_W = 6
WIDGET_H = 6

SPEC_PATH = Path(__file__).resolve().parent.parent / "health_dashboard.json"


def _builder_query_data(metric_name: str, group_by: list[str]) -> dict[str, Any]:
    """A single flat `panelvalidator.BuilderQuery` entry for a metrics query."""
    return {
        "queryName": "A",
        "dataSource": "metrics",
        "aggregateOperator": "avg",
        "aggregateAttribute": {"key": metric_name, "dataType": "", "type": ""},
        "timeAggregation": "avg",
        "spaceAggregation": "avg",
        "functions": [],
        "filters": {"items": [], "op": "AND"},
        "groupBy": [{"key": key} for key in group_by],
        "expression": "A",
        "disabled": False,
        "having": [],
        "limit": None,
        "stepInterval": None,
        "orderBy": [],
        "reduceTo": "",
        "legend": "",
    }


def _promql_query_data(metric_name: str, group_by: list[str]) -> dict[str, Any]:
    # Prometheus 3.x UTF-8 quoted-selector form for a dotted OTel metric name,
    # per signoz-mcp-server's `signoz://promql/instructions` resource
    # description (not independently confirmed live -- see module docstring).
    by_clause = f"by ({', '.join(k.replace('.', '_') for k in group_by)}) " if group_by else ""
    return {
        "name": "A",
        "query": f'avg {by_clause}({{"{metric_name}"}})',
        "disabled": False,
    }


def _base_panel(widget_id: str, title: str, description: str, panel_type: str, query: dict[str, Any]) -> dict[str, Any]:
    """Fields every `panelvalidator.Panel` needs, matching CreateDefaultPanel's output."""
    return {
        "id": widget_id,
        "panelTypes": panel_type,
        "title": title,
        "description": description,
        "query": query,
        "opacity": "1",
        "nullZeroValues": "zero",
        "selectedLogFields": None,
        "selectedTracesFields": None,
    }


def _builder_query_envelope(metric_name: str, group_by: list[str]) -> dict[str, Any]:
    return {
        "queryType": "builder",
        "builder": {"queryData": [_builder_query_data(metric_name, group_by)], "queryFormulas": []},
        "promql": [],
        "clickhouse_sql": [],
        "id": str(uuid.uuid4()),
        "unit": "",
    }


def _promql_query_envelope(metric_name: str, group_by: list[str]) -> dict[str, Any]:
    return {
        "queryType": "promql",
        "builder": {"queryData": [], "queryFormulas": []},
        "promql": [_promql_query_data(metric_name, group_by)],
        "clickhouse_sql": [],
        "id": str(uuid.uuid4()),
        "unit": "",
    }


def _build_widget(panel: dict[str, Any], x: int, y: int) -> tuple[dict[str, Any], dict[str, Any]]:
    widget_id = str(uuid.uuid4())
    panel_type = "table" if panel["panel_type"] == "table" else "graph"
    query = _builder_query_envelope(panel["metric_name"], panel.get("group_by", []))
    widget = _base_panel(widget_id, panel["title"], panel.get("description", ""), panel_type, query)
    layout_item = {"i": widget_id, "x": x, "y": y, "w": WIDGET_W, "h": WIDGET_H, "moved": False, "static": False}
    return widget, layout_item


def _build_promql_comparison_widgets(spec: dict[str, Any], x: int, y: int) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    cmp = spec["promql_vs_builder_comparison"]
    out = []

    builder_id = str(uuid.uuid4())
    builder_query = _builder_query_envelope(cmp["metric_name"], cmp.get("group_by", []))
    builder_widget = _base_panel(builder_id, f"{cmp['title']} (Builder)", "", "graph", builder_query)
    out.append(
        (builder_widget, {"i": builder_id, "x": x, "y": y, "w": WIDGET_W, "h": WIDGET_H, "moved": False, "static": False})
    )

    promql_id = str(uuid.uuid4())
    promql_query = _promql_query_envelope(cmp["metric_name"], cmp.get("group_by", []))
    promql_widget = _base_panel(promql_id, f"{cmp['title']} (PromQL)", "", "graph", promql_query)
    out.append(
        (
            promql_widget,
            {"i": promql_id, "x": x + WIDGET_W, "y": y, "w": WIDGET_W, "h": WIDGET_H, "moved": False, "static": False},
        )
    )
    return out


def build_dashboard_payload() -> dict[str, Any]:
    spec = json.loads(SPEC_PATH.read_text())

    widgets: list[dict[str, Any]] = []
    layout: list[dict[str, Any]] = []
    x, y = 0, 0

    for panel in spec["panels"]:
        widget, layout_item = _build_widget(panel, x, y)
        widgets.append(widget)
        layout.append(layout_item)
        if x + WIDGET_W >= GRID_COLUMNS:
            x, y = 0, y + WIDGET_H
        else:
            x += WIDGET_W

    if x != 0:  # start the comparison row fresh so it isn't split mid-row
        x, y = 0, y + WIDGET_H
    for widget, layout_item in _build_promql_comparison_widgets(spec, x, y):
        widgets.append(widget)
        layout.append(layout_item)

    return {
        "title": spec["title"],
        "description": spec["description"],
        "tags": spec.get("tags", []),
        "layout": layout,
        "widgets": widgets,
        "variables": {},
    }


async def main(dry_run: bool) -> None:
    payload = build_dashboard_payload()
    if dry_run:
        print(json.dumps(payload, indent=2))
        return

    async with SignozMCPClient() as client:
        result = await client.call_tool("signoz_create_dashboard", payload)
        print("signoz_create_dashboard result:", json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run))