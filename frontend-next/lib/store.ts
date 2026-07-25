"use client";

import { create } from "zustand";
import { persist } from "zustand/middleware";
import { DEFAULT_GUARDIAN_QUESTION, PRIMARY_SERVICE, normalizeService, type ChatResponse } from "./api";
import type { RuleId } from "./rule-meta";

export type AuditHistoryEntry = {
  timestamp: number;
  service: string | null;
  fired_rule_ids: RuleId[];
  score: number;
};

export type ChatMessage = {
  id: string;
  role: "user" | "assistant";
  text: string;
  response?: ChatResponse;
};

type GuardianState = {
  selectedService: string;
  auditWindow: string;
  theme: "dark" | "light";
  auditHistory: AuditHistoryEntry[];
  chatMessages: ChatMessage[];
  chatDraft: string;
  setSelectedService: (service: string) => void;
  setAuditWindow: (window: string) => void;
  setTheme: (theme: "dark" | "light") => void;
  addAuditHistory: (entry: AuditHistoryEntry) => void;
  addChatMessage: (message: ChatMessage) => void;
  setChatDraft: (draft: string) => void;
  clearChat: () => void;
};

export const useGuardianStore = create<GuardianState>()(
  persist(
    (set) => ({
      selectedService: PRIMARY_SERVICE,
      auditWindow: "5m",
      theme: "dark",
      auditHistory: [],
      chatMessages: [],
      chatDraft: DEFAULT_GUARDIAN_QUESTION,
      setSelectedService: (service) => set({ selectedService: normalizeService(service) }),
      setAuditWindow: (window) => set({ auditWindow: window }),
      setTheme: (theme) => set({ theme }),
      addAuditHistory: (entry) =>
        set((state) => ({
          auditHistory: [
            ...state.auditHistory,
            { ...entry, service: normalizeService(entry.service) },
          ].slice(-40),
        })),
      addChatMessage: (message) =>
        set((state) => ({
          chatMessages: [...state.chatMessages, message].slice(-30),
        })),
      setChatDraft: (draft) => set({ chatDraft: draft }),
      clearChat: () => set({ chatMessages: [], chatDraft: DEFAULT_GUARDIAN_QUESTION }),
    }),
    {
      name: "guardian-frontend-state",
      partialize: (state) => ({
        selectedService: normalizeService(state.selectedService),
        auditWindow: state.auditWindow,
        theme: state.theme,
        auditHistory: state.auditHistory.map((entry) => ({
          ...entry,
          service: normalizeService(entry.service),
        })),
        chatMessages: state.chatMessages,
      }),
      merge: (persisted, current) => {
        const state = { ...current, ...(persisted as Partial<GuardianState>) };
        const auditHistory = state.auditHistory ?? [];
        return {
          ...state,
          selectedService: normalizeService(state.selectedService),
          auditHistory: auditHistory.map((entry) => ({
            ...entry,
            service: normalizeService(entry.service),
          })),
        };
      },
    },
  ),
);
