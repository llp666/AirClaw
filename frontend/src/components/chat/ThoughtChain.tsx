"use client";

import { useState } from "react";
import { ChevronRight, ChevronDown, CheckCircle2, XCircle, SkipForward } from "lucide-react";
import type { ToolCall } from "@/lib/types";
import { cn } from "@/lib/utils";

/**
 * 工具调用思维链（可折叠）。
 *
 * 注意：这里的「思维链」按 README 的定义指**工具调用链**，
 * 即 tool_start / tool_end 事件构成的执行序列；模型的推理内容
 * 由单独的 ReasoningBlock 展示。
 */
export default function ThoughtChain({ calls }: { calls: ToolCall[] }) {
  const [open, setOpen] = useState(false);

  if (!calls.length) return null;

  // 折叠状态下也展示调用了哪些工具及次数：透明可控是项目核心诉求，
  // 不该为了折叠而隐藏「Agent 用了什么能力」这一关键信息。
  const summary = summarize(calls);
  const failed = calls.filter((c) => isFailed(c.output)).length;

  return (
    <div className="my-2 overflow-hidden rounded-lg border border-border bg-black/[0.015]">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1.5 px-3 py-1.5 text-left text-[11px] font-medium text-muted-foreground transition hover:bg-black/[0.03]"
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <span className="shrink-0">工具调用 · {calls.length} 次</span>

        <span className="flex min-w-0 flex-1 flex-wrap items-center gap-1">
          {summary.map((s) => (
            <code
              key={s.tool}
              className="truncate rounded bg-accent-soft px-1.5 py-0.5 font-mono text-[10px] font-normal text-accent"
            >
              {s.tool}
              {s.count > 1 && ` ×${s.count}`}
            </code>
          ))}
        </span>

        {failed > 0 && (
          <span className="shrink-0 text-[10px] font-normal text-danger">
            {failed} 次异常
          </span>
        )}

        <span className="flex shrink-0 gap-1">
          {calls.map((c, i) => (
            <StatusDot key={i} output={c.output} />
          ))}
        </span>
      </button>

      {open && (
        <div className="border-t border-border">
          {calls.map((call, i) => (
            <ToolCallItem key={i} call={call} index={i} />
          ))}
        </div>
      )}
    </div>
  );
}

/** 按工具名归并计数，保留首次出现顺序。 */
function summarize(calls: ToolCall[]): { tool: string; count: number }[] {
  const order: string[] = [];
  const counts = new Map<string, number>();
  for (const c of calls) {
    if (!counts.has(c.tool)) order.push(c.tool);
    counts.set(c.tool, (counts.get(c.tool) ?? 0) + 1);
  }
  return order.map((tool) => ({ tool, count: counts.get(tool)! }));
}

function isFailed(output: string): boolean {
  return (
    output.startsWith("[退出码") ||
    output.startsWith("[已拦截]") ||
    output.includes("[已被审查拦截]") ||
    output.includes("Traceback")
  );
}

function StatusDot({ output }: { output: string }) {
  if (!output) return <span className="text-[9px] text-muted-foreground">●</span>;
  return (
    <span className={cn("text-[9px]", isFailed(output) ? "text-danger" : "text-success")}>
      ●
    </span>
  );
}

function ToolCallItem({ call, index }: { call: ToolCall; index: number }) {
  const [open, setOpen] = useState(false);
  const output = call.output ?? "";
  const done = output.length > 0;
  const failed =
    output.startsWith("[退出码") ||
    output.startsWith("[已拦截]") ||
    output.includes("[已被审查拦截]") ||
    output.includes("Traceback");
  const deduped = output.startsWith("[去重命中");

  return (
    <div className="border-b border-border last:border-b-0">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left transition hover:bg-black/[0.02]"
      >
        <span className="shrink-0 font-mono text-[10px] text-muted-foreground">
          {String(index + 1).padStart(2, "0")}
        </span>
        <span className="shrink-0 rounded bg-accent-soft px-1.5 py-0.5 font-mono text-[10px] font-medium text-accent">
          {call.tool}
        </span>
        <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-muted-foreground">
          {previewInput(call.input)}
        </span>
        {!done ? (
          <span className="shrink-0 text-[9px] text-muted-foreground">运行中…</span>
        ) : failed ? (
          <XCircle size={12} className="shrink-0 text-danger" />
        ) : deduped ? (
          <SkipForward size={12} className="shrink-0 text-muted-foreground" />
        ) : (
          <CheckCircle2 size={12} className="shrink-0 text-success" />
        )}
      </button>

      {open && (
        <div className="space-y-2 bg-black/[0.02] px-3 py-2">
          <Field label="入参" value={prettyInput(call.input)} />
          {done && <Field label="输出" value={output} />}
        </div>
      )}
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="mb-2 text-[10px] font-medium text-muted-foreground">{label}</div>
      <pre className="scroll-thin max-h-72 overflow-auto whitespace-pre-wrap rounded-md bg-white/70 p-2 font-mono text-[10.5px] leading-relaxed text-foreground/90">
        {value}
      </pre>
    </div>
  );
}

function previewInput(input: unknown): string {
  if (input == null) return "";
  if (typeof input === "string") return input;
  const obj = input as Record<string, unknown>;
  const first = obj.command ?? obj.code ?? obj.query ?? obj.file_path ?? obj.path;
  return typeof first === "string" ? first.split("\n")[0] : JSON.stringify(input);
}

function prettyInput(input: unknown): string {
  if (input == null) return "(无)";
  if (typeof input === "string") return input;
  const obj = input as Record<string, unknown>;
  const raw = obj.command ?? obj.code ?? obj.query ?? obj.file_path ?? obj.path;
  return typeof raw === "string" ? raw : JSON.stringify(input, null, 2);
}
