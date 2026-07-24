import { Activity, Bell, Bot, Database, Gauge, GitBranch, MessageSquare, Server } from "lucide-react";

const nodes = [
  { icon: Bot, label: "Target agent" },
  { icon: Activity, label: "otel-griptape" },
  { icon: Database, label: "SigNoz" },
  { icon: Gauge, label: "Rule engine" },
  { icon: GitBranch, label: "MCP" },
  { icon: Bell, label: "Writeback" },
  { icon: Server, label: "Dashboards" },
  { icon: MessageSquare, label: "Guardian UI" },
];

export function ArchitectureDiagram() {
  return (
    <div className="architecture panel">
      {nodes.map((node, index) => {
        const Icon = node.icon;
        return (
          <div className="architecture-node" key={node.label}>
            <Icon size={18} />
            <span>{node.label}</span>
            {index < nodes.length - 1 ? <i aria-hidden /> : null}
          </div>
        );
      })}
    </div>
  );
}
