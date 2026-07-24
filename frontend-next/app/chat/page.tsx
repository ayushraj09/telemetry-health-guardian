"use client";

import { FormEvent } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Send } from "lucide-react";
import { ChatMessage } from "@/components/product/chat-message";
import { RecentFindingsRail } from "@/components/product/recent-findings-rail";
import { SuggestedQuestionChips } from "@/components/product/suggested-question-chips";
import { AuditRunButton } from "@/components/product/audit-run-button";
import { Button } from "@/components/ui/button";
import { askGuardian, getAuditReport } from "@/lib/api";
import { useGuardianStore } from "@/lib/store";

export default function ChatPage() {
  const selectedService = useGuardianStore((state) => state.selectedService);
  const messages = useGuardianStore((state) => state.chatMessages);
  const draft = useGuardianStore((state) => state.chatDraft);
  const setDraft = useGuardianStore((state) => state.setChatDraft);
  const addMessage = useGuardianStore((state) => state.addChatMessage);
  const report = useQuery({ queryKey: ["audit", selectedService], queryFn: () => getAuditReport(selectedService), retry: 0 });
  const mutation = useMutation({
    mutationFn: (question: string) => askGuardian(question, selectedService),
    onSuccess: (response) => {
      addMessage({ id: crypto.randomUUID(), role: "assistant", text: response.answer, response });
    },
  });

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const question = draft.trim();
    if (!question) {
      return;
    }
    addMessage({ id: crypto.randomUUID(), role: "user", text: question });
    setDraft("");
    mutation.mutate(question);
  }

  return (
    <div className="content-stack">
      <header className="page-header">
        <div>
          <p className="eyebrow">Ask Guardian</p>
          <h1>Chat over cited telemetry findings.</h1>
        </div>
        <AuditRunButton service={selectedService} />
      </header>
      <RecentFindingsRail />
      <section className="chat-thread panel">
        {messages.length ? messages.map((message) => <ChatMessage key={message.id} message={message} />) : <div className="empty-state">Ask why a rule fired, what changed, or which telemetry defect to fix first.</div>}
        {mutation.isPending ? <div className="chat-message assistant scan"><strong>Guardian</strong><p>Reading the current audit report...</p></div> : null}
        {mutation.isError ? <div className="callout error">Chat failed: {mutation.error instanceof Error ? mutation.error.message : "unknown error"}</div> : null}
      </section>
      <SuggestedQuestionChips cycle={report.data} />
      <form className="chat-input panel" onSubmit={submit}>
        <input onChange={(event) => setDraft(event.target.value)} placeholder="Why did the score drop?" value={draft} />
        <Button disabled={mutation.isPending} type="submit" variant="primary"><Send size={16} /> Send</Button>
      </form>
    </div>
  );
}
