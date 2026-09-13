"use client";

import { useCallback, useEffect, useState } from "react";
import dynamic from "next/dynamic";
import { Save, RotateCcw, Loader2, Check, AlertCircle, FileText, Wrench, X } from "lucide-react";
import * as api from "@/lib/api";
import { useStore } from "@/lib/store";
import { cn } from "@/lib/utils";
import KnowledgePanel from "./KnowledgePanel";
import SkillUploadBar from "./SkillUploadBar";

// Monaco 依赖浏览器 API，必须禁用 SSR；同时它体积较大，按需加载。
const MonacoEditor = dynamic(() => import("@monaco-editor/react"), {
  ssr: false,
  loading: () => (
    <div className="flex h-full items-center justify-center text-[12px] text-muted-foreground">
      正在加载编辑器…
    </div>
  ),
});

type Status = { kind: "idle" | "saving" | "saved" | "error"; message?: string };

const TABS = [
  { key: "memory", label: "记忆 / 提示词" },
  { key: "skills", label: "技能" },
  { key: "knowledge", label: "知识库" },
] as const;

/** 右侧检查器：文件编辑（记忆/提示词、技能）与知识库语料管理。 */
export default function InspectorPanel() {
  const { inspectorView, setInspectorView, selectedFile, setSelectedFile } = useStore();

  const [skills, setSkills] = useState<api.SkillMeta[]>([]);
  const [content, setContent] = useState("");
  const [dirty, setDirty] = useState(false);
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState<Status>({ kind: "idle" });

  const files =
    inspectorView === "memory"
      ? [
          { name: "MEMORY.md", path: "memory/MEMORY.md" },
          { name: "USER.md", path: "workspace/USER.md" },
          { name: "AGENTS.md", path: "workspace/AGENTS.md" },
          { name: "SOUL.md", path: "workspace/SOUL.md" },
          { name: "IDENTITY.md", path: "workspace/IDENTITY.md" },
        ]
      : skills;

  const reloadSkills = useCallback(async () => {
    try {
      setSkills(await api.listSkills());
    } catch {
      setSkills([]);
    }
  }, []);

  useEffect(() => {
    void reloadSkills();
  }, [reloadSkills]);

  const removeSkill = async (name: string) => {
    try {
      await api.uninstallSkill(name);
      await reloadSkills();
      // 被卸载的技能若正打开在编辑器里，切回内置文件避免指向已删除路径
      if (selectedFile?.includes(`skills/${name}/`)) {
        setSelectedFile("memory/MEMORY.md");
      }
    } catch (e) {
      setStatus({ kind: "error", message: e instanceof Error ? e.message : String(e) });
    }
  };

  const load = useCallback(async (path: string) => {
    setLoading(true);
    setStatus({ kind: "idle" });
    try {
      const r = await api.readFile(path);
      setContent(r.content);
      setDirty(false);
    } catch (e) {
      setContent("");
      setStatus({ kind: "error", message: e instanceof Error ? e.message : String(e) });
    } finally {
      setLoading(false);
    }
  }, []);

  // 切到文件类标签时载入；知识库标签不涉及编辑器
  useEffect(() => {
    if (inspectorView !== "knowledge" && selectedFile) void load(selectedFile);
  }, [selectedFile, load, inspectorView]);

  const save = async () => {
    if (!selectedFile) return;
    setStatus({ kind: "saving" });
    try {
      await api.saveFile(selectedFile, content);
      setDirty(false);
      setStatus({ kind: "saved" });
    } catch (e) {
      setStatus({ kind: "error", message: e instanceof Error ? e.message : String(e) });
    }
  };

  const isKnowledge = inspectorView === "knowledge";

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* ---- 顶部：标签切换 + 保存 ---- */}
      <div className="flex items-center justify-between border-b border-border px-3 pt-3 pb-2">
        <div className="flex gap-1">
          {TABS.map((t) => (
            <button
              key={t.key}
              onClick={() => setInspectorView(t.key)}
              className={cn(
                "rounded-md px-2.5 py-1 text-[11px] font-medium transition",
                inspectorView === t.key
                  ? "bg-accent-soft text-accent"
                  : "text-muted-foreground hover:bg-black/[0.04]",
              )}
            >
              {t.label}
            </button>
          ))}
        </div>

        {!isKnowledge && (
          <div className="flex items-center gap-1">
            {dirty && (
              <button
                onClick={() => selectedFile && void load(selectedFile)}
                title="放弃修改并重新载入"
                className="rounded-md p-1 text-muted-foreground transition hover:bg-black/[0.06] hover:text-foreground"
              >
                <RotateCcw size={13} />
              </button>
            )}
            <button
              onClick={() => void save()}
              disabled={!dirty || status.kind === "saving"}
              title="保存（保存 MEMORY.md 会触发记忆索引重建）"
              className="flex items-center gap-1 rounded-md bg-accent px-2.5 py-1 text-[11px] font-medium text-accent-foreground transition hover:bg-accent-hover disabled:cursor-not-allowed disabled:opacity-35"
            >
              {status.kind === "saving" ? (
                <Loader2 size={12} className="animate-spin" />
              ) : (
                <Save size={12} />
              )}
              保存
            </button>
          </div>
        )}
      </div>

      {isKnowledge ? (
        <KnowledgePanel />
      ) : (
        <>
          {/* ---- 技能包上传 ---- */}
          {inspectorView === "skills" && (
            <SkillUploadBar onInstalled={() => void reloadSkills()} />
          )}

          {/* ---- 文件选择 ---- */}
          <nav className="flex flex-wrap gap-1 border-b border-border px-3 py-2">
            {files.length === 0 && inspectorView === "skills" && (
              <span className="py-1 text-[11px] text-muted-foreground">
                尚无技能。上传技能包后这里会列出各自的 SKILL.md。
              </span>
            )}
            {files.map((f) => (
              <span
                key={f.path}
                className={cn(
                  "group flex items-center gap-1 rounded-md font-mono text-[10.5px] transition",
                  selectedFile === f.path
                    ? "bg-black/[0.07] text-foreground"
                    : "text-muted-foreground hover:bg-black/[0.04]",
                )}
              >
                <button
                  onClick={() => setSelectedFile(f.path)}
                  className="flex items-center gap-1 py-1 pl-2 pr-1"
                >
                  {inspectorView === "skills" ? <Wrench size={10} /> : <FileText size={10} />}
                  {f.name}
                </button>
                {inspectorView === "skills" && (
                  <button
                    onClick={() => void removeSkill(f.name)}
                    title={`卸载技能 ${f.name}`}
                    className="rounded p-0.5 pr-1.5 opacity-0 transition group-hover:opacity-100 hover:text-danger"
                  >
                    <X size={10} />
                  </button>
                )}
              </span>
            ))}
          </nav>

          {/* ---- 编辑器 ---- */}
          <div className="flex min-h-0 flex-1 flex-col">
            {loading ? (
              <div className="flex flex-1 items-center justify-center text-[12px] text-muted-foreground">
                载入中…
              </div>
            ) : (
              <MonacoEditor
                height="100%"
                language={selectedFile?.endsWith(".md") ? "markdown" : "python"}
                theme="light"
                value={content}
                onChange={(v) => {
                  setContent(v ?? "");
                  setDirty(true);
                  setStatus({ kind: "idle" });
                }}
                options={{
                  fontSize: 12.5,
                  lineHeight: 20,
                  minimap: { enabled: false },
                  wordWrap: "on",
                  scrollBeyondLastLine: false,
                  renderLineHighlight: "none",
                  overviewRulerLanes: 0,
                  scrollbar: { verticalScrollbarSize: 8, horizontalScrollbarSize: 8 },
                  padding: { top: 10, bottom: 10 },
                }}
              />
            )}
          </div>

          {/* ---- 状态条 ---- */}
          <div className="flex h-7 items-center gap-1.5 border-t border-border px-3 text-[10.5px]">
            {status.kind === "saved" && (
              <>
                <Check size={11} className="text-success" />
                <span className="text-success">已保存</span>
              </>
            )}
            {status.kind === "error" && (
              <>
                <AlertCircle size={11} className="text-danger" />
                <span className="truncate text-danger">{status.message}</span>
              </>
            )}
            {status.kind === "idle" && (
              <span className="text-muted-foreground">
                {dirty ? "有未保存的修改" : selectedFile ?? "未选择文件"}
              </span>
            )}
          </div>
        </>
      )}
    </div>
  );
}
