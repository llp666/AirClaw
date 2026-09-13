/** AirClaw API 类型定义。与后端 SSE 事件、会话文件格式一一对应。 */

export type Role = "user" | "assistant";

/** 一次工具调用的完整记录。input/output 与后端会话文件中的 tool_calls 结构一致。 */
export interface ToolCall {
  tool: string;
  input: unknown;
  output: string;
}

/** RAG 检索结果（retrieval 事件） */
export interface Retrieval {
  query: string;
  results: {
    text: string;
    source: string;
    score?: number;
    bm25_score?: number;
    vector_score?: number;
  }[];
  note?: string;
}

/** 审查告警（audit_warning 事件） */
export interface AuditWarning {
  rule_id: string;
  reason: string;
  severity: "high" | "medium" | "low";
  matched?: string;
  /** false 表示仅告警未阻断（flag 规则），undefined/true 表示已阻断 */
  blocked?: boolean;
  tool?: string;
}

export interface Message {
  id: string;
  role: Role;
  content: string;
  /** 模型思考过程分片，按到达顺序拼接展示 */
  reasoning: string;
  toolCalls: ToolCall[];
  retrievals: Retrieval[];
  warnings: AuditWarning[];
  streaming?: boolean;
  error?: string;
  /** 秒级时间戳。历史消息来自会话文件，实时消息在收完 done 时打点。 */
  ts?: number;
}

export interface SessionMeta {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  message_count: number;
}

export interface SkillMeta {
  name: string;
  path: string;
}

export interface TokenStats {
  session_id: string;
  system_tokens: number;
  message_tokens: number;
  summary_tokens: number;
  total_tokens: number;
  message_count: number;
}

export interface SystemPromptStats {
  total_tokens: number;
  characters: number;
  components: { tag: string; path: string; chars: number; tokens: number }[];
  rag_mode: boolean;
}

/** 后端 SSE 事件的联合类型 */
export type StreamEvent =
  | { type: "token"; content: string }
  | { type: "reasoning"; content: string }
  | { type: "tool_start"; tool: string; input: unknown }
  | { type: "tool_end"; tool: string; input: unknown; output: string }
  | { type: "audit_warning"; rule_id: string; reason: string; severity: AuditWarning["severity"]; matched?: string; blocked?: boolean; tool?: string }
  | { type: "retrieval"; query: string; results: Retrieval["results"]; note?: string }
  | { type: "new_response" }
  | { type: "done"; content: string; session_id: string; tool_calls?: ToolCall[] }
  | { type: "title"; session_id: string; title: string }
  | { type: "error"; error: string };
