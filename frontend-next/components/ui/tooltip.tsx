import { ReactNode } from "react";

export function Tooltip({ label, children }: { label: string; children: ReactNode }) {
  return (
    <span className="tooltip" data-tooltip={label}>
      {children}
    </span>
  );
}
