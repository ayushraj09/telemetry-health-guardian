"use client";

import { useState } from "react";
import { motion } from "motion/react";
import { AlertTriangle, ChevronDown } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { ChatMessage as ChatMessageType } from "@/lib/store";
import type { CitedSegment } from "@/lib/api";
import { isRuleId, RULE_META, type RuleId } from "@/lib/rule-meta";
import { highlightRules } from "./rule-highlight";

// ---------------------------------------------------------------------------
// Client-side fallback: split raw prose into rule-based blocks when the
// backend doesn't return structured segments (LLM JSON parse failure, or
// old cached messages without the segments field).
// ---------------------------------------------------------------------------

type ParsedBlock = {
  ruleId: RuleId | null;
  text: string;
  evidence: string[];
};

const RULE_PATTERN = /\b(R[1-7])\b/;
const EVIDENCE_PATTERN = /\b([0-9a-f]{16,})\b/gi;
const SPAN_ID_PATTERN = /\b[0-9A-Fa-f]{16}\b/g;
const TRACE_ID_PATTERN = /\b[0-9A-Fa-f]{32}\b/g;

function parseProseIntoBlocks(text: string): ParsedBlock[] {
  // Split on sentence boundaries that mention a rule ID
  const sentences = text.split(/(?<=[.!?])\s+/);
  const blocks: ParsedBlock[] = [];
  let currentBlock: ParsedBlock = { ruleId: null, text: "", evidence: [] };

  for (const sentence of sentences) {
    const ruleMatch = sentence.match(RULE_PATTERN);
    const detectedRule = ruleMatch && isRuleId(ruleMatch[1]) ? (ruleMatch[1] as RuleId) : null;

    // Start a new block when we encounter a new rule ID
    if (detectedRule && detectedRule !== currentBlock.ruleId && currentBlock.text) {
      blocks.push(currentBlock);
      currentBlock = { ruleId: detectedRule, text: "", evidence: [] };
    } else if (detectedRule && !currentBlock.ruleId) {
      currentBlock.ruleId = detectedRule;
    }

    currentBlock.text += (currentBlock.text ? " " : "") + sentence;

    // Extract span/trace IDs as evidence
    const spanIds = sentence.match(SPAN_ID_PATTERN) ?? [];
    const traceIds = sentence.match(TRACE_ID_PATTERN) ?? [];
    for (const id of [...traceIds, ...spanIds]) {
      if (!currentBlock.evidence.includes(id)) {
        currentBlock.evidence.push(id);
      }
    }
  }

  if (currentBlock.text) {
    blocks.push(currentBlock);
  }

  return blocks;
}

// ---------------------------------------------------------------------------
// Segment card (structured backend response)
// ---------------------------------------------------------------------------

function SegmentCard({ segment, index }: { segment: CitedSegment; index: number }) {
  const [expanded, setExpanded] = useState(false);
  const ruleId = segment.rule_id.toUpperCase() as RuleId;
  const meta = isRuleId(ruleId) ? RULE_META[ruleId] : null;
  const color = meta?.color ?? "var(--accent-guardian)";

  return (
    <motion.div
      className="segment-card"
      style={{ "--segment-color": color } as React.CSSProperties}
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.06, duration: 0.2, ease: "easeOut" }}
    >
      <div className="segment-header">
        {isRuleId(ruleId) && <Badge rule={ruleId}>{ruleId}</Badge>}
        <span className="segment-label">{meta?.label ?? segment.rule_id}</span>
        {segment.severity_rank === 1 && (
          <span className="severity-chip">fix first</span>
        )}
      </div>

      <p className="segment-claim">{segment.claim}</p>

      {segment.evidence.length > 0 && (
        <div className="evidence-chips">
          {segment.evidence.map((ev, i) => (
            <span key={i} className="evidence-chip" title={ev}>
              &#9656; {ev}
            </span>
          ))}
        </div>
      )}

      {segment.detail && (
        <>
          <p className={`segment-detail ${!expanded ? "collapsed" : ""}`}>
            {segment.detail}
          </p>
          {segment.detail.length > 120 && (
            <button
              type="button"
              className="accordion-trigger"
              onClick={() => setExpanded(!expanded)}
              aria-expanded={expanded}
            >
              <ChevronDown
                size={14}
                style={{ transform: expanded ? "rotate(180deg)" : undefined, transition: "transform 150ms" }}
              />
              {expanded ? "Less" : "More"}
            </button>
          )}
        </>
      )}
    </motion.div>
  );
}

// ---------------------------------------------------------------------------
// Fallback block card (client-side parsed from prose)
// ---------------------------------------------------------------------------

function FallbackBlockCard({ block, index }: { block: ParsedBlock; index: number }) {
  const ruleId = block.ruleId;
  const meta = ruleId ? RULE_META[ruleId] : null;
  const color = meta?.color ?? "var(--accent-guardian)";

  // Strip evidence IDs from the display text to avoid inline hex noise
  let cleanText = block.text;
  for (const ev of block.evidence) {
    cleanText = cleanText.replace(new RegExp(ev.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "g"), "").replace(/\s{2,}/g, " ");
  }

  return (
    <motion.div
      className="segment-card"
      style={{ "--segment-color": color } as React.CSSProperties}
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.06, duration: 0.2, ease: "easeOut" }}
    >
      {ruleId && (
        <div className="segment-header">
          <Badge rule={ruleId}>{ruleId}</Badge>
          <span className="segment-label">{meta?.label ?? ruleId}</span>
        </div>
      )}

      <p className="segment-claim">{highlightRules(cleanText.trim())}</p>

      {block.evidence.length > 0 && (
        <div className="evidence-chips">
          {block.evidence.slice(0, 6).map((ev, i) => (
            <span key={i} className="evidence-chip" title={ev}>
              &#9656; {ev}
            </span>
          ))}
        </div>
      )}
    </motion.div>
  );
}

// ---------------------------------------------------------------------------
// Main ChatMessage component
// ---------------------------------------------------------------------------

export function ChatMessage({ message }: { message: ChatMessageType }) {
  const segments = message.response?.segments ?? [];
  const uncited = message.response?.rules_fired_but_uncited ?? [];
  const hasSegments = segments.length > 0;

  if (message.role === "user") {
    return (
      <div className="chat-message user">
        <strong>You</strong>
        <p className="text-body">{message.text}</p>
      </div>
    );
  }

  // Client-side fallback: parse prose into blocks when no structured segments
  const fallbackBlocks = !hasSegments ? parseProseIntoBlocks(message.text) : [];
  const useFallbackCards = fallbackBlocks.length > 1 || (fallbackBlocks.length === 1 && fallbackBlocks[0].ruleId);

  return (
    <div className="chat-message assistant">
      <strong>Guardian</strong>

      {hasSegments ? (
        <div className="segment-cards">
          {[...segments]
            .sort((a, b) => a.severity_rank - b.severity_rank)
            .map((segment, index) => (
              <SegmentCard key={`${segment.rule_id}-${index}`} segment={segment} index={index} />
            ))}
        </div>
      ) : useFallbackCards ? (
        <div className="segment-cards">
          {fallbackBlocks.map((block, index) => (
            <FallbackBlockCard key={index} block={block} index={index} />
          ))}
        </div>
      ) : (
        <p className="text-body">{highlightRules(message.text)}</p>
      )}

      {uncited.length > 0 && (
        <div className="uncited-chips">
          <AlertTriangle size={14} />
          <span>Also fired:</span>
          {uncited.map((ruleId) => (
            <Badge key={ruleId} rule={ruleId}>{ruleId}</Badge>
          ))}
        </div>
      )}
    </div>
  );
}
