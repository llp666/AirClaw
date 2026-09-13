"use client";

/**
 * RAG 诊断页 —— 面向入库管理员。
 *
 * 解决的问题：相关性下限、切块粒度、两路召回的比例，这些参数光看代码或日志调不出来。
 * 管理员需要看到**每一条候选的原始分数**，尤其是被阈值挡掉的那一批——它们才是判断
 * 「阈值卡得松还是紧」的依据。故这个页面刻意同时展示保留与挡下两类候选。
 *
 * 数据来自 /api/rag/inspect，它与生产检索共用同一条打分路径，所以这里看到的数字
 * 就是实际检索时用的数字。若诊断页另算一套，据此调的阈值就会和生产对不上。
 *
 * **本页不做身份验证**：系统没有登录体系（见 README「操作者身份与审计目录」），
 * 能打开前端的人都能进来。它依赖部署侧的网络隔离，不是受控后台。
 */

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import {
  ArrowLeft,
  Database,
  Loader2,
  Play,
  ShieldAlert,
  Sigma,
  Check,
  X,
} from "lucide-react";
import * as api from "@/lib/api";
import { cn } from "@/lib/utils";

export default function RagAdminPage() {
  const [status, setStatus] = useState<api.RagStatus | null>(null);
  const [corpus, setCorpus] = useState<"knowledge" | "memory">("knowledge");
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(8);
  const [result, setResult] = useState<api.RagInspectResult | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await api.getRagStatus());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void loadStatus();
  }, [loadStatus]);

  const run = async () => {
    const q = query.trim();
    if (!q || busy) return;
    setBusy(true);
    setError(null);
    try {
      setResult(await api.inspectRag(corpus, q, topK));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setResult(null);
    } finally {
      setBusy(false);
    }
  };

  const kept = result?.candidates.filter((c) => c.kept) ?? [];

  return (
    <div className="scroll-thin min-h-screen bg-background">
      {/* ---- 顶部 ---- */}
      <header className="glass-strong sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-border px-5">
        <Link
          href="/"
          className="flex items-center gap-1.5 rounded-md px-2 py-1 text-[11.5px] text-muted-foreground transition hover:bg-black/[0.05] hover:text-foreground"
        >
          <ArrowLeft size={13} />
          返回对话
        </Link>
        <span className="text-[14px] font-semibold tracking-tight">RAG 诊断</span>
        <span className="text-[11px] text-muted-foreground">
          检索召回了什么、为什么 —— 入库管理员用
        </span>
        <span className="flex-1" />
        <span className="flex items-center gap-1.5 text-[11px] text-warning">
          <ShieldAlert size={13} />
          本页无身份验证，依赖部署侧网络隔离
        </span>
      </header>

      <div className="mx-auto max-w-[1180px] px-5 py-5">
        {/* ---- 索引概况 ---- */}
        {status && (
          <div className="mb-4 grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Embedding" value={status.embedding.model}
                  hint={`${status.embedding.dimensions} 维 · ${status.embedding.provider}`} />
            <Stat label="相关性下限" value={status.min_score.toString()}
                  hint="余弦低于此值且无字面命中即判为不相关" />
            <Stat label="记忆索引" value={`${status.memory.chunks} 块`}
                  hint={`向量 ${status.memory.vector_available ? "可用" : "不可用"} · 内联阈值 ${status.memory.inline_max_chars} 字符 · 当前${status.memory.inline_ok ? "整篇注入" : "走检索"}`} />
            <Stat label="知识库索引" value={`${status.knowledge.chunks} 块`}
                  hint={`向量 ${status.knowledge.vector_available ? "可用" : "不可用"} · 语料 ${status.knowledge.corpus?.count ?? "?"} 篇`} />
          </div>
        )}

        {/* ---- 查询 ---- */}
        <div className="rounded-panel border border-border bg-white/60 p-3">
          <div className="flex flex-wrap items-center gap-2">
            <div className="flex overflow-hidden rounded-lg border border-border">
              {(["knowledge", "memory"] as const).map((c) => (
                <button
                  key={c}
                  onClick={() => setCorpus(c)}
                  className={cn(
                    "px-3 py-1.5 text-[11.5px] transition",
                    corpus === c
                      ? "bg-accent text-accent-foreground"
                      : "text-muted hover:bg-black/[0.04]",
                  )}
                >
                  {c === "knowledge" ? "知识库" : "长期记忆"}
                </button>
              ))}
            </div>

            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void run()}
              placeholder="输入一个查询，看它召回了什么（与生产检索同一套打分）"
              className="min-w-[240px] flex-1 rounded-lg border border-border bg-white/80 px-3 py-1.5 text-[12.5px] outline-none focus:border-accent/50"
            />

            <label className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
              候选数
              <select
                value={topK}
                onChange={(e) => setTopK(Number(e.target.value))}
                className="rounded-md border border-border bg-white/80 px-1.5 py-1 text-[11.5px]"
              >
                {[5, 8, 12, 20, 30].map((n) => (
                  <option key={n} value={n}>{n}</option>
                ))}
              </select>
            </label>

            <button
              onClick={() => void run()}
              disabled={busy || !query.trim()}
              className="flex items-center gap-1.5 rounded-lg bg-accent px-3.5 py-1.5 text-[11.5px] font-medium text-accent-foreground transition hover:bg-accent-hover disabled:opacity-40"
            >
              {busy ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}
              运行检索
            </button>
          </div>
        </div>

        {error && (
          <p className="mt-3 rounded-lg bg-danger/10 px-3 py-2 text-[12px] text-danger">{error}</p>
        )}

        {/* ---- 结果 ---- */}
        {result && (
          <>
            <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-1 text-[11.5px] text-muted-foreground">
              <span>
                模式 <b className="text-foreground">{result.mode}</b>
              </span>
              <span>
                候选 <b className="text-foreground">{result.candidates.length}</b> 条，
                保留 <b className="text-success">{kept.length}</b> 条，
                挡下 <b className="text-foreground">{result.candidates.length - kept.length}</b> 条
              </span>
              <span>耗时 {result.elapsed_ms} ms</span>
              <span>索引共 {result.chunk_count} 块</span>
            </div>

            <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px] text-muted-foreground">
              实词通道判定用：
              {result.strong_tokens.length ? (
                result.strong_tokens.map((t) => (
                  <code key={t} className="rounded bg-black/[0.05] px-1.5 py-0.5 font-mono text-[10.5px]">
                    {t}
                  </code>
                ))
              ) : (
                <span>无（查询里没有够「实」的词，只剩向量通道）</span>
              )}
            </div>

            {/* 查询向量 */}
            <div className="mt-4 rounded-panel border border-border bg-white/60 p-3">
              <div className="mb-2 flex items-center gap-2 text-[11.5px]">
                <Sigma size={13} className="text-accent" />
                <span className="font-medium">查询向量</span>
                <span className="text-muted-foreground">
                  {result.query_vector.dim} 维 · L2 {result.query_vector.norm} · 均值{" "}
                  {result.query_vector.mean} · 范围 [{result.query_vector.min},{" "}
                  {result.query_vector.max}]
                </span>
              </div>
              <HeatStrip heat={result.query_vector.heat ?? []} />
            </div>

            {/* 候选 */}
            <div className="mt-3 space-y-2">
              {result.candidates.map((c, i) => (
                <CandidateCard key={i} rank={i + 1} c={c} minScore={result.min_score} />
              ))}
            </div>
          </>
        )}

        {!result && !error && (
          <p className="mt-10 text-center text-[12px] text-muted-foreground">
            输入一个查询并运行，即可看到它召回的候选、各自的分数与向量。
          </p>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="rounded-panel border border-border bg-white/60 px-3 py-2">
      <div className="text-[10.5px] text-muted-foreground">{label}</div>
      <div className="mt-0.5 truncate text-[13px] font-medium">{value}</div>
      <div className="mt-0.5 text-[10px] leading-relaxed text-muted-foreground">{hint}</div>
    </div>
  );
}

