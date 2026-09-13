"use client";

import { useEffect, useRef } from "react";
import { Sparkles } from "lucide-react";
import { useStore } from "@/lib/store";
import type { Message } from "@/lib/types";
import { cn } from "@/lib/utils";
import ChatMessage from "./ChatMessage";
import ChatInput from "./ChatInput";

/** 中间对话区：消息流 + 输入框。 */
export default function ChatPanel() {
  const { messages, sendMessage } = useStore();
  const bottomRef = useRef<HTMLDivElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  const turns = mergeTurns(messages);

  // 流式期间保持贴底；用户手动上滚时不打断阅读
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
    if (nearBottom) bottomRef.current?.scrollIntoView({ block: "end" });
  }, [messages]);

  const suggestions = [
    "读取 backend/config.py，说明里面定义了哪些配置项",
    "查一下知识库里壳体材料的屈服强度要求，引用原文",
    "用 terminal 看看 /workspace/src 下的目录结构",
  ];

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div ref={scrollRef} className="scroll-thin min-h-0 flex-1 overflow-y-auto">
        {messages.length === 0 ? (
          <EmptyState suggestions={suggestions} onPick={sendMessage} />
        ) : (
          <div className="mx-auto flex max-w-4xl flex-col gap-4 px-5 py-5">
            {turns.map((m) => (
              <ChatMessage key={m.id} message={m} />
            ))}
            <TurnFooter turn={turns[turns.length - 1]} />
            <div ref={bottomRef} />
          </div>
        )}
      </div>

      <ChatInput />
    </div>
  );
}

/**
 * 对话末尾的状态行。
 *
 * 这是历史视图里**唯一**能判断「本轮回复是完整的」的线索：光看气泡本身，
 * 一段被中断的回复和一段答完的回复长得一模一样。实时生成中不显示，
 * 光标已经在提示了。
 */
function TurnFooter({ turn }: { turn: Message | undefined }) {
  if (!turn || turn.streaming) return null;

  const label = turn.error
    ? "回复中断"
    : turn.role === "assistant"
      ? `回复完成${turn.ts ? ` · ${formatClock(turn.ts)}` : ""}`
      : "这条提问还没有收到回复";

  return (
    <div className="flex items-center gap-2.5 text-[10.5px] text-muted-foreground">
      <span className="h-px flex-1 bg-border" />
      <span>{label}</span>
      <span className="h-px flex-1 bg-border" />
    </div>
  );
}

function formatClock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * 一次提问 → 一个回答。
 *
 * 后端按段落盘：模型在工具调用前后各生成一段文本，会话文件里因此是多条连续的
 * assistant 消息（见 graph/session_manager.py）。若照着渲染，界面上一次提问会
 * 裂成好几段回复。这里把连续的 assistant 消息合并成一个气泡，落盘的分段结构
 * 仍可在「原始消息」视图里逐条核对。
 */
function mergeTurns(messages: Message[]): Message[] {
  const out: Message[] = [];
  for (const m of messages) {
    const prev = out[out.length - 1];
    if (m.role === "assistant" && prev?.role === "assistant") {
      out[out.length - 1] = {
        ...prev,
        content: join(prev.content, m.content),
        reasoning: join(prev.reasoning, m.reasoning),
        toolCalls: [...prev.toolCalls, ...m.toolCalls],
        retrievals: [...prev.retrievals, ...m.retrievals],
        warnings: [...prev.warnings, ...m.warnings],
        // 取最后一段的状态：结束/中断都只标记在最后一段上
        streaming: m.streaming,
        error: prev.error ?? m.error,
        ts: m.ts ?? prev.ts,
      };
      continue;
    }
    out.push(m);
  }
  return out;
}

/** 拼接两段文本，空段不留多余空行。 */
function join(a: string, b: string): string {
  if (!a) return b;
  if (!b) return a;
  return `${a}\n\n${b}`;
}

/**
 * 空状态：首次进入时给几条示例问句。
 *
 * 示例默认只露出第一条，其余收在下面——几张等宽卡片堆在中栏会喧宾夺主，
 * 鼠标移入再展开。文字一律居中：卡片是整行宽的，左对齐会在右侧拖出一截空白。
 */
function EmptyState({
  suggestions,
  onPick,
}: {
  suggestions: string[];
  onPick: (text: string) => void;
}) {
  const [first, ...rest] = suggestions;

  return (
    <div className="flex h-full flex-col items-center justify-center px-6">
      <div className="mb-3 flex h-11 w-11 items-center justify-center rounded-2xl bg-accent-soft">
        <Sparkles size={20} className="text-accent" />
      </div>
      <h1 className="text-[17px] font-semibold tracking-tight">AirClaw</h1>
      <p className="mt-1 max-w-md text-center text-[12.5px] leading-relaxed text-muted-foreground">
        本地科研办公助手。文件即记忆，技能即插件，所有工具调用全程审计。
      </p>

      <div className="group mt-6 w-full max-w-lg">
        <div className="relative">
          <Suggestion text={first} onPick={onPick} />

          {/* 牌堆的下一张，暗示下面还有内容；展开时让位给真正的卡片 */}
          {rest.length > 0 && (
            <div className="pointer-events-none absolute inset-x-3 top-full mt-1 h-2.5 rounded-b-xl border border-t-0 border-border bg-white/60 transition-opacity duration-300 group-hover:opacity-0" />
          )}
        </div>

        {/* 0fr → 1fr：不必写死高度也能得到平滑的展开动画 */}
        <div className="grid grid-rows-[0fr] transition-[grid-template-rows] duration-500 ease-out group-hover:grid-rows-[1fr] group-focus-within:grid-rows-[1fr]">
          <div className="overflow-hidden">
            {rest.map((s, i) => (
              <div
                key={s}
                className={cn(
                  "translate-y-1 pt-1.5 opacity-0 transition-all duration-300 ease-out group-hover:translate-y-0 group-hover:opacity-100 group-focus-within:translate-y-0 group-focus-within:opacity-100",
                  i === 0 ? "delay-75" : "delay-150",
                )}
              >
                <Suggestion text={s} onPick={onPick} />
              </div>
            ))}
          </div>
        </div>

        {rest.length > 0 && (
          <p className="mt-3.5 text-center text-[10.5px] text-muted-foreground transition-opacity duration-300 group-hover:opacity-0">
            鼠标移入展开其余 {rest.length} 条示例
          </p>
        )}
      </div>

      <p className="mt-6 text-[11px] text-muted-foreground">
        所有代码执行均在无网络 Docker 沙箱内完成 · 敏感内容由审计钩子实时拦截
      </p>
    </div>
  );
}

function Suggestion({
  text,
  onPick,
}: {
  text: string;
  onPick: (text: string) => void;
}) {
  return (
    <button
      onClick={() => onPick(text)}
      className="block w-full rounded-lg border border-border bg-white/60 px-4 py-2.5 text-center text-[12.5px] text-muted transition hover:border-accent/30 hover:bg-accent-soft hover:text-accent"
    >
      {text}
    </button>
  );
}
