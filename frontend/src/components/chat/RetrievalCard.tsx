"use client";

import { useState } from "react";
import { ChevronRight, ChevronDown, Database, Info } from "lucide-react";
import type { Retrieval } from "@/lib/types";

/** RAG 检索结果卡片（紫色折叠卡片，对应 README RetrievalCard）。 */
export default function RetrievalCard({ retrieval }: { retrieval: Retrieval }) {
  const [open, setOpen] = useState(false);
  const n = retrieval.results?.length ?? 0;

  return (
    <div className="my-2 overflow-hidden rounded-lg border border-retrieval-border bg-retrieval-bg">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1.5 px-3 py-1.5 text-left text-[11px] font-medium text-retrieval transition hover:bg-black/[0.03]"
      >
        {open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
        <Database size={12} />
        <span>记忆检索 · 命中 {n} 条</span>
        <span className="ml-1 min-w-0 flex-1 truncate font-normal text-muted-foreground">
          {retrieval.query}
        </span>
      </button>

      {open && (
        <div className="space-y-2 border-t border-retrieval-border px-3 py-2">
          {retrieval.note && (
            <div className="flex items-start gap-1.5 text-[11px] text-warning">
              <Info size={12} className="mt-0.5 shrink-0" />
              <span>{retrieval.note}</span>
            </div>
          )}

          {n === 0 && !retrieval.note && (
            <p className="text-[11px] text-muted-foreground">未检索到相关记忆。</p>
          )}

          {retrieval.results.map((r, i) => (
            <div key={i} className="rounded-md bg-white/70 p-2">
              <div className="mb-1 flex items-center gap-2 text-[10px] text-muted-foreground">
                <span className="font-mono">{r.source}</span>
                {typeof r.score === "number" && (
                  <span>score {r.score.toFixed(4)}</span>
                )}
              </div>
              <p className="whitespace-pre-wrap text-[11.5px] leading-relaxed text-foreground/85">
                {r.text}
              </p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