function CandidateCard({
  rank,
  c,
  minScore,
}: {
  rank: number;
  c: api.RagCandidate;
  minScore: number;
}) {
  return (
    <div
      className={cn(
        "rounded-panel border bg-white/60 p-3",
        c.kept ? "border-border" : "border-border/60 bg-white/25",
      )}
    >
      <div className="flex flex-wrap items-center gap-2 text-[11px]">
        <span className="font-mono text-muted-foreground">
          #{String(rank).padStart(2, "0")}
        </span>

        <span
          className={cn(
            "flex items-center gap-1 rounded px-1.5 py-0.5 font-medium",
            c.kept ? "bg-success/12 text-success" : "bg-black/[0.06] text-muted-foreground",
          )}
        >
          {c.kept ? <Check size={10} /> : <X size={10} />}
          {c.kept ? "保留" : "挡下"}
          {c.hit === "vector" && " · 向量达标"}
          {c.hit === "literal" && " · 字面命中"}
        </span>

        <span className="flex items-center gap-1 font-mono text-[10.5px] text-muted-foreground">
          <Database size={10} />
          {c.source}
          {c.entry ? `「${c.entry}」` : ""}
          {c.section ? ` · ${c.section}` : ""}
        </span>

        <span className="flex-1" />

        <span className="tabular-nums text-muted-foreground">
          向量 <b className={cn(c.vector_score >= minScore ? "text-success" : "text-foreground")}>
            {c.vector_score.toFixed(4)}
          </b>
          {"  "}BM25 <b className="text-foreground">{c.bm25_score.toFixed(2)}</b>
          {"  "}RRF <b className="text-foreground">{c.rrf_score.toFixed(5)}</b>
        </span>
      </div>

      <div className="mt-2">
        <HeatStrip heat={c.vector.heat ?? []} height={10} />
      </div>

      <p className="mt-2 whitespace-pre-wrap break-words text-[12px] leading-relaxed text-foreground/85">
        {c.text}
      </p>
    </div>
  );
}

/**
 * 向量色带：把 1024 维压成 64 段后按符号着色——正偏克莱因蓝、负偏红，
 * 透明度表示幅度。一眼能看出向量是否正常：整条几乎透明说明接近零向量，
 * 即 embedding 服务有问题。
 */
function HeatStrip({ heat, height = 14 }: { heat: number[]; height?: number }) {
  if (!heat.length) {
    return (
      <div
        className="flex items-center justify-center rounded border border-dashed border-border text-[10px] text-muted-foreground"
        style={{ height }}
      >
        无向量（向量分支不可用）
      </div>
    );
  }
  const max = Math.max(...heat.map(Math.abs), 1e-9);
  return (
    <div className="flex overflow-hidden rounded" style={{ height }}>
      {heat.map((v, i) => {
        const t = v / max;
        const alpha = Math.min(1, Math.abs(t) * 1.5);
        return (
          <div
            key={i}
            style={{
              flex: 1,
              background:
                t >= 0
                  ? `rgba(0, 47, 167, ${alpha.toFixed(3)})`
                  : `rgba(214, 69, 69, ${alpha.toFixed(3)})`,
            }}
          />
        );
      })}
    </div>
  );
}
