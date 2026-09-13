"use client";

import Navbar from "@/components/layout/Navbar";
import Sidebar from "@/components/layout/Sidebar";
import ResizeHandle from "@/components/layout/ResizeHandle";
import ReplyToast from "@/components/layout/ReplyToast";
import ChatPanel from "@/components/chat/ChatPanel";
import RawMessages from "@/components/chat/RawMessages";
import InspectorPanel from "@/components/editor/InspectorPanel";
import { useStore } from "@/lib/store";

/** 三栏 IDE 风格主界面。对应 PRD 第六章第 1 节。 */
export default function Home() {
  const {
    sidebarWidth,
    inspectorWidth,
    sidebarCollapsed,
    inspectorCollapsed,
    setSidebarWidth,
    setInspectorWidth,
    centerView,
    error,
    clearError,
  } = useStore();

  return (
    <div className="flex h-screen flex-col overflow-hidden bg-background">
      <Navbar />

      <div className="flex min-h-0 flex-1">
        {/* 收起时宽度归零。内层保持原宽度不重排，动画期间内容是「被推出去」
            而不是被压扁；overflow-hidden 负责裁掉露出的部分。 */}
        <aside
          style={{ width: sidebarCollapsed ? 0 : sidebarWidth }}
          className="glass flex shrink-0 flex-col overflow-hidden border-r border-border transition-[width] duration-300 ease-out"
        >
          <div style={{ width: sidebarWidth }} className="flex min-h-0 flex-1 flex-col">
            <Sidebar />
          </div>
        </aside>

        {!sidebarCollapsed && (
          <ResizeHandle
            onResize={(delta) =>
              setSidebarWidth(clamp(sidebarWidth + delta, 200, 420))
            }
          />
        )}

        <main className="flex min-w-0 flex-1 flex-col">
          {centerView === "raw" ? <RawMessages /> : <ChatPanel />}
        </main>

        {!inspectorCollapsed && (
          <ResizeHandle
            onResize={(delta) =>
              setInspectorWidth(clamp(inspectorWidth - delta, 320, 760))
            }
          />
        )}

        <aside
          style={{ width: inspectorCollapsed ? 0 : inspectorWidth }}
          className="glass flex shrink-0 flex-col overflow-hidden border-l border-border transition-[width] duration-300 ease-out"
        >
          <div style={{ width: inspectorWidth }} className="flex min-h-0 flex-1 flex-col">
            <InspectorPanel />
          </div>
        </aside>
      </div>

      {/* 在别的会话里答完时，右下角提示一句，免得要自己去侧栏翻 */}
      <ReplyToast />

      {error && (
        <div className="glass-strong absolute bottom-4 left-1/2 z-50 -translate-x-1/2 rounded-panel border border-warning-border px-4 py-2.5 text-sm shadow-lg">
          <span className="mr-3 text-danger">⚠ {error}</span>
          <button
            onClick={clearError}
            className="text-muted-foreground underline hover:text-foreground"
          >
            关闭
          </button>
        </div>
      )}
    </div>
  );
}

function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max);
}
