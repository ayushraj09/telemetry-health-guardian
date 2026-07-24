"use client";

import { ReactNode, useState } from "react";

export function Tabs<T extends string>({
  tabs,
  initial,
  children,
}: {
  tabs: { id: T; label: string; count?: number }[];
  initial: T;
  children: (active: T) => ReactNode;
}) {
  const [active, setActive] = useState<T>(initial);

  return (
    <div className="tabs">
      <div className="tab-list" role="tablist">
        {tabs.map((tab) => (
          <button
            className={tab.id === active ? "tab active" : "tab"}
            key={tab.id}
            onClick={() => setActive(tab.id)}
            type="button"
          >
            {tab.label}
            {typeof tab.count === "number" ? <span className="tab-count mono">{tab.count}</span> : null}
          </button>
        ))}
      </div>
      {children(active)}
    </div>
  );
}
