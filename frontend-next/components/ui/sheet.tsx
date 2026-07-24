"use client";

import { ReactNode } from "react";
import { X } from "lucide-react";

export function Sheet({
  open,
  title,
  children,
  onClose,
}: {
  open: boolean;
  title: string;
  children: ReactNode;
  onClose: () => void;
}) {
  if (!open) {
    return null;
  }
  return (
    <div className="sheet-backdrop">
      <aside className="sheet panel">
        <div className="sheet-header">
          <h2>{title}</h2>
          <button aria-label="Close" onClick={onClose} type="button">
            <X size={18} />
          </button>
        </div>
        {children}
      </aside>
    </div>
  );
}
