"use client";

import { PanelLeftClose, PanelLeftOpen, PanelRightClose, PanelRightOpen, ShieldCheck } from "lucide-react";
import { useStore } from "@/lib/store";
import { cn } from "@/lib/utils";

/**
 * 顶部固定导航栏。
 * 左侧 AirClaw，右侧显示当前用户与保密警示语（PRD 第六章第 3 节）。
 * 两端的按钮分别收起 / 展开左右侧边栏，把中间对话区让出来。
 */
export default function Navbar() {
  const {
    ragMode,
    toggleRag,
    streamingSessions,
    user,
    sidebarCollapsed,
    inspectorCollapsed,
    toggleSidebar,
    toggleInspector,
  } = useStore();

  // 可能同时有好几个会话在生成，报总数而不是只报当前这条
  const busyCount = Object.keys(streamingSessions).length;

  return (
    <header className="glass-strong z-30 flex h-14 shrink-0 items-center justify-between border-b border-border px-3.5">
      <div className="flex items-center gap-2.5">
        <PanelToggle
          collapsed={sidebarCollapsed}
          onClick={toggleSidebar}
          label="会话栏"
          side="left"
        />
        <span className="text-[15px] font-semibold tracking-tight">AirClaw</span>
        <span className="rounded-full bg-accent-soft px-2 py-0.5 text-[10px] font-medium text-accent">
          本地原型
        </span>
        {busyCount > 0 && (
          <span className="text-[11px] text-muted-foreground">
            {busyCount > 1 ? `${busyCount} 个会话生成中…` : "生成中…"}
          </span>
        )}
      </div>

      <div className="flex items-center gap-4">
        <button
          onClick={toggleRag}
          title="开启后，Agent 在回答前先检索长期记忆；关闭则把 MEMORY.md 直接注入 System Prompt"
          className={
            "rounded-full px-3 py-1 text-[11px] font-medium transition " +
            (ragMode
              ? "bg-accent text-accent-foreground"
              : "bg-black/[0.05] text-muted hover:bg-black/[0.08]")
          }
        >
          RAG {ragMode ? "已开启" : "已关闭"}
        </button>

        <div className="flex items-center gap-1.5 text-[11px] text-warning">
          <ShieldCheck size={13} strokeWidth={2.2} />
          <span className="font-medium">本地部署 · 数据不出网</span>
        </div>

        <span className="text-[11px] text-muted-foreground">
          用户 <span className="font-medium text-foreground">{user || "—"}</span>
        </span>

        <PanelToggle
          collapsed={inspectorCollapsed}
          onClick={toggleInspector}
          label="检查器"
          side="right"
        />
      </div>
    </header>
  );
}

/** 侧边栏折叠开关。收起后按钮转为未激活样式，作为「当前是收起状态」的提示。 */
function PanelToggle({
  collapsed,
  onClick,
  label,
  side,
}: {
  collapsed: boolean;
  onClick: () => void;
  label: string;
  side: "left" | "right";
}) {
  const Icon =
    side === "left"
      ? collapsed
        ? PanelLeftOpen
        : PanelLeftClose
      : collapsed
        ? PanelRightOpen
        : PanelRightClose;

  return (
    <button
      onClick={onClick}
      title={`${collapsed ? "展开" : "收起"}${label}`}
      aria-label={`${collapsed ? "展开" : "收起"}${label}`}
      aria-pressed={!collapsed}
      className={cn(
        "shrink-0 rounded-md p-1.5 transition",
        collapsed
          ? "text-muted-foreground hover:bg-black/[0.06] hover:text-foreground"
          : "bg-black/[0.05] text-foreground hover:bg-black/[0.09]",
      )}
    >
      <Icon size={15} />
    </button>
  );
}
