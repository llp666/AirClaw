"use client";

import { useRef, useState } from "react";
import { PackagePlus, Loader2, Check, AlertCircle } from "lucide-react";
import * as api from "@/lib/api";
import { cn } from "@/lib/utils";

/**
 * 技能包上传条。
 *
 * 技能包是含 SKILL.md 的目录打包成的 zip。上传成功后后端会重新生成
 * SKILLS_SNAPSHOT.md，Agent 下一轮对话即可感知新技能 —— 对应 PRD 第三章
 * 「拖入即用」的技能扩展方式。
 */
export default function SkillUploadBar({ onInstalled }: { onInstalled: () => void }) {
  const [percent, setPercent] = useState<number | null>(null);
  const [message, setMessage] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const upload = async (file: File) => {
    setMessage(null);
    setPercent(0);
    try {
      const r = await api.uploadSkill(file, setPercent);
      setMessage({
        kind: "ok",
        text: `${r.message}（当前共 ${r.skills_total} 个技能${r.snapshot_rebuilt ? "" : "，但快照重建失败"}）`,
      });
      onInstalled();
    } catch (e) {
      setMessage({ kind: "err", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setPercent(null);
    }
  };

  return (
    <div className="border-b border-border px-3 py-2">
      <button
        onClick={() => inputRef.current?.click()}
        disabled={percent !== null}
        className={cn(
          "flex w-full items-center justify-center gap-1.5 rounded-lg border border-dashed py-2 text-[11.5px] font-medium transition",
          percent !== null
            ? "border-accent/40 bg-accent-soft text-accent"
            : "border-border-strong text-muted-foreground hover:border-accent/40 hover:bg-accent-soft/50 hover:text-accent",
        )}
      >
        {percent !== null ? (
          <>
            <Loader2 size={12} className="animate-spin" />
            上传中 {percent}%
          </>
        ) : (
          <>
            <PackagePlus size={12} />
            上传技能包（.zip，内含 SKILL.md）
          </>
        )}
      </button>

      <input
        ref={inputRef}
        type="file"
        accept=".zip"
        hidden
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) void upload(f);
          e.target.value = "";
        }}
      />

      {message && (
        <div
          className={cn(
            "mt-2 flex items-start gap-1.5 rounded-lg px-2.5 py-1.5 text-[11px]",
            message.kind === "ok" ? "bg-success/10 text-success" : "bg-danger/10 text-danger",
          )}
        >
          {message.kind === "ok" ? (
            <Check size={12} className="mt-0.5 shrink-0" />
          ) : (
            <AlertCircle size={12} className="mt-0.5 shrink-0" />
          )}
          <span className="min-w-0 break-words">{message.text}</span>
        </div>
      )}
    </div>
  );
}
