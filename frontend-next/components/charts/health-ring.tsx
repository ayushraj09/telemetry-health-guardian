"use client";

import { useEffect, useRef, useState } from "react";
import { motion, useMotionValue, useTransform, animate } from "motion/react";
import NumberFlow from "@number-flow/react";
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
  const targetOffset = circumference - (clamped / 100) * circumference;

  // Animate strokeDashoffset with Motion
  const offsetMV = useMotionValue(circumference);

  useEffect(() => {
    animate(offsetMV, targetOffset, { duration: 0.6, ease: "easeOut" });
  }, [targetOffset, offsetMV]);

  // Use transform to read the motion value as a string for the SVG attribute
  const strokeDashoffset = useTransform(offsetMV, (v) => v);

  return (
    <div className="health-ring" style={{ width: size, height: size }}>
      <svg aria-hidden viewBox="0 0 120 120">
        <circle className="ring-track" cx="60" cy="60" r={radius} />
        <motion.circle
          className="ring-value"
          cx="60"
          cy="60"
          r={radius}
          stroke={scoreColor(clamped)}
          strokeDasharray={circumference}
          style={{ strokeDashoffset }}
          fill="none"
          strokeLinecap="round"
          strokeWidth={10}
        />
      </svg>
      <span className="ring-score mono">
        <NumberFlow value={Math.round(clamped)} />
      </span>
      <span className="ring-label">{label}</span>
    </div>
  );
}

export function ScoreDelta({ current, previous }: { current: number; previous?: number }) {
  const [flash, setFlash] = useState(false);
  const prevRef = useRef(previous);

  useEffect(() => {
    if (prevRef.current !== undefined && prevRef.current !== current) {
      setFlash(true);
      const timer = setTimeout(() => setFlash(false), 800);
      return () => clearTimeout(timer);
    }
    prevRef.current = current;
  }, [current]);

  if (previous === undefined) {
    return <span className="delta neutral mono">new</span>;
  }

  const delta = Math.round(current - previous);
  const isNegative = delta < 0;

  return (
    <motion.span
      className={clsx("delta mono", isNegative ? "negative" : "positive")}
      initial={flash ? { scale: 0.9, backgroundColor: isNegative ? "rgba(242,84,91,0.2)" : "rgba(51,209,122,0.2)" } : false}
      animate={flash ? { scale: 1, backgroundColor: "rgba(0,0,0,0)" } : undefined}
      transition={{ duration: 0.8, ease: "easeOut" }}
    >
      {delta >= 0 ? "+" : ""}
      <NumberFlow value={delta} />
    </motion.span>
  );
}
