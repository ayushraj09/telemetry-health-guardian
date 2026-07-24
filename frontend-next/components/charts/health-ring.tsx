import { clsx } from "clsx";

function scoreColor(score: number) {
  if (score >= 85) {
    return "var(--signal-healthy)";
  }
  if (score >= 60) {
    return "var(--signal-watch)";
  }
  return "var(--signal-critical)";
}

export function HealthRing({ score, size = 132, label = "Health Score" }: { score: number; size?: number; label?: string }) {
  const radius = 46;
  const circumference = 2 * Math.PI * radius;
  const clamped = Math.max(0, Math.min(100, score));
  const offset = circumference - (clamped / 100) * circumference;

  return (
    <div className="health-ring" style={{ width: size, height: size }}>
      <svg aria-hidden viewBox="0 0 120 120">
        <circle className="ring-track" cx="60" cy="60" r={radius} />
        <circle
          className="ring-value"
          cx="60"
          cy="60"
          r={radius}
          stroke={scoreColor(clamped)}
          strokeDasharray={circumference}
          strokeDashoffset={offset}
        />
      </svg>
      <span className="ring-score mono">{Math.round(clamped)}</span>
      <span className="ring-label">{label}</span>
    </div>
  );
}

export function ScoreDelta({ current, previous }: { current: number; previous?: number }) {
  if (previous === undefined) {
    return <span className="delta neutral mono">new</span>;
  }
  const delta = Math.round(current - previous);
  return <span className={clsx("delta mono", delta >= 0 ? "positive" : "negative")}>{delta >= 0 ? "+" : ""}{delta}</span>;
}
