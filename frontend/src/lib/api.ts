/**
 * 后端 API 客户端。
 *
 * API_BASE 动态取 window.location.hostname，使同一份代码同时支持
 * 本机（localhost:3000）与内网授权终端（<本机IP>:3000）访问，无需改配置。
 *
 * streamChat 实现了自定义 SSE 解析器：浏览器原生 EventSource 只支持 GET，
 * 而 /api/chat 是 POST。
 */

import type {
  SessionMeta,
  SkillMeta,
  StreamEvent,
  SystemPromptStats,
  TokenStats,
} from "./types";

// 组件通过 `api.XXX` 引用这些类型，故在此再导出，避免各处分别 import 两个模块。
export type {
  SessionMeta,
  SkillMeta,
  StreamEvent,
  SystemPromptStats,
  TokenStats,
} from "./types";

const API_PORT = 8002;

export const API_BASE =
  typeof window === "undefined"
    ? `http://localhost:${API_PORT}`
    : `${window.location.protocol}//${window.location.hostname}:${API_PORT}`;

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body?.detail ?? detail;
    } catch {
      /* 响应不是 JSON，沿用 statusText */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

/* ---------------- 会话 ---------------- */

export const listSessions = () =>
  request<{ sessions: SessionMeta[] }>("/api/sessions").then((r) => r.sessions);

export const createSession = () =>
  request<SessionMeta>("/api/sessions", { method: "POST" });

export const renameSession = (id: string, title: string) =>
  request<{ id: string; title: string }>(`/api/sessions/${id}`, {
    method: "PUT",
    body: JSON.stringify({ title }),
  });

export const deleteSession = (id: string) =>
  request<{ id: string; deleted: boolean }>(`/api/sessions/${id}`, {
    method: "DELETE",
  });

export const getHistory = (id: string) =>
  request<{ session_id: string; messages: RawMessage[] }>(
    `/api/sessions/${id}/history`,
  ).then((r) => r.messages);

export const compressSession = (id: string) =>
  request<{ archived_count: number; remaining_count: number; summary: string }>(
    `/api/sessions/${id}/compress`,
    { method: "POST" },
  );

/** 会话文件中的消息结构（与前端 Message 不同：内容里不含思考链等运行态数据） */
export interface RawMessage {
  role: "user" | "assistant";
  content: string;
  tool_calls?: { tool: string; input: unknown; output: string }[];
  ts?: number;
}

/* ---------------- Token ---------------- */

export const getSessionTokens = (id: string) =>
  request<TokenStats>(`/api/tokens/session/${id}`);

export const getSystemPromptStats = () =>
  request<SystemPromptStats>("/api/tokens/system-prompt");

/* ---------------- 文件 / 技能 ---------------- */

export const readFile = (path: string) =>
  request<{ path: string; content: string; size: number }>(
    `/api/files?path=${encodeURIComponent(path)}`,
  );

export const saveFile = (path: string, content: string) =>
  request<{ path: string; saved: boolean; created: boolean }>("/api/files", {
    method: "POST",
    body: JSON.stringify({ path, content }),
  });

export const listSkills = () =>
  request<{ skills: SkillMeta[] }>("/api/skills").then((r) => r.skills);

/**
 * 带进度的文件上传。
 *
 * 用 XMLHttpRequest 而非 fetch —— 需要上传进度回调，fetch 目前没有
 * 通用的上传进度事件。请求体为 multipart/form-data，不可手动设置
 * Content-Type（需由浏览器补上 boundary）。
 */
function postFile<T>(
  url: string,
  file: File,
  onProgress?: (percent: number) => void,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append("file", file);

    const xhr = new XMLHttpRequest();
    xhr.open("POST", `${API_BASE}${url}`);

    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) {
        onProgress(Math.round((e.loaded / e.total) * 100));
      }
    };
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        /* 非 JSON 响应 */
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(body as T);
      } else {
        const detail = (body as { detail?: string })?.detail;
        reject(new Error(detail ?? `上传失败（HTTP ${xhr.status}）`));
      }
    };
    xhr.onerror = () => reject(new Error("网络错误，上传失败"));
    xhr.onabort = () => reject(new Error("上传已取消"));
    xhr.send(form);
  });
}

