"use client";

/**
 * 全局状态。按 PRD/README，全部通过 React Context 管理，不引入 Redux。
 *
 * 消息列表采用「追加 + 原地更新末条」的模型：流式期间只更新最后一条 assistant
 * 消息，避免每个 token 都重建整个数组。
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import * as api from "./api";
import type { Message, SessionMeta, StreamEvent } from "./types";

const uid = () =>
  typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : Math.random().toString(36).slice(2);

function newMessage(role: Message["role"]): Message {
  return {
    id: uid(),
    role,
    content: "",
    reasoning: "",
    toolCalls: [],
    retrievals: [],
    warnings: [],
  };
}

/** 把会话文件里的原始消息转成前端消息模型 */
function fromRaw(raw: api.RawMessage): Message {
  return {
    id: uid(),
    role: raw.role,
    content: raw.content ?? "",
    reasoning: "",
    toolCalls: (raw.tool_calls ?? []).map((t) => ({
      tool: t.tool,
      input: t.input,
      output: t.output ?? "",
    })),
    retrievals: [],
    warnings: [],
    ts: raw.ts,
  };
}

export type InspectorView = "memory" | "skills" | "knowledge";

/** 中间主区显示什么：对话流，还是会话文件的原始消息 */
export type CenterView = "chat" | "raw";

interface Store {
  messages: Message[];
  sessions: SessionMeta[];
  sessionId: string | null;
  /** **当前会话**是否正在生成 */
  streaming: boolean;
  /** 所有正在生成的会话。各会话可同时提问，侧栏据此逐个显示状态 */
  streamingSessions: Record<string, boolean>;
  /** 在别的会话里生成完、用户还没看过的回复：sessionId → 完成时间 */
  unseenReplies: Record<string, number>;
  ragMode: boolean;
  /** 部署级操作者身份，由后端 /api/config 提供（前端只显示，不上报） */
  user: string;
  /** 输入框里还没发出去的内容，按会话分开记 */
  draft: string;
  centerView: CenterView;
  error: string | null;
  sidebarWidth: number;
  inspectorWidth: number;
  sidebarCollapsed: boolean;
  inspectorCollapsed: boolean;
  inspectorView: InspectorView;
  selectedFile: string | null;

  sendMessage: (text: string) => Promise<void>;
  /** 不传参数则中断当前会话 */
  stopStreaming: (sessionId?: string) => void;
  newSession: () => Promise<void>;
  switchSession: (id: string) => Promise<void>;
  removeSession: (id: string) => Promise<void>;
  compress: () => Promise<void>;
  toggleRag: () => Promise<void>;
  setDraft: (text: string) => void;
  /** 关掉「后台回复完成」的提醒（不跳转，仅清除角标） */
  dismissUnseen: (sessionId: string) => void;
  setCenterView: (v: CenterView) => void;
  setSidebarWidth: (w: number) => void;
  setInspectorWidth: (w: number) => void;
  toggleSidebar: () => void;
  toggleInspector: () => void;
  setInspectorView: (v: InspectorView) => void;
  setSelectedFile: (p: string | null) => void;
  clearError: () => void;
}

const StoreContext = createContext<Store | null>(null);

/** 每个会话各存自己的消息。
 *
 * 切换会话**不中断**进行中的生成：流式事件写进「发起它的那条会话」，
 * 与当前显示的是哪条无关。切走只是换个显示，切回来内容还在——后端也一直在
 * 照常收流并落盘，切回来时读到的就是完整的回复。
 */
type Threads = Record<string, Message[]>;

/** 稳定的空数组：会话未选中时不能每次渲染都造一个新引用 */
const NO_MESSAGES: Message[] = [];

/** 草稿在「还没建会话」阶段挂在这个键下 */
const DRAFT_NEW = "__new__";

const LS_SESSION = "airclaw.sessionId";
const LS_DRAFTS = "airclaw.drafts";

function readStored(key: string): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null; // 隐私模式等场景下 localStorage 不可用，不影响功能
  }
}

function writeStored(key: string, value: string | null): void {
  if (typeof window === "undefined") return;
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    /* 同上 */
  }
}

