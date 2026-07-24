"use client";

import { ReactNode, useState } from "react";
import { ChevronDown } from "lucide-react";

export function Accordion({ title, children }: { title: ReactNode; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="accordion">
      <button className="accordion-trigger" onClick={() => setOpen((value) => !value)} type="button">
        <span>{title}</span>
        <ChevronDown aria-hidden size={16} />
      </button>
      {open ? <div className="accordion-content">{children}</div> : null}
    </div>
  );
}
