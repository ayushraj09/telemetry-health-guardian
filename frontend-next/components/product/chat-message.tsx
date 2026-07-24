import { AlertTriangle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { ChatMessage as ChatMessageType } from "@/lib/store";
import { highlightRules } from "./rule-highlight";
import { ProbableFixPanel } from "./probable-fix-panel";

export function ChatMessage({ message }: { message: ChatMessageType }) {
  return (
    <div className={`chat-message ${message.role}`}>
      <strong>{message.role === "assistant" ? "Guardian" : "You"}</strong>
      <p>{highlightRules(message.text)}</p>
      {message.response?.rules_fired_but_uncited.length ? (
        <div className="uncited-row">
          <AlertTriangle size={15} />
          {message.response.rules_fired_but_uncited.map((ruleId) => (
            <Badge key={ruleId} rule={ruleId}>{ruleId} uncited</Badge>
          ))}
        </div>
      ) : null}
      {message.response ? <ProbableFixPanel fixes={message.response.probable_fixes} /> : null}
    </div>
  );
}
