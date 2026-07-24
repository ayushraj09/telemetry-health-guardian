"use client";

import { AUDIT_SERVICES, serviceLabel } from "@/lib/api";
import { useGuardianStore } from "@/lib/store";

export function Combobox() {
  const selectedService = useGuardianStore((state) => state.selectedService);
  const setSelectedService = useGuardianStore((state) => state.setSelectedService);

  return (
    <label className="field-label">
      Service
      <select className="select" value={selectedService} onChange={(event) => setSelectedService(event.target.value)}>
        {AUDIT_SERVICES.map((service) => (
          <option key={service} value={service}>
            {serviceLabel(service)}
          </option>
        ))}
      </select>
    </label>
  );
}