export function StoreProvider({ children }: { children: React.ReactNode }) {
  const [threads, setThreads] = useState<Threads>({});
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [streamingSessions, setStreamingSessions] = useState<Record<string, boolean>>({});
  const [unseenReplies, setUnseenReplies] = useState<Record<string, number>>({});
  const [ragMode, setRagMode] = useState(false);
  const [user, setUser] = useState("");
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  /** 是否已经把 localStorage 里的会话与草稿读回内存 */
  const [hydrated, setHydrated] = useState(false);
  const [centerView, setCenterView] = useState<CenterView>("chat");
  const [error, setError] = useState<string | null>(null);
  const [sidebarWidth, setSidebarWidth] = useState(260);
  const [inspectorWidth, setInspectorWidth] = useState(420);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [inspectorCollapsed, setInspectorCollapsed] = useState(false);
  const [inspectorView, setInspectorView] = useState<InspectorView>("memory");
  const [selectedFile, setSelectedFile] = useState<string | null>("memory/MEMORY.md");

  // 每个会话各有一个 AbortController：多个会话可以同时提问，互不打断
  const abortRefs = useRef<Record<string, AbortController>>({});
  // 流式过程中需要读取当前会话 id，用 ref 避免闭包捕获旧值
  const sessionIdRef = useRef<string | null>(null);
  sessionIdRef.current = sessionId;
  // 上次停留的会话 id，挂载时从 localStorage 读一次
  const savedSessionRef = useRef<string | null>(null);

  const messages = sessionId ? (threads[sessionId] ?? NO_MESSAGES) : NO_MESSAGES;
  const draft = drafts[sessionId ?? DRAFT_NEW] ?? "";
  const streaming = sessionId ? !!streamingSessions[sessionId] : false;

  const refreshSessions = useCallback(async (): Promise<SessionMeta[]> => {
    try {
      const list = await api.listSessions();
      setSessions(list);
      return list;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return [];
    }
  }, []);

  /* ---------------- 初始化 ---------------- */

  useEffect(() => {
    void (async () => {
      try {
        setRagMode(await api.getRagMode());
        setUser(await api.getAppUser());
      } catch {
        /* 后端未就绪时不阻断页面渲染 */
      }
      await refreshSessions();
    })();
  }, [refreshSessions]);

  /* 存储的回读必须早于任何写入：挂载时 sessionId 还是 null、drafts 还是空表，
     那两个写入 effect 若立刻执行，会把上次存下的会话与草稿原地抹掉。
     故先读进内存并置 hydrated，之后才允许写。会话 id 先存进 ref——
     等会话列表到手才能确认它还存不存在，而那时存储里的键已经被清掉了。
     草稿同理不能放进 useState 初值：那会让服务端预渲染与客户端不一致。 */
  useEffect(() => {
    savedSessionRef.current = readStored(LS_SESSION);
    try {
      const raw = readStored(LS_DRAFTS);
      if (raw) setDrafts(JSON.parse(raw) as Record<string, string>);
    } catch {
      /* 存坏了就当没有草稿 */
    }
    setHydrated(true);
  }, []);

  useEffect(() => {
    if (hydrated) writeStored(LS_DRAFTS, JSON.stringify(drafts));
  }, [drafts, hydrated]);

  useEffect(() => {
    if (hydrated) writeStored(LS_SESSION, sessionId);
  }, [sessionId, hydrated]);

  /* ---------------- 事件处理 ---------------- */

  /** 更新某条会话的最后一条消息（流式期间只动它，避免每个 token 重建整个列表） */
  const patchThread = useCallback((id: string, patch: (m: Message) => Message) => {
    setThreads((prev) => {
      const list = prev[id];
      if (!list?.length) return prev;
      const next = list.slice();
      next[next.length - 1] = patch(next[next.length - 1]);
      return { ...prev, [id]: next };
    });
  }, []);

  /** 把一条流式事件写进它所属的会话。
   *
   * sid 由发起请求的那次调用带进来，而不是读「当前显示的是哪条」——多个会话
   * 可以同时生成，事件必须各归各的会话，否则会互相写错列表。
   */
  const dispatch = useCallback(
    (sid: string, event: StreamEvent) => {
      const patchLast = (patch: (m: Message) => Message) => patchThread(sid, patch);

      switch (event.type) {
        case "token":
          patchLast((m) => ({ ...m, content: m.content + event.content }));
          break;

        case "reasoning":
          patchLast((m) => ({ ...m, reasoning: m.reasoning + event.content }));
          break;

        case "tool_start":
          patchLast((m) => ({
            ...m,
            toolCalls: [
              ...m.toolCalls,
              { tool: event.tool, input: event.input, output: "" },
            ],
          }));
          break;

        case "tool_end":
          // 与最近一条同名且尚无输出的调用配对。模型会并行发出相同调用，
          // 故此处分两步：先找未闭合的，找不到再退化为追加。
          patchLast((m) => {
            const calls = m.toolCalls.slice();
            let idx = -1;
            for (let i = calls.length - 1; i >= 0; i--) {
              if (calls[i].tool === event.tool && !calls[i].output) {
                idx = i;
                break;
              }
            }
            if (idx >= 0) {
              calls[idx] = {
                ...calls[idx],
                input: event.input ?? calls[idx].input,
                output: event.output,
              };
            } else {
              calls.push({
                tool: event.tool,
                input: event.input,
                output: event.output,
              });
            }
            return { ...m, toolCalls: calls };
          });
          break;

        case "audit_warning":
          patchLast((m) => ({
            ...m,
            warnings: [
              ...m.warnings,
              {
                rule_id: event.rule_id,
                reason: event.reason,
                severity: event.severity,
                matched: event.matched,
                blocked: event.blocked,
                tool: event.tool,
              },
            ],
          }));
          break;

        case "retrieval":
          patchLast((m) => ({
            ...m,
            retrievals: [
              ...m.retrievals,
              {
                query: event.query,
                results: event.results ?? [],
                note: event.note,
              },
            ],
          }));
          break;

        case "new_response":
          // 工具执行完毕，模型开始新一轮文本生成。数据上仍记为独立的一段
          // （与后端落盘的分段结构一致），由 ChatPanel 合并进同一个回答气泡。
          setThreads((prev) => {
            const list = prev[sid] ?? [];
            const last = list[list.length - 1];
            if (last && !last.content && !last.toolCalls.length) return prev;
            return {
              ...prev,
              [sid]: [...list, { ...newMessage("assistant"), streaming: true }],
            };
          });
          break;

        case "done":
          // 打上完成时间：对话末尾据此显示「回复完成 · HH:MM」
          patchLast((m) => ({ ...m, streaming: false, ts: Date.now() / 1000 }));
          break;

        case "title":
          setSessions((prev) =>
            prev.map((s) =>
              s.id === event.session_id ? { ...s, title: event.title } : s,
            ),
          );
          break;

        case "error":
          setError(event.error);
          patchLast((m) => ({ ...m, streaming: false, error: event.error }));
          break;
      }
    },
    [patchThread],
  );

  /* ---------------- 动作 ---------------- */

  const ensureSession = useCallback(async (): Promise<string> => {
    if (sessionIdRef.current) return sessionIdRef.current;
    const created = await api.createSession();
    setSessionId(created.id);
    sessionIdRef.current = created.id;
    await refreshSessions();
    return created.id;
  }, [refreshSessions]);

  const sendMessage = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      const current = sessionIdRef.current;
      // 只拦「这条会话自己正在生成」：别的会话在生成不影响在这里提问
      if (!trimmed || (current && streamingSessions[current])) return;

      setError(null);
      const id = await ensureSession();
      if (streamingSessions[id]) return;

      const userMsg = newMessage("user");
      userMsg.content = trimmed;
      const assistant = { ...newMessage("assistant"), streaming: true };
      setThreads((prev) => ({
        ...prev,
        [id]: [...(prev[id] ?? []), userMsg, assistant],
      }));
      setStreamingSessions((prev) => ({ ...prev, [id]: true }));
      // 又发了新问题，之前那条「后台已完成」的提醒就该收了
      setUnseenReplies((prev) => {
        if (!prev[id]) return prev;
        const next = { ...prev };
        delete next[id];
        return next;
      });

      const controller = new AbortController();
      abortRefs.current[id] = controller;

      try {
        await api.streamChat(
          trimmed,
          id,
          { onEvent: (event) => dispatch(id, event) },
          controller.signal,
        );
      } catch (e) {
        if ((e as Error).name !== "AbortError") {
          setError(e instanceof Error ? e.message : String(e));
        }
        patchThread(id, (m) => ({ ...m, streaming: false }));
      } finally {
        delete abortRefs.current[id];
        // 一律用 id 定位：期间用户可能已经切到别的会话去了
        setStreamingSessions((prev) => {
          if (!prev[id]) return prev;
          const next = { ...prev };
          delete next[id];
          return next;
        });
        patchThread(id, (m) => ({ ...m, streaming: false }));
        await refreshSessions();
        // 生成完时人不在这个会话里 —— 留个提醒，否则要自己翻侧栏才知道答完了
        if (sessionIdRef.current !== id) {
          setUnseenReplies((prev) => ({ ...prev, [id]: Date.now() / 1000 }));
        }
      }
    },
    [streamingSessions, ensureSession, dispatch, patchThread, refreshSessions],
  );

  const stopStreaming = useCallback(
    (target?: string) => {
      const id = target ?? sessionIdRef.current;
      if (!id) return;
      abortRefs.current[id]?.abort();
      delete abortRefs.current[id];
      setStreamingSessions((prev) => {
        if (!prev[id]) return prev;
        const next = { ...prev };
        delete next[id];
        return next;
      });
      patchThread(id, (m) => ({ ...m, streaming: false }));
    },
    [patchThread],
  );

  const dismissUnseen = useCallback((id: string) => {
    setUnseenReplies((prev) => {
      if (!prev[id]) return prev;
      const next = { ...prev };
      delete next[id];
      return next;
    });
  }, []);

  const newSession = useCallback(async () => {
    // 不中断正在生成的会话：它们继续在后台收流并落盘，只是不再显示
    setSessionId(null);
    sessionIdRef.current = null;
    await refreshSessions();
  }, [refreshSessions]);

  const switchSession = useCallback(
    async (id: string) => {
      setUnseenReplies((prev) => {
        if (!prev[id]) return prev;
        const next = { ...prev };
        delete next[id];
        return next;
      });
      if (id === sessionIdRef.current) return;
      sessionIdRef.current = id;
      setSessionId(id);

      // 正在收流的那条：本地就是最新内容，回读会话文件反而会把已收到的增量抹掉
      if (streamingSessions[id]) return;

      try {
        const raw = await api.getHistory(id);
        setThreads((prev) => ({ ...prev, [id]: raw.map(fromRaw) }));
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [streamingSessions],
  );

  const removeSession = useCallback(
    async (id: string) => {
      try {
        // 删掉的会话若正在生成，先停掉：否则后端跑完会把文件原样写回来
        if (streamingSessions[id]) stopStreaming(id);
        dismissUnseen(id);
        await api.deleteSession(id);
        if (id === sessionIdRef.current) {
          setSessionId(null);
          sessionIdRef.current = null;
        }
        setThreads((prev) => {
          const next = { ...prev };
          delete next[id];
          return next;
        });
        await refreshSessions();
      } catch (e) {
        setError(e instanceof Error ? e.message : String(e));
      }
    },
    [refreshSessions, stopStreaming, streamingSessions, dismissUnseen],
  );

  const compress = useCallback(async () => {
    const id = sessionIdRef.current;
    if (!id) return;
    try {
      await api.compressSession(id);
      const raw = await api.getHistory(id);
      setThreads((prev) => ({ ...prev, [id]: raw.map(fromRaw) }));
      await refreshSessions();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [refreshSessions]);

  const toggleRag = useCallback(async () => {
    try {
      setRagMode(await api.setRagMode(!ragMode));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [ragMode]);

  const setDraft = useCallback((text: string) => {
    const key = sessionIdRef.current ?? DRAFT_NEW;
    setDrafts((prev) => ({ ...prev, [key]: text }));
  }, []);

  const toggleSidebar = useCallback(() => setSidebarCollapsed((v) => !v), []);
  const toggleInspector = useCallback(() => setInspectorCollapsed((v) => !v), []);

  // 刷新页面后回到上次停留的会话。等会话列表到手再恢复，否则无从判断它是否还在
  const restoredRef = useRef(false);
  useEffect(() => {
    if (restoredRef.current || !sessions.length) return;
    restoredRef.current = true;
    const saved = savedSessionRef.current;
    if (saved && sessions.some((s) => s.id === saved)) void switchSession(saved);
  }, [sessions, switchSession]);

  const value = useMemo<Store>(
    () => ({
      messages,
      sessions,
      sessionId,
      streaming,
      streamingSessions,
      unseenReplies,
      ragMode,
      user,
      draft,
      centerView,
      error,
      sidebarWidth,
      inspectorWidth,
      sidebarCollapsed,
      inspectorCollapsed,
      inspectorView,
      selectedFile,
      sendMessage,
      stopStreaming,
      newSession,
      switchSession,
      removeSession,
      compress,
      toggleRag,
      setDraft,
      dismissUnseen,
      setCenterView,
      setSidebarWidth,
      setInspectorWidth,
      toggleSidebar,
      toggleInspector,
      setInspectorView,
      setSelectedFile,
      clearError: () => setError(null),
    }),
    [
      messages, sessions, sessionId, streaming, streamingSessions, unseenReplies,
      ragMode, user, draft, centerView,
      error, sidebarWidth, inspectorWidth, sidebarCollapsed, inspectorCollapsed,
      inspectorView, selectedFile,
      sendMessage, stopStreaming, newSession, switchSession, removeSession,
      compress, toggleRag, setDraft, dismissUnseen, toggleSidebar, toggleInspector,
    ],
  );

  return <StoreContext.Provider value={value}>{children}</StoreContext.Provider>;
}

export function useStore(): Store {
  const ctx = useContext(StoreContext);
  if (!ctx) throw new Error("useStore 必须在 StoreProvider 内使用");
  return ctx;
}