/* ---------------- 知识库语料 ---------------- */

export interface KnowledgeFile {
  name: string;
  size: number;
}

export interface KnowledgeIndexInfo {
  files: number;
  chunks: number;
  vector_ok: boolean;
}

export interface KnowledgeList {
  files: KnowledgeFile[];
  count: number;
  index: KnowledgeIndexInfo | null;
}

export const listKnowledge = () =>
  request<KnowledgeList>("/api/knowledge/files");

export const deleteKnowledge = (name: string) =>
  request<{ name: string; deleted: boolean }>(
    `/api/knowledge/files?path=${encodeURIComponent(name)}`,
    { method: "DELETE" },
  );

export const rebuildKnowledge = () =>
  request<{ built: boolean; chunks?: number; vector_ok?: boolean }>(
    "/api/knowledge/rebuild",
    { method: "POST" },
  );

export interface UploadResult {
  name: string;
  size: number;
  replaced: boolean;
  pending: boolean;
  message: string;
}

/** 语料上传落到待审目录，**不入库**；确认入库后才参与检索。 */
export const uploadKnowledge = (
  file: File,
  onProgress?: (percent: number) => void,
) => postFile<UploadResult>("/api/knowledge/upload", file, onProgress);

export interface PendingList {
  files: KnowledgeFile[];
  count: number;
}

export const listPendingKnowledge = () =>
  request<PendingList>("/api/knowledge/pending");

/** 确认入库：待审 → knowledge/，并触发索引重建 */
export const publishKnowledge = (name: string) =>
  request<{ name: string; published: boolean; replaced: boolean; index_rebuilt: boolean }>(
    `/api/knowledge/publish?name=${encodeURIComponent(name)}`,
    { method: "POST" },
  );

/** 退回：丢弃待审文件，不入库 */
export const discardKnowledge = (name: string) =>
  request<{ name: string; discarded: boolean }>(
    `/api/knowledge/pending?name=${encodeURIComponent(name)}`,
    { method: "DELETE" },
  );

/* ---------------- 技能包 ---------------- */

export interface SkillUploadResult {
  name: string;
  files: number;
  size: number;
  replaced: boolean;
  skills_total: number;
  snapshot_rebuilt: boolean;
  message: string;
}

export const uploadSkill = (
  file: File,
  onProgress?: (percent: number) => void,
) => postFile<SkillUploadResult>("/api/skills/upload", file, onProgress);

export const uninstallSkill = (name: string) =>
  request<{ name: string; deleted: boolean }>(
    `/api/skills/${encodeURIComponent(name)}`,
    { method: "DELETE" },
  );

/* ---------------- 配置 ---------------- */

export const getRagMode = () =>
  request<{ enabled: boolean }>("/api/config/rag-mode").then((r) => r.enabled);

export const setRagMode = (enabled: boolean) =>
  request<{ enabled: boolean }>("/api/config/rag-mode", {
    method: "PUT",
    body: JSON.stringify({ enabled }),
  }).then((r) => r.enabled);

/**
 * 操作者身份由后端部署配置决定（`.env` 的 `APP_USER`），前端只负责显示。
 * 客户端不再随请求上报身份——可伪造的字段进审计日志等于没有审计。
 */
export const getAppUser = () =>
  request<{ user: string }>("/api/config").then((r) => r.user);

/* ---------------- RAG 诊断（入库管理员） ---------------- */

/** 一条向量的可读摘要。1024 维浮点数组没法直接看，故压成统计量 + 色带。 */
export interface VectorSummary {
  dim: number;
  ok: boolean;
  norm?: number;
  mean?: number;
  min?: number;
  max?: number;
  /** 前 16 维原始数值 */
  preview?: number[];
  /** 分段均值，用于渲染色带 */
  heat?: number[];
}

