"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "motion/react";
import { Bell, BellRing, ChevronDown, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { useToast } from "@/components/ui/toast";
import { isRuleId, type RuleId } from "@/lib/rule-meta";

const API_BASE = process.env.NEXT_PUBLIC_GUARDIAN_API_URL ?? "http://localhost:8000";

export type AlertEvent = {
  id: string;
  status: "firing" | "resolved";
  alert_name: string;
  severity: string;
  summary: string;
  description: string;
  rule_id: string;
  service: string;
  starts_at: string;
  ends_at: string;
  received_at: number;
  labels: Record<string, string>;
};

function timeAgo(ts: number): string {
  const seconds = Math.floor(Date.now() / 1000 - ts);
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}

function severityTone(severity: string): "ok" | "error" {
  if (severity === "critical" || severity === "error") return "error";
  return "error"; // All alerts are important
}

export function AlertNotifications() {
  const [alerts, setAlerts] = useState<AlertEvent[]>([]);
  const [open, setOpen] = useState(false);
  const [connected, setConnected] = useState(false);
  const [hasNew, setHasNew] = useState(false);
  const eventSourceRef = useRef<EventSource | null>(null);
  const toast = useToast();

  // Fetch existing alerts on mount
  useEffect(() => {
    fetch(`${API_BASE}/alerts`)
      .then((res) => res.json())
      .then((data: AlertEvent[]) => {
        if (Array.isArray(data)) {
          setAlerts(data.slice(0, 20));
        }
      })
      .catch(() => {
        // Silently fail -- alerts are best-effort
      });
  }, []);

  // SSE connection
  useEffect(() => {
    const es = new EventSource(`${API_BASE}/alerts/stream`);
    eventSourceRef.current = es;

    es.addEventListener("connected", () => {
      setConnected(true);
    });

    es.addEventListener("alert", (event) => {
      try {
        const alert: AlertEvent = JSON.parse(event.data);
        setAlerts((prev) => [alert, ...prev].slice(0, 50));
        setHasNew(true);

        // Push toast notification
        toast.push({
          title: `${alert.status === "resolved" ? "✓ Resolved" : "⚠ Alert"}: ${alert.alert_name}`,
          detail: alert.summary || alert.description,
          tone: alert.status === "resolved" ? "ok" : severityTone(alert.severity),
        });
      } catch {
        // Ignore unparseable events
      }
    });

    es.addEventListener("ping", () => {
      // Keepalive — no action needed
    });

    es.onerror = () => {
      setConnected(false);
      // EventSource auto-reconnects
    };

    return () => {
      es.close();
      eventSourceRef.current = null;
    };
  }, [toast]);

  const togglePanel = useCallback(() => {
    setOpen((prev) => !prev);
    if (hasNew) setHasNew(false);
  }, [hasNew]);

  const dismissAlert = useCallback((id: string) => {
    setAlerts((prev) => prev.filter((a) => a.id !== id));
  }, []);

  const firingCount = alerts.filter((a) => a.status === "firing").length;

  return (
    <div className="alert-notifications">
      <button
        type="button"
        className="alert-bell-button"
        onClick={togglePanel}
        aria-label={`Alerts${firingCount ? ` (${firingCount} active)` : ""}`}
      >
        {hasNew || firingCount > 0 ? <BellRing size={18} /> : <Bell size={18} />}
        {firingCount > 0 && <span className="alert-count-badge">{firingCount}</span>}
        {connected && <span className="alert-connected-dot" />}
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            className="alert-panel"
            initial={{ opacity: 0, y: -8, scale: 0.96 }}
            animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -8, scale: 0.96 }}
            transition={{ duration: 0.15 }}
          >
            <div className="alert-panel-header">
              <strong>Alerts</strong>
              <span className="text-detail">
                {connected ? "Live" : "Disconnected"} · {alerts.length} total
              </span>
              <button type="button" className="alert-panel-close" onClick={() => setOpen(false)} aria-label="Close">
                <ChevronDown size={16} />
              </button>
            </div>
            <div className="alert-panel-list">
              {alerts.length === 0 ? (
                <div className="alert-empty">No alerts yet. Alerts will appear here when rules fire.</div>
              ) : (
                alerts.map((alert) => (
                  <motion.div
                    key={alert.id}
                    className={`alert-item ${alert.status}`}
                    initial={{ opacity: 0, x: -12 }}
                    animate={{ opacity: 1, x: 0 }}
                    transition={{ duration: 0.2 }}
                  >
                    <div className="alert-item-header">
                      <span className={`alert-status-dot ${alert.status}`} />
                      <span className="alert-item-name">{alert.alert_name}</span>
                      {alert.rule_id && isRuleId(alert.rule_id.toUpperCase() as RuleId) && (
                        <Badge rule={alert.rule_id.toUpperCase() as RuleId}>{alert.rule_id.toUpperCase()}</Badge>
                      )}
                      <span className="alert-time text-detail">{timeAgo(alert.received_at)}</span>
                      <button type="button" className="alert-dismiss" onClick={() => dismissAlert(alert.id)} aria-label="Dismiss">
                        <X size={13} />
                      </button>
                    </div>
                    {alert.summary && <p className="alert-item-summary">{alert.summary}</p>}
                    {alert.service && <span className="alert-item-service mono">{alert.service}</span>}
                  </motion.div>
                ))
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
