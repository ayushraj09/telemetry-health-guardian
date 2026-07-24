"use client";

import { createContext, ReactNode, useContext, useMemo, useState } from "react";
import { X } from "lucide-react";

type Toast = { id: string; title: string; detail?: string; tone?: "ok" | "error" };
type ToastContextValue = { push: (toast: Omit<Toast, "id">) => void };

const ToastContext = createContext<ToastContextValue | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const value = useMemo(
    () => ({
      push: (toast: Omit<Toast, "id">) => {
        const id = crypto.randomUUID();
        setToasts((items) => [...items, { ...toast, id }].slice(-3));
        window.setTimeout(() => setToasts((items) => items.filter((item) => item.id !== id)), 5200);
      },
    }),
    [],
  );

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toast-stack">
        {toasts.map((toast) => (
          <div className={`toast ${toast.tone ?? "ok"}`} key={toast.id}>
            <strong>{toast.title}</strong>
            {toast.detail ? <span>{toast.detail}</span> : null}
            <button aria-label="Dismiss" onClick={() => setToasts((items) => items.filter((item) => item.id !== toast.id))}>
              <X size={14} />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const context = useContext(ToastContext);
  if (!context) {
    throw new Error("useToast must be used inside ToastProvider");
  }
  return context;
}
