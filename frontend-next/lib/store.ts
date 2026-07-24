"use client";

import { create } from "zustand";
import { persist } from "zustand/middleware";
import type { ChatResponse } from "./api";
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
  theme: "dark" | "light";
  auditHistory: AuditHistoryEntry[];
  chatMessages: ChatMessage[];
  chatDraft: string;
  setSelectedService: (service: string) => void;
  setTheme: (theme: "dark" | "light") => void;
  addAuditHistory: (entry: AuditHistoryEntry) => void;
  addChatMessage: (message: ChatMessage) => void;
  setChatDraft: (draft: string) => void;
};

export const useGuardianStore = create<GuardianState>()(
  persist(
    (set) => ({
      selectedService: "_all_",
      theme: "dark",
      auditHistory: [],
      chatMessages: [],
      chatDraft: "",
      setSelectedService: (service) => set({ selectedService: service }),
      setTheme: (theme) => set({ theme }),
      addAuditHistory: (entry) =>
        set((state) => ({
          auditHistory: [...state.auditHistory, entry].slice(-40),
        })),
      addChatMessage: (message) =>
        set((state) => ({
          chatMessages: [...state.chatMessages, message].slice(-30),
        })),
      setChatDraft: (draft) => set({ chatDraft: draft }),
    }),
    {
      name: "guardian-frontend-state",
      partialize: (state) => ({
        selectedService: state.selectedService,
        theme: state.theme,
        auditHistory: state.auditHistory,
        chatMessages: state.chatMessages,
      }),
    },
  ),
);
