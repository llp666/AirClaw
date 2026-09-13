"use client";

/**
 * 原始消息视图。
 *
 * 对应 PRD 第六章 Sidebar 的 Raw Messages 入口，服务于「透明可控」这一核心定位：
 * 对话流（ChatPanel）把一次工具调用前后的多段助手消息合并成气泡展示，并叠加上
 * 思考链、检索卡片、告警卡片等运行态数据——好看，但已经不是落盘的东西了。
 * 这里按**存储原样**逐条列出会话文件里的内容：多段 assistant 各自独立、
 * tool_calls 的 input/output 原封不动，便于核对「到底存了什么」。
 *
 * 数据来自 GET /api/sessions/{id}/history，与切会话时还原消息用的是同一个接口，
 * 区别只在于这里不做任何转换。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { RefreshCw, Loader2, ChevronDown, ChevronRight, Wrench } from "lucide-react";
import * as api from "@/lib/api";
import { useStore } from "@/lib/store";
import { cn } from "@/lib/utils";

export default function RawMessages() {
  const { sessionId, sessions, streaming } = useStore();
  const [messages, setMessages] = useState<api.RawMessage[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** 展开 tool_calls 的 [消息序号, 调用序号] 集合 */
  const [open, setOpen] = useState<Set<string>>(new Set());
  const [showJson, setShowJson] = useState(false);

  const meta = useMemo(
    () => sessions.find((s) => s.id === sessionId) ?? null,
    [sessions, sessionId],
  );

  const load = useCallback(async () => {
    if (!sessionId) {
      setMessages(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      setMessages(await api.getHistory(sessionId));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [sessionId]);

  useEffect(() => {
    void load();
  }, [load]);

  const toggle = (key: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });

  if (!sessionId) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center">
        <p className="text-[12px] text-muted-foreground">
          未选择会话。在左侧选择一个会话，或直接发消息开一个新的。
        </p>
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* ---- 头部：会话元信息 + 刷新 ---- */}
      <div className="flex shrink-0 items-center gap-3 border-b border-border px-5 py-2.5">
        <div className="min-w-0 flex-1">
          <div className="truncate text-[12.5px] font-medium">
            {meta?.title ?? "会话"}
          </div>
          <div className="mt-0.5 font-mono text-[10px] text-muted-foreground">
            sessions/{sessionId}.json · 落盘 {messages?.length ?? 0} 条
            {meta ? ` · 更新于 ${formatTime(meta.updated_at)}` : ""}
          </div>
        </div>

        <button
          onClick={() => setShowJson((v) => !v)}
          className={cn(
            "rounded-md px-2 py-1 text-[11px] transition",
            showJson
              ? "bg-accent-soft text-accent"
              : "text-muted-foreground hover:bg-black/[0.05] hover:text-foreground",
          )}
          title="直接查看会话文件的 JSON 原文"
        >
          原始 JSON
        </button>

        <button
          onClick={() => void load()}
          disabled={loading}
          title="重新读取会话文件"
          className="flex items-center gap-1 rounded-md px-2 py-1 text-[11px] text-muted-foreground transition hover:bg-black/[0.05] hover:text-foreground disabled:opacity-40"
        >
          {loading ? (
            <Loader2 size={11} className="animate-spin" />
          ) : (
            <RefreshCw size={11} />
          )}
          刷新
        </button>
      </div>

      {streaming && (
        <p className="shrink-0 bg-warning-bg px-5 py-1.5 text-[11px] text-foreground/80">
          正在生成——本视图是会话文件的快照，刷新可看到本轮落盘后的结果。
        </p>
      )}

      {error && (
        <p className="shrink-0 bg-danger/10 px-5 py-1.5 text-[11px] text-danger">{error}</p>
      )}

      {/* ---- 内容 ---- */}
      <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-5 py-3">
        {showJson ? (
          <pre className="font-mono text-[11px] leading-relaxed text-foreground/90">
            {JSON.stringify(messages ?? [], null, 2)}
          </pre>
        ) : (
          <>
            {messages?.length === 0 && (
              <p className="py-8 text-center text-[12px] text-muted-foreground">
                会话文件里还没有消息。
              </p>
            )}

            <div className="space-y-2">
              {messages?.map((m, i) => (
                <div
                  key={i}
                  className={cn(
                    "rounded-lg border px-3 py-2",
                    m.role === "user"
                      ? "border-accent/25 bg-accent-soft/40"
                      : "border-border bg-white/60",
                  )}
                >
                  <div className="flex items-center gap-2 text-[10.5px]">
                    <span className="font-mono text-muted-foreground">
                      #{String(i).padStart(2, "0")}
                    </span>
                    <span
                      className={cn(
                        "rounded px-1.5 py-0.5 font-medium",
                        m.role === "user"
                          ? "bg-accent text-accent-foreground"
                          : "bg-black/[0.06] text-muted",
                      )}
                    >
                      {m.role}
                    </span>
                    {m.ts ? (
                      <span className="tabular-nums text-muted-foreground">
                        {formatTime(m.ts)}
                      </span>
                    ) : null}
                    <span className="flex-1" />
                    {m.tool_calls?.length ? (
                      <span className="flex items-center gap-1 text-muted-foreground">
                        <Wrench size={10} />
                        {m.tool_calls.length} 次工具调用
                      </span>
                    ) : null}
                  </div>

                  {m.content ? (
                    <div className="mt-1.5 whitespace-pre-wrap break-words text-[12px] leading-relaxed">
                      {m.content}
                    </div>
                  ) : (
                    <div className="mt-1.5 text-[11px] italic text-muted-foreground">
                      （本条 content 为空，仅承载工具调用）
                    </div>
                  )}

                  {m.tool_calls?.map((call, j) => {
                    const key = `${i}:${j}`;
                    const isOpen = open.has(key);
                    return (
                      <div key={key} className="mt-1.5 rounded-md border border-border">
                        <button
                          onClick={() => toggle(key)}
                          className="flex w-full items-center gap-1.5 px-2 py-1 text-left text-[11px] transition hover:bg-black/[0.03]"
                        >
                          {isOpen ? (
                            <ChevronDown size={11} className="shrink-0" />
                          ) : (
                            <ChevronRight size={11} className="shrink-0" />
                          )}
                          <span className="font-mono font-medium">{call.tool}</span>
                          <span className="min-w-0 flex-1 truncate text-muted-foreground">
                            {summarize(call.input)}
                          </span>
                        </button>

                        {isOpen && (
                          <div className="border-t border-border px-2 py-1.5">
                            <div className="text-[10px] text-muted-foreground">input</div>
                            <pre className="scroll-thin max-h-56 overflow-auto font-mono text-[10.5px] leading-relaxed">
                              {typeof call.input === "string"
                                ? call.input
                                : JSON.stringify(call.input, null, 2)}
                            </pre>
                            <div className="mt-2 text-[10px] text-muted-foreground">
                              output
                            </div>
                            <pre className="scroll-thin max-h-56 overflow-auto whitespace-pre-wrap break-words font-mono text-[10.5px] leading-relaxed">
                              {call.output || "（空）"}
                            </pre>
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

/** 折叠态的一行摘要：优先取命令/路径这类最有信息量的字段 */
function summarize(input: unknown): string {
  if (typeof input === "string") return input;
  if (input && typeof input === "object") {
    const o = input as Record<string, unknown>;
    for (const k of ["command", "file_path", "path", "query", "code", "out_dir"]) {
      const v = o[k];
      if (typeof v === "string" && v) return v;
    }
  }
  return JSON.stringify(input ?? {});
}

function formatTime(ts: number): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return `${d.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" })} ${d.toLocaleTimeString(
    "zh-CN",
    { hour: "2-digit", minute: "2-digit" },
  )}`;
}
