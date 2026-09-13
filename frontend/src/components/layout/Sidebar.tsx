"use client";

import { useEffect, useState } from "react";
import { MessageSquarePlus, Trash2, Wrench, Loader2, Braces } from "lucide-react";
import * as api from "@/lib/api";
import { useStore } from "@/lib/store";
import { cn } from "@/lib/utils";

/** 左侧边栏：会话列表、压缩入口、Token 统计。 */
export default function Sidebar() {
  const {
    sessions,
    sessionId,
    messages,
    streaming,
    streamingSessions,
    unseenReplies,
    centerView,
    setCenterView,
    newSession,
    switchSession,
    removeSession,
    compress,
  } = useStore();

  const [tokens, setTokens] = useState<api.TokenStats | null>(null);
  const [compressing, setCompressing] = useState(false);

  // 会话切换或流式结束后刷新 Token 统计
  useEffect(() => {
    if (!sessionId) {
      setTokens(null);
      return;
    }
    let cancelled = false;
    api
      .getSessionTokens(sessionId)
      .then((t) => !cancelled && setTokens(t))
      .catch(() => !cancelled && setTokens(null));
    return () => {
      cancelled = true;
    };
  }, [sessionId, messages.length, streaming]);

  const handleCompress = async () => {
    if (compressing) return;
    setCompressing(true);
    try {
      await compress();
    } finally {
      setCompressing(false);
    }
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center justify-between px-4 pt-4 pb-2">
        <span className="text-[11px] font-medium tracking-wide text-muted-foreground">
          会话
        </span>
        <button
          onClick={() => void newSession()}
          title="新建会话"
          className="rounded-md p-1 text-muted-foreground transition hover:bg-black/[0.06] hover:text-foreground"
        >
          <MessageSquarePlus size={15} />
        </button>
      </div>

      <nav className="scroll-thin min-h-0 flex-1 overflow-y-auto px-2">
        {sessions.length === 0 && (
          <p className="px-2 py-3 text-[12px] leading-relaxed text-muted-foreground">
            暂无历史会话。
            <br />
            直接在右侧输入框中提问即可开始。
          </p>
        )}

        {sessions.map((s) => (
          <div
            key={s.id}
            className={cn(
              "group mb-0.5 flex items-center gap-1 rounded-lg px-2.5 py-2 transition",
              s.id === sessionId
                ? "bg-accent-soft text-accent"
                : "text-muted hover:bg-black/[0.04]",
            )}
          >
            <button
              onClick={() => void switchSession(s.id)}
              className="min-w-0 flex-1 text-left"
            >
              <div className="truncate text-[13px] font-medium">{s.title}</div>
              <div className="mt-0.5 text-[10px] text-muted-foreground">
                {s.message_count} 条 · {formatTime(s.updated_at)}
              </div>
            </button>

            {/* 各会话可同时提问，所以状态得逐个显示：转圈＝正在生成，圆点＝
                在别处生成完了还没看 */}
            {streamingSessions[s.id] ? (
              <Loader2 size={12} className="shrink-0 animate-spin text-accent" />
            ) : unseenReplies[s.id] ? (
              <span
                title="回复已完成"
                className="h-1.5 w-1.5 shrink-0 rounded-full bg-accent"
              />
            ) : null}

            <button
              onClick={() => void removeSession(s.id)}
              title="删除会话"
              className="shrink-0 rounded p-1 text-muted-foreground opacity-0 transition group-hover:opacity-100 hover:text-danger"
            >
              <Trash2 size={13} />
            </button>
          </div>
        ))}
      </nav>

      <div className="border-t border-border px-4 py-3">
        <button
          onClick={() => setCenterView(centerView === "raw" ? "chat" : "raw")}
          disabled={!sessionId}
          title="按存储原样查看会话文件里的消息：多段助手消息各自独立、工具调用的输入输出原封不动"
          className={cn(
            "mb-3 flex w-full items-center justify-center gap-1.5 rounded-lg py-1.5 text-[11px] font-medium transition disabled:cursor-not-allowed disabled:opacity-40",
            centerView === "raw"
              ? "bg-accent-soft text-accent"
              : "bg-black/[0.04] text-muted hover:bg-black/[0.07]",
          )}
        >
          <Braces size={12} />
          {centerView === "raw" ? "返回对话" : "原始消息"}
        </button>

        <button
          onClick={() => void handleCompress()}
          disabled={!sessionId || compressing}
          title="把前 50% 的历史消息归档为摘要，释放上下文空间"
          className="mb-3 flex w-full items-center justify-center gap-1.5 rounded-lg bg-black/[0.04] py-1.5 text-[11px] font-medium text-muted transition hover:bg-black/[0.07] disabled:cursor-not-allowed disabled:opacity-40"
        >
          {compressing ? (
            <Loader2 size={12} className="animate-spin" />
          ) : (
            <Wrench size={12} />
          )}
          压缩对话历史
        </button>

        <div className="space-y-1 text-[10px] text-muted-foreground">
          <div className="flex justify-between">
            <span>System Prompt</span>
            <span className="tabular-nums">
              {tokens ? tokens.system_tokens.toLocaleString() : "—"}
            </span>
          </div>
          <div className="flex justify-between">
            <span>对话消息</span>
            <span className="tabular-nums">
              {tokens ? tokens.message_tokens.toLocaleString() : "—"}
            </span>
          </div>
          {tokens && tokens.summary_tokens > 0 && (
            <div className="flex justify-between">
              <span>压缩摘要</span>
              <span className="tabular-nums">
                {tokens.summary_tokens.toLocaleString()}
              </span>
            </div>
          )}
          <div className="flex justify-between border-t border-border pt-1 font-medium text-muted">
            <span>合计 tokens</span>
            <span className="tabular-nums">
              {tokens ? tokens.total_tokens.toLocaleString() : "—"}
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

function formatTime(ts: number): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  return sameDay
    ? d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })
    : d.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}
