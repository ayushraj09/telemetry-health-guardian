"use client";

import { Search } from "lucide-react";
import { useRouter } from "next/navigation";
import { AUDIT_SERVICES, serviceLabel } from "@/lib/api";

export function CommandPalette() {
  const router = useRouter();

  return (
    <div className="command-box">
      <Search size={15} />
      <select
        aria-label="Command palette"
        onChange={(event) => {
          if (event.target.value) {
            router.push(event.target.value);
            event.target.value = "";
          }
        }}
      >
        <option value="">Jump or inspect</option>
        <option value="/">Overview</option>
        <option value="/rules">Rule Explorer</option>
        <option value="/chat">Ask Guardian</option>
        <option value="/about">How it works</option>
        {AUDIT_SERVICES.map((service) => (
          <option key={service} value={`/service/${encodeURIComponent(service)}`}>
            Service: {serviceLabel(service)}
          </option>
        ))}
      </select>
    </div>
  );
}
