"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import {
  Upload,
  Trash2,
  RefreshCw,
  Loader2,
  Check,
  AlertCircle,
  FileText,
  ShieldAlert,
  Activity,
} from "lucide-react";
import * as api from "@/lib/api";
import { cn } from "@/lib/utils";

const ACCEPT = ".md,.txt,.pdf,.docx,.xlsx,.csv";
const MAX_MB = 50;

interface Uploading {
  name: string;
  percent: number;
}

/** 知识库语料管理：上传待审 / 确认入库 / 删除 / 重建索引。 */
export default function KnowledgePanel() {
  const [data, setData] = useState<api.KnowledgeList | null>(null);
  const [pending, setPending] = useState<api.PendingList | null>(null);
  const [uploading, setUploading] = useState<Uploading[]>([]);
  const [dragging, setDragging] = useState(false);
  const [message, setMessage] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [rebuilding, setRebuilding] = useState(false);
  /** 正在入库/退回的文件名，用于禁用按钮防重复点击 */
  const [acting, setActing] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const refresh = useCallback(async () => {
    try {
      const [published, waiting] = await Promise.all([
        api.listKnowledge(),
        api.listPendingKnowledge(),
      ]);
      setData(published);
      setPending(waiting);
    } catch (e) {
      setMessage({ kind: "err", text: e instanceof Error ? e.message : String(e) });
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const upload = useCallback(
    async (files: File[]) => {
      if (!files.length) return;
      setMessage(null);

      for (const file of files) {
        setUploading((prev) => [...prev, { name: file.name, percent: 0 }]);
        try {
          const r = await api.uploadKnowledge(file, (percent) =>
            setUploading((prev) =>
              prev.map((u) => (u.name === file.name ? { ...u, percent } : u)),
            ),
          );
          setMessage({
            kind: "ok",
            text: `${r.name} 已上传至待审目录（${formatSize(r.size)}${r.replaced ? "，覆盖待审同名文件" : ""}）`,
          });
        } catch (e) {
          setMessage({
            kind: "err",
            text: `${file.name}：${e instanceof Error ? e.message : String(e)}`,
          });
        } finally {
          setUploading((prev) => prev.filter((u) => u.name !== file.name));
        }
      }

      await refresh();
      // 不上库也不重建索引：待审文件不参与检索，索引要等「确认入库」时才重建
    },
    [refresh],
  );

  const rebuild = async () => {
    setRebuilding(true);
    try {
      await api.rebuildKnowledge();
      await refresh();
    } catch (e) {
      setMessage({ kind: "err", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setRebuilding(false);
    }
  };

  const remove = async (name: string) => {
    setMessage(null);
    try {
      await api.deleteKnowledge(name);
      await refresh();
    } catch (e) {
      setMessage({ kind: "err", text: e instanceof Error ? e.message : String(e) });
    }
  };

  /** 确认入库：待审 → knowledge/，后端会顺带重建索引 */
  const publish = async (name: string) => {
    setMessage(null);
    setActing(name);
    try {
      const r = await api.publishKnowledge(name);
      setMessage({
        kind: "ok",
        text: `${name} 已入库${r.replaced ? "（覆盖同名语料）" : ""}，${
          r.index_rebuilt ? "索引已重建，现在可被检索" : "但索引重建失败，需手动重建"
        }`,
      });
      await refresh();
    } catch (e) {
      setMessage({ kind: "err", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setActing(null);
    }
  };

  /** 退回：丢弃待审文件 */
  const discard = async (name: string) => {
    setMessage(null);
    setActing(name);
    try {
      await api.discardKnowledge(name);
      setMessage({ kind: "ok", text: `${name} 已退回丢弃，未入库` });
      await refresh();
    } catch (e) {
      setMessage({ kind: "err", text: e instanceof Error ? e.message : String(e) });
    } finally {
      setActing(null);
    }
  };

  const onDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    void upload(Array.from(e.dataTransfer.files));
  };

  return (
    <div className="scroll-thin min-h-0 flex-1 overflow-y-auto p-3">
      {/* ---- 上传区 ---- */}
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        onClick={() => inputRef.current?.click()}
        className={cn(
          "flex cursor-pointer flex-col items-center justify-center rounded-panel border-2 border-dashed px-4 py-7 text-center transition",
          dragging
            ? "border-accent bg-accent-soft"
            : "border-border-strong bg-white/40 hover:border-accent/40 hover:bg-accent-soft/40",
        )}
      >
        <Upload size={18} className={dragging ? "text-accent" : "text-muted-foreground"} />
        <p className="mt-2 text-[12.5px] font-medium">拖拽文件到此处，或点击选择</p>
        <p className="mt-1 text-[11px] text-muted-foreground">
          支持 {ACCEPT.replace(/\./g, "").toUpperCase()} · 单文件 ≤ {MAX_MB}MB
        </p>
        <input
          ref={inputRef}
          type="file"
          accept={ACCEPT}
          multiple
          hidden
          onChange={(e) => {
            void upload(Array.from(e.target.files ?? []));
            e.target.value = ""; // 允许重复选择同一文件
          }}
        />
      </div>

      {/* ---- 保密提醒（PRD 要求语料入库前须经保密审查）---- */}
      <div className="mt-3 flex items-start gap-2 rounded-lg border-l-[3px] border-l-warning bg-warning-bg px-3 py-2">
        <ShieldAlert size={13} className="mt-0.5 shrink-0 text-warning" />
        <p className="text-[11px] leading-relaxed text-foreground/85">
          上传后文件落在<strong>待审目录，不参与检索</strong>。须由保密管理员确认内容可
          入库存放，再点「确认入库」才会进入知识库。上传、入库、退回、删除均写入审计日志。
        </p>
      </div>

      {/* ---- 上传进度 ---- */}
      {uploading.map((u) => (
        <div key={u.name} className="mt-2 rounded-lg border border-border bg-white/60 p-2.5">
          <div className="flex items-center gap-2 text-[11.5px]">
            <Loader2 size={12} className="animate-spin text-accent" />
            <span className="min-w-0 flex-1 truncate">{u.name}</span>
            <span className="tabular-nums text-muted-foreground">{u.percent}%</span>
          </div>
          <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-black/[0.07]">
            <div
              className="h-full rounded-full bg-accent transition-[width]"
              style={{ width: `${u.percent}%` }}
            />
          </div>
        </div>
      ))}

      {/* ---- 结果提示 ---- */}
      {message && (
        <div
          className={cn(
            "mt-2 flex items-start gap-1.5 rounded-lg px-2.5 py-1.5 text-[11px]",
            message.kind === "ok"
              ? "bg-success/10 text-success"
              : "bg-danger/10 text-danger",
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

      {/* ---- 待审语料：入库前的必经环节 ---- */}
      {(pending?.count ?? 0) > 0 && (
        <>
          <div className="mt-4 flex items-center justify-between">
            <span className="text-[11px] font-medium text-warning">
              待审语料 · {pending?.count}
            </span>
            <span className="text-[10px] text-muted-foreground">未入库，检索不到</span>
          </div>
          <div className="mt-1.5 space-y-0.5">
            {pending?.files.map((f) => (
              <div
                key={f.name}
                className="flex items-center gap-2 rounded-lg border border-warning-border bg-warning-bg/50 px-2 py-1.5"
              >
                <FileText size={12} className="shrink-0 text-warning" />
                <span className="min-w-0 flex-1 truncate font-mono text-[11px]">
                  {f.name}
                </span>
                <span className="shrink-0 text-[10px] tabular-nums text-muted-foreground">
                  {formatSize(f.size)}
                </span>
                <button
                  onClick={() => void publish(f.name)}
                  disabled={acting === f.name}
                  title="确认入库：进入知识库并立即可被检索"
                  className="shrink-0 rounded p-0.5 text-success transition hover:bg-black/[0.05] disabled:opacity-40"
                >
                  {acting === f.name ? (
                    <Loader2 size={12} className="animate-spin" />
                  ) : (
                    <Check size={12} />
                  )}
                </button>
                <button
                  onClick={() => void discard(f.name)}
                  disabled={acting === f.name}
                  title="退回：丢弃该待审文件，不入库"
                  className="shrink-0 rounded p-0.5 text-muted-foreground transition hover:text-danger disabled:opacity-40"
                >
                  <Trash2 size={12} />
                </button>
              </div>
            ))}
          </div>
        </>
      )}

      {/* ---- 语料列表 ---- */}
      <div className="mt-4 flex items-center justify-between">
        <span className="text-[11px] font-medium text-muted-foreground">
          语料文件 · {data?.count ?? 0}
        </span>
        <div className="flex items-center gap-1">
          {/* 检索诊断在独立页面：那里要展示每条候选的分数与向量，塞进这个窄栏没法看 */}
          <Link
            href="/admin"
            title="打开 RAG 诊断页：查看检索召回了什么、每条候选的分数与向量"
            className="flex items-center gap-1 rounded-md px-2 py-0.5 text-[11px] text-muted-foreground transition hover:bg-black/[0.05] hover:text-foreground"
          >
            <Activity size={11} />
            检索诊断
          </Link>
          <button
            onClick={() => void rebuild()}
            disabled={rebuilding}
            title="重建检索索引"
            className="flex items-center gap-1 rounded-md px-2 py-0.5 text-[11px] text-muted-foreground transition hover:bg-black/[0.05] hover:text-foreground disabled:opacity-40"
          >
            {rebuilding ? (
              <Loader2 size={11} className="animate-spin" />
            ) : (
              <RefreshCw size={11} />
            )}
            重建索引
          </button>
        </div>
      </div>

      <div className="mt-1.5 space-y-0.5">
        {data?.count === 0 && (
          <p className="px-1 py-3 text-[11.5px] leading-relaxed text-muted-foreground">
            知识库为空。上传标准规范、型号资料等文档后，Agent 即可通过
            <code className="mx-1 rounded bg-black/[0.05] px-1 font-mono text-[10.5px]">
              search_knowledge_base
            </code>
            检索。
          </p>
        )}

        {data?.files.map((f) => (
          <div
            key={f.name}
            className="group flex items-center gap-2 rounded-lg px-2 py-1.5 transition hover:bg-black/[0.03]"
          >
            <FileText size={12} className="shrink-0 text-muted-foreground" />
            <span className="min-w-0 flex-1 truncate font-mono text-[11px]">
              {f.name}
            </span>
            <span className="shrink-0 text-[10px] tabular-nums text-muted-foreground">
              {formatSize(f.size)}
            </span>
            <button
              onClick={() => void remove(f.name)}
              title="删除该语料"
              className="shrink-0 rounded p-0.5 text-muted-foreground opacity-0 transition group-hover:opacity-100 hover:text-danger"
            >
              <Trash2 size={12} />
            </button>
          </div>
        ))}
      </div>

      {/* ---- 索引状态 ---- */}
      {data?.index && (
        <div className="mt-3 border-t border-border pt-2 text-[10px] text-muted-foreground">
          已索引 {data.index.files} 篇 / {data.index.chunks} 分块 ·
          向量分支 {data.index.vector_ok ? "可用" : "不可用（降级为纯 BM25）"}
        </div>
      )}
    </div>
  );
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}
