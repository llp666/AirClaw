"use client";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Message } from "@/lib/types";
import { cn } from "@/lib/utils";
import ThoughtChain from "./ThoughtChain";
import RetrievalCard from "./RetrievalCard";
import AlertCard from "./AlertCard";

/** 单条消息气泡。 */
export default function ChatMessage({ message }: { message: Message }) {
  const isUser = message.role === "user";
  const streaming = message.streaming;

  return (
    <div className={cn("flex flex-col", isUser ? "items-end" : "items-stretch")}>
      <div className="mb-1 px-1 text-[10px] font-medium tracking-wide text-muted-foreground">
        {isUser ? "你" : "AirClaw"}
      </div>

      <div
        className={cn(
          "rounded-panel px-3.5 py-2.5",
          isUser
            ? "max-w-[78%] bg-accent text-accent-foreground"
            : "border border-border bg-white/70",
        )}
      >
        {!isUser && (
          <>
            {message.reasoning && <ReasoningBlock text={message.reasoning} />}
            {message.retrievals.map((r, i) => (
              <RetrievalCard key={i} retrieval={r} />
            ))}
            {message.warnings.map((w, i) => (
              <AlertCard key={i} warning={w} />
            ))}
            <ThoughtChain calls={message.toolCalls} />
          </>
        )}

        {isUser ? (
          <p className="whitespace-pre-wrap text-[13.5px] leading-relaxed">
            {message.content}
          </p>
        ) : (
          message.content && (
            <div className="prose-airclaw text-[13.5px]">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {message.content}
              </ReactMarkdown>
            </div>
          )
        )}

        {streaming && !message.content && (
          <span className="text-[12px] text-muted-foreground">
            {message.reasoning ? "思考中…" : "正在响应…"}
          </span>
        )}

        {streaming && message.content && (
          <span className="streaming-caret" />
        )}

        {message.error && (
          <p className="mt-2 text-[12px] text-danger">⚠ {message.error}</p>
        )}
      </div>
    </div>
  );
}

/**
 * 模型推理内容（agnes 等推理模型的 reasoning_content）。
 *
 * 默认折叠：推理过程通常较长且非最终答案，展开与否由用户决定。
 * 若模型的思考开关被关闭（LLM_ENABLE_THINKING=false），这里不会出现。
 */
function ReasoningBlock({ text }: { text: string }) {
  return (
    <details className="my-2 rounded-lg border border-border bg-black/[0.015]">
      <summary className="cursor-pointer list-none px-3 py-1.5 text-[11px] font-medium text-muted-foreground transition hover:bg-black/[0.03]">
        模型推理 · {text.length} 字符
      </summary>
      <p className="scroll-thin max-h-56 overflow-y-auto whitespace-pre-wrap border-t border-border px-3 py-2 text-[11.5px] leading-relaxed text-muted-foreground">
        {text}
      </p>
    </details>
  );
}