export interface RagCandidate {
  text: string;
  source: string;
  section?: string;
  entry?: string;
  vector_score: number;
  bm25_score: number;
  rrf_score: number;
  /** 是否通过了相关性下限 */
  kept: boolean;
  /** 靠哪条通道通过：vector / literal / null（被挡下） */
  hit: "vector" | "literal" | null;
  vector: VectorSummary;
}

export interface RagInspectResult {
  corpus: "knowledge" | "memory";
  query: string;
  elapsed_ms: number;
  mode: string;
  chunk_count: number;
  vector_available: boolean;
  min_score: number;
  /** 参与字面通道判定的实词 */
  strong_tokens: string[];
  query_vector: VectorSummary;
  candidates: RagCandidate[];
}

export interface RagStatus {
  min_score: number;
  embedding: { provider: string; model: string; dimensions: number };
  memory: {
    chunks: number;
    vector_available: boolean;
    built_at?: number;
    md5?: string;
    inline_max_chars: number;
    inline_ok: boolean;
    retrieval_top_k: number;
  };
  knowledge: {
    chunks: number;
    vector_available: boolean;
    built_at?: number;
    corpus?: { count: number };
  };
}

export const getRagStatus = () => request<RagStatus>("/api/rag/status");

export const inspectRag = (corpus: "knowledge" | "memory", query: string, topK: number) =>
  request<RagInspectResult>("/api/rag/inspect", {
    method: "POST",
    body: JSON.stringify({ corpus, query, top_k: topK }),
  });

/* ---------------- 流式对话 ---------------- */

export interface StreamHandlers {
  onEvent: (event: StreamEvent) => void;
}

/**
 * 发起一轮对话并逐事件回调。
 *
 * SSE 规范规定行终止符为 CRLF，帧之间以空行分隔。sse-starlette 严格按规范
 * 输出 `\r\n`，因此帧分隔符是 `\r\n\r\n` 而非 `\n\n` —— 后者并不包含于前者，
 * 只按 `\n\n` 切分会切不出任何帧，表现为「流式结束但界面一片空白」。
 * 规范同时允许 LF 与 CR，故三者都要支持。
 */
const FRAME_SEP = /\r\n\r\n|\n\n|\r\r/;
const LINE_SEP = /\r\n|\n|\r/;

export async function streamChat(
  message: string,
  sessionId: string,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message, session_id: sessionId, stream: true }),
    signal,
  });

  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json())?.detail ?? detail;
    } catch {
      /* 忽略 */
    }
    handlers.onEvent({ type: "error", error: detail });
    return;
  }
  if (!res.body) {
    handlers.onEvent({ type: "error", error: "响应无数据流" });
    return;
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let pending = 0;

  const dispatch = (frame: string) => {
    const dataLines: string[] = [];
    for (const line of frame.split(LINE_SEP)) {
      if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    }
    if (!dataLines.length) return;
    try {
      handlers.onEvent(JSON.parse(dataLines.join("\n")) as StreamEvent);
    } catch {
      // 单个事件解析失败不应中断整条流
      console.warn("[airclaw] 无法解析的 SSE 帧", frame.slice(0, 200));
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    pending += 1;
    // 每积累若干块就尝试切帧，避免长回复时反复扫描整个缓冲区
    if (pending >= 8 || buffer.length > 8192) {
      pending = 0;
      buffer = drain(buffer, dispatch);
    }
  }
  buffer = drain(buffer, dispatch);
  // 流结束时缓冲区可能还残留最后一帧（后端未以空行收尾）
  if (buffer.trim()) dispatch(buffer);
}

/** 切出缓冲区中所有完整的帧并派发，返回剩余的不完整部分。 */
function drain(buffer: string, dispatch: (frame: string) => void): string {
  for (;;) {
    const sep = buffer.match(FRAME_SEP);
    if (!sep || sep.index === undefined) return buffer;
    dispatch(buffer.slice(0, sep.index));
    buffer = buffer.slice(sep.index + sep[0].length);
  }
}
