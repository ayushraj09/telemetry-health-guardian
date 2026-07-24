import type { AuditHistoryEntry } from "@/lib/store";

export function ScoreSparkline({ history }: { history: AuditHistoryEntry[] }) {
  const values = history.slice(-12).map((entry) => entry.score);
  if (values.length < 2) {
    return <div className="sparkline-empty" />;
  }
  const points = values
    .map((value, index) => {
      const x = (index / (values.length - 1)) * 100;
      const y = 34 - (Math.max(0, Math.min(100, value)) / 100) * 30;
      return `${x},${y}`;
    })
    .join(" ");
  return (
    <svg className="sparkline" viewBox="0 0 100 36" preserveAspectRatio="none" aria-hidden>
      <polyline points={points} />
    </svg>
  );
}
