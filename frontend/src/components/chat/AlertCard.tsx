"use client";

import { ShieldAlert, ShieldCheck } from "lucide-react";
import type { AuditWarning } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * 审计告警卡片。
 *
 * 对应 PRD 第六章：对审查拦截事件以醒目样式（警告色边框 + 命中规则摘要 +
 * 建议动作）内联展示。
 *
 * 区分两种情形：
 *   blocked=true  —— 工具调用被阻断（block 规则）
 *   blocked=false —— 仅告警未阻断（flag 规则，如「秘密」这类中文常用词）
 */
export default function AlertCard({ warning }: { warning: AuditWarning }) {
  const blocked = warning.blocked !== false;

  return (
    <div
      className={cn(
        "my-2 rounded-lg border-l-[3px] px-3 py-2.5",
        blocked
          ? "border-l-danger bg-warning-bg"
          : "border-l-warning bg-warning-bg/60",
      )}
    >
      <div className="flex items-start gap-2">
        {blocked ? (
          <ShieldAlert size={14} className="mt-0.5 shrink-0 text-danger" />
        ) : (
          <ShieldCheck size={14} className="mt-0.5 shrink-0 text-warning" />
        )}

        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span
              className={cn(
                "text-[11px] font-semibold",
                blocked ? "text-danger" : "text-warning",
              )}
            >
              {blocked ? "审查已阻断本次工具调用" : "审计提示"}
            </span>
            <code className="rounded bg-black/[0.06] px-1.5 py-0.5 font-mono text-[10px] text-foreground/80">
              {warning.rule_id}
            </code>
            {warning.tool && (
              <code className="rounded bg-accent-soft px-1.5 py-0.5 font-mono text-[10px] text-accent">
                {warning.tool}
              </code>
            )}
          </div>

          <p className="mt-1 text-[11.5px] leading-relaxed text-foreground/85">
            {warning.reason}
          </p>

          {warning.matched && (
            <p className="mt-1 font-mono text-[10.5px] text-muted-foreground">
              命中内容：<span className="text-foreground/75">{warning.matched}</span>
            </p>
          )}

          {blocked && (
            <p className="mt-1.5 text-[10.5px] leading-relaxed text-muted-foreground">
              建议动作：确认该操作是否确有必要。如确需执行，请联系保密管理员
              走受控流程，不要通过改写参数规避审查。
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
