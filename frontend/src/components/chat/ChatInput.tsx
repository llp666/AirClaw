"use client";

import { useEffect, useRef } from "react";
import { ArrowUp, Square } from "lucide-react";
import { useStore } from "@/lib/store";

/** 输入框。Enter 发送，Shift+Enter 换行；流式期间可中断。 */
export default function ChatInput() {
  const { sendMessage, streaming, stopStreaming, draft, setDraft } = useStore();
  const ref = useRef<HTMLTextAreaElement>(null);

  // 输入框随内容高度自适应，上限 200px 后转为内部滚动
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [draft]);

  const submit = () => {
    const value = draft.trim();
    if (!value || streaming) return;
    setDraft("");
    void sendMessage(value);
  };

  return (
    <div className="border-t border-border bg-white/50 px-4 py-3">
      <div className="mx-auto flex max-w-4xl items-end gap-2 rounded-panel border border-border bg-white/80 p-2 transition focus-within:border-accent/40 focus-within:ring-2 focus-within:ring-accent/10">
        <textarea
          ref={ref}
          rows={1}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
          placeholder="输入问题，或让 Agent 读文件、跑测试、查知识库…（Enter 发送，Shift+Enter 换行）"
          className="scroll-thin min-h-[24px] flex-1 resize-none bg-transparent px-1.5 py-1 text-[13.5px] leading-relaxed outline-none placeholder:text-muted-foreground"
        />

        {streaming ? (
          <button
            onClick={() => stopStreaming()}
            title="中断生成"
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-black/[0.06] text-foreground transition hover:bg-black/[0.1]"
          >
            <Square size={13} fill="currentColor" />
          </button>
        ) : (
          <button
            onClick={submit}
            disabled={!draft.trim()}
            title="发送"
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-accent text-accent-foreground transition hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-35"
          >
            <ArrowUp size={15} strokeWidth={2.5} />
          </button>
        )}
      </div>
    </div>
  );
}
