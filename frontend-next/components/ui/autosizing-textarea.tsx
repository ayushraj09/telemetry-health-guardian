"use client";

import { useLayoutEffect, useRef } from "react";
import type { TextareaHTMLAttributes } from "react";
import { clsx } from "clsx";

const MAX_VISIBLE_LINES = 2.5;
const MIN_HEIGHT_PX = 38;

type AutosizingTextareaProps = TextareaHTMLAttributes<HTMLTextAreaElement>;

function measureTextareaHeight(textarea: HTMLTextAreaElement) {
  const computed = window.getComputedStyle(textarea);
  const lineHeight = Number.parseFloat(computed.lineHeight) || Number.parseFloat(computed.fontSize) * 1.5 || 20;
  const paddingTop = Number.parseFloat(computed.paddingTop) || 0;
  const paddingBottom = Number.parseFloat(computed.paddingBottom) || 0;
  const borderTop = Number.parseFloat(computed.borderTopWidth) || 0;
  const borderBottom = Number.parseFloat(computed.borderBottomWidth) || 0;
  const maxHeight = lineHeight * MAX_VISIBLE_LINES + paddingTop + paddingBottom + borderTop + borderBottom;

  textarea.style.height = "auto";
  const nextHeight = Math.min(Math.max(textarea.scrollHeight, MIN_HEIGHT_PX), maxHeight);
  textarea.style.height = `${nextHeight}px`;
  textarea.style.overflowY = textarea.scrollHeight > maxHeight ? "auto" : "hidden";
}

export function AutosizingTextarea({ className, style, value, ...props }: AutosizingTextareaProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useLayoutEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;

    measureTextareaHeight(textarea);

    if (typeof ResizeObserver === "undefined") {
      return;
    }

    const observer = new ResizeObserver(() => {
      measureTextareaHeight(textarea);
    });
    observer.observe(textarea);
    return () => observer.disconnect();
  }, [value]);

  return (
    <textarea
      ref={textareaRef}
      className={clsx("input question-input", className)}
      rows={1}
      style={style}
      value={value}
      {...props}
    />
  );
}