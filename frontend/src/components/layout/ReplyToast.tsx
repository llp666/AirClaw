"use client";

import { useEffect } from "react";
import { CheckCircle2, X } from "lucide-react";
import { useStore } from "@/lib/store";

/**
 * 右下角的「回复完成」提示。
 *
 * 生成结束时人不在那条会话里，界面就没有任何反馈——切回去才知道答完了，
 * 或者一直以为它还在跑。这里给一条可点击跳转的提示（即 ChatGPT 在别的对话
 * 答完时的做法），8 秒后自动收起；侧栏对应条目另留一个圆点，提示不会丢。
 *
 * 同一时刻只弹最近完成的那条，同时答完多个时不至于在右下角摞成一叠。
 */
export default function ReplyToast() {
  const { unseenReplies, sessions, switchSession, dismissUnseen } = useStore();

  const latest = Object.entries(unseenReplies).sort((a, b) => b[1] - a[1])[0];
  const id = latest?.[0];
  const title = id
    ? (sessions.find((s) => s.id === id)?.title ?? "会话")
    : "";

  useEffect(() => {
    if (!id) return;
    const timer = window.setTimeout(() => dismissUnseen(id), 8000);
    return () => window.clearTimeout(timer);
  }, [id, dismissUnseen]);

  if (!id) return null;

  return (
    <div className="glass-strong fixed bottom-5 right-5 z-40 flex max-w-xs items-center gap-2.5 rounded-panel border border-border py-2.5 pl-3.5 pr-2 shadow-lg">
      <CheckCircle2 size={15} className="shrink-0 text-success" />

      <div className="min-w-0 flex-1">
        <div className="truncate text-[11.5px] font-medium">{title}</div>
        <div className="text-[10.5px] text-muted-foreground">回复已完成</div>
      </div>

      <button
        onClick={() => void switchSession(id)}
        className="shrink-0 rounded-md bg-accent-soft px-2 py-1 text-[11px] font-medium text-accent transition hover:bg-accent/15"
      >
        查看
      </button>

      <button
        onClick={() => dismissUnseen(id)}
        title="关闭提示"
        className="shrink-0 rounded p-1 text-muted-foreground transition hover:bg-black/[0.06] hover:text-foreground"
      >
        <X size={13} />
      </button>
    </div>
  );
}
