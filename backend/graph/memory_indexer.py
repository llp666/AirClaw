"""MEMORY.md 的检索索引。

对应 README「memory_indexer.py — MEMORY.md 向量索引」。解决的问题是：长期记忆
整篇注入 System Prompt，在文件长大后会持续吃掉上下文；RAG 模式下改为按需检索
相关片段注入。

## 什么时候才需要它

MEMORY.md 是几 KB 的时候，整篇注入比检索更划算——切块后按 top-k 检索，一个 1KB
的文件会被切成 4 块、只取回 3 块，等于**静默丢掉四分之一**。所以只有在文件超过
`MEMORY_INLINE_MAX_CHARS`（默认 4000 字符）时才真的走检索，见
`Settings.memory_inline_ok()`。阈值以下的文件一律整篇注入，与普通模式无异。

## 与知识库索引的三点不同

  1. 语料只有 MEMORY.md 一个文件，不需要 SimpleDirectoryReader
  2. 变更检测用内容 MD5 而非 mtime——MEMORY.md 由 Agent 频繁覆写，mtime 的变化
     远多于内容的实际变化，用 mtime 会反复重建索引、白调 Embedding 服务
  3. **按条目切块，而不是按固定字符数切**。MEMORY.md 一份文件里有几十条互不相干的
     记忆，按 256 字符硬切会把三条捆进一个块，检索时具体那条被稀释——实测七条查询
     有四次捞不到正确条目。改为一条记忆一个块（超长的再按 256 字符细分），
     检索粒度才对得上「一条记忆」这个语义单位

## 检索方式

与知识库一致：BM25 关键词 + 向量语义，倒数排名融合（RRF）。理由也一样——记忆
条目里全是项目代号、固定路径、命名规范这类精确词，纯语义检索容易漏掉。分词器
直接复用 tools/knowledge_index.tokenize，避免维护两套中文分词规则。

向量服务不可用时降级为纯 BM25，并在返回中如实标注，不静默失败。

一个候选要被留下，须满足两者之一：余弦相似度达到 `Settings.retrieval_min_score`，
或它的原文里出现了查询的「实词」（`literal_tokens`）。全部不满足时返回空结果并把
模式标为 `low_relevance`。没有这道闸，检索永远会返回 top-k——而 `RAG_GUIDANCE`
告诉模型「若无相关片段，说明记忆中确实没有对应内容」，那句话只有在闸门存在时才成立。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

import bm25s
from llama_index.core import StorageContext, VectorStoreIndex, load_index_from_storage
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.schema import TextNode
from llama_index.core.vector_stores import VectorStoreQuery

from config import Settings
from graph.embedding import build_embedding, vector_summary
from tools.knowledge_index import discriminative_literal_tokens, tokenize

logger = logging.getLogger("airclaw.memory")

# README 规定的切分参数：仅用于超长条目的二次细分，正常情况下一条记忆就是一个块
CHUNK_SIZE = 256
CHUNK_OVERLAP = 32
RRF_K = 60

_SECTION_RE = re.compile(r"^##[ \t]+(?P<title>.+?)[ \t]*$", re.MULTILINE)
_ENTRY_RE = re.compile(r"^###[ \t]+(?P<title>.+?)[ \t]*$", re.MULTILINE)


def _split_by(text: str, pattern: re.Pattern[str]) -> list[tuple[str, str]]:
    """按给定级别的标题把正文切成 [(标题, 正文)]。

    无匹配标题时整体作为一条返回，标题为空串。标题前的内容也单独成一条，
    以免文件头说明被丢掉。
    """
    matches = list(pattern.finditer(text))
    if not matches:
        return [("", text)]

    out: list[tuple[str, str]] = []
    if matches[0].start() > 0:
        out.append(("", text[: matches[0].start()]))
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out.append((match.group("title"), text[match.end() : end]))
    return out


def split_sections(text: str) -> list[tuple[str, str]]:
    """按二级标题切分为 [(小节标题, 正文)]。"""
    return _split_by(text, _SECTION_RE)


def split_entries(body: str) -> list[tuple[str, str]]:
    """按三级标题切分为 [(条目标题, 条目正文)]。"""
    return _split_by(body, _ENTRY_RE)


class MemoryIndexer:
    """MEMORY.md 的混合检索索引。惰性加载，内容变更时自动重建。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._dir = settings.storage_path / "memory_index"
        self._nodes_file = self._dir / "nodes.json"
        self._manifest_file = self._dir / "manifest.json"
        self._vector_dir = self._dir / "vector"

        self._nodes: list[TextNode] | None = None
        self._bm25: bm25s.BM25 | None = None
        self._vector: VectorStoreIndex | None = None
        self._vector_ok = False
        self._embed_model: Any = None

    def _embedder(self) -> Any:
        """复用同一个 embedding 实例。

        `build_embedding()` 每次都会新建一个 `httpx.Client`，连接池随之作废。
        实测每次查询因此多付约 1.8 秒的 TLS 握手（2103ms vs 331ms）——
        向量查询是每轮对话都可能走的路径，这个开销不能按次付。
        惰性创建：向量分支不可用时不该白白建立客户端。
        """
        if self._embed_model is None:
            self._embed_model = build_embedding()
        return self._embed_model

    @property
    def _memory_file(self) -> Path:
        return self._settings.memory_path / "MEMORY.md"

    # ---- 语料状态 ----

    def _md5(self) -> str | None:
        """MEMORY.md 内容的 MD5。文件不存在时返回 None。

        按**规范化后的文本**取哈希，而非原始字节：Windows 上 Path.write_text 会把
        \\n 翻成 \\r\\n，同一份内容在不同平台落盘字节不同。用原始字节会让索引在
        换行被翻动的每一次保存后都白重建一遍——而改用 MD5 正是为了少重建。
        """
        if not self._memory_file.is_file():
            return None
        try:
            text = self._memory_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning("读取 MEMORY.md 失败：%s", exc)
            return None
        return hashlib.md5(text.encode("utf-8")).hexdigest()

    def _manifest(self) -> dict[str, Any]:
        if not self._manifest_file.is_file():
            return {}
        try:
            return json.loads(self._manifest_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    @property
    def is_built(self) -> bool:
        return self._nodes_file.is_file() and bool(self._manifest())

    @property
    def needs_rebuild(self) -> bool:
        return not self.is_built or self._manifest().get("md5") != self._md5()

    # ---- 构建 ----

    def rebuild_index(self, *, force: bool = True) -> dict[str, Any]:
        """重建索引并持久化。force=False 且内容未变时跳过。"""
        if self.is_built and not force and not self.needs_rebuild:
            return {"built": False, "reason": "MEMORY.md 未变更", **self._manifest()}

        self._dir.mkdir(parents=True, exist_ok=True)

        text = ""
        if self._memory_file.is_file():
            text = self._memory_file.read_text(encoding="utf-8")

        # 一条记忆一个块：先按二级标题分节，再按三级标题分条目。
        # 只有超长条目才交给 SentenceSplitter 二次细分。
        nodes: list[TextNode] = []
        splitter = SentenceSplitter(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)
        for section, body in split_sections(text):
            for entry, content in split_entries(body):
                content = content.strip()
                if not content:
                    continue
                # 块内带上所属小节与条目名，片段脱离原文件也说得清自己是什么
                head = (f"## {section}\n" if section else "") + (
                    f"### {entry}\n" if entry else ""
                )
                meta = {"source": "MEMORY.md", "section": section, "entry": entry}
                pieces = (
                    [content]
                    if len(content) <= CHUNK_SIZE
                    else splitter.split_text(content)
                )
                for piece in pieces:
                    if piece.strip():
                        nodes.append(
                            TextNode(text=f"{head}{piece.strip()}", metadata=meta)
                        )

        self._bm25 = bm25s.BM25()
        if nodes:
            self._bm25.index([tokenize(n.text) for n in nodes], show_progress=False)

        self._vector_ok = False
        self._vector = None
        if nodes:
            try:
                self._vector = VectorStoreIndex(nodes, embed_model=self._embedder())
                self._vector.storage_context.persist(persist_dir=str(self._vector_dir))
                self._vector_ok = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("记忆向量索引构建失败，降级为纯 BM25 关键词检索：%s", exc)

        self._nodes = nodes
        self._nodes_file.write_text(
            json.dumps(
                [{"id": n.node_id, "text": n.text, "metadata": n.metadata} for n in nodes],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        manifest = {
            "md5": self._md5(),
            "built_at": time.time(),
            "vector_ok": self._vector_ok,
            "chunks": len(nodes),
        }
        self._manifest_file.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(
            "记忆索引重建完成：%d 个分块，向量分支=%s",
            len(nodes),
            "可用" if self._vector_ok else "不可用",
        )
        return {"built": True, **manifest}

    def _maybe_rebuild(self) -> None:
        """检索前确认索引是否过期。内容没变就不重建，避免白调 Embedding 服务。"""
        if self.needs_rebuild:
            logger.info("检测到 MEMORY.md 变更，重建记忆索引")
            self.rebuild_index(force=True)

    # ---- 加载 ----

    def _ensure_loaded(self) -> None:
        if self._nodes is not None:
            return
        if not self.is_built:
            self.rebuild_index(force=True)

        try:
            raw = json.loads(self._nodes_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raw = []
        self._nodes = [
            TextNode(id_=r["id"], text=r["text"], metadata=r.get("metadata", {}))
            for r in raw
        ]

        self._bm25 = bm25s.BM25()
        if self._nodes:
            self._bm25.index([tokenize(n.text) for n in self._nodes], show_progress=False)

        self._vector_ok = False
        self._vector = None
        if self._nodes and self._manifest().get("vector_ok") and self._vector_dir.is_dir():
            try:
                # 与知识库同理：必须用 load_index_from_storage，from_vector_store
                # 只取向量存储，而节点文本在 docstore 里，会报 "does not store text"
                storage = StorageContext.from_defaults(persist_dir=str(self._vector_dir))
                self._vector = load_index_from_storage(
                    storage, embed_model=self._embedder()
                )
                self._vector_ok = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "记忆向量索引加载失败，降级为纯 BM25（可用 rebuild 重建）：%s", exc
                )

    # ---- 检索 ----

    def _bm25_hits(self, query: str, k: int) -> list[tuple[str, float]]:
        if not self._nodes or self._bm25 is None:
            return []
        results, scores = self._bm25.retrieve(
            [tokenize(query)], k=min(k, len(self._nodes)), show_progress=False
        )
        return [
            (self._nodes[int(idx)].node_id, float(score))
            for idx, score in zip(results[0], scores[0])
        ]

    def _vector_hits(
        self, query: str, k: int, query_embedding: list[float] | None = None
    ) -> list[tuple[str, float]]:
        """向量召回。

        query_embedding 可外部传入以避免重复调用 Embedding 服务——诊断页既要分数又要
        展示查询向量，若各算一次，屏幕上显示的就不是实际参与打分的那条（服务本身有
        微小抖动）。传入同一条即可保证两者对应。
        """
        if not self._vector_ok or self._vector is None:
            return []
        qv = (
            query_embedding
            if query_embedding is not None
            else self._embedder().get_query_embedding(query)
        )
        result = self._vector.vector_store.query(
            VectorStoreQuery(query_embedding=qv, similarity_top_k=k)
        )
        return [
            (node_id, float(score))
            for node_id, score in zip(result.ids or [], result.similarities or [])
        ]

    def _rank(
        self, query: str, fetch: int, query_embedding: list[float] | None = None
    ) -> tuple[list[tuple[str, float]], dict[str, float], dict[str, float], str]:
        """两路召回并融合。返回 (按融合分降序的候选, bm25 分, 向量分, 模式)。

        检索与诊断共用这一条路径，保证诊断页看到的分数与生产检索完全一致。
        """
        bm_hits = self._bm25_hits(query, fetch)
        vec_hits = self._vector_hits(query, fetch, query_embedding=query_embedding)

        if vec_hits:
            mode = "hybrid"
        elif bm_hits:
            mode = "bm25"
        else:
            return [], {}, {}, "bm25"

        fused: dict[str, float] = {}
        for hits in (bm_hits, vec_hits):
            for rank, (node_id, _score) in enumerate(hits):
                fused[node_id] = fused.get(node_id, 0.0) + 1.0 / (RRF_K + rank + 1)

        ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)
        return ranked, dict(bm_hits), dict(vec_hits), mode

    def retrieve(self, query: str, top_k: int = 3) -> tuple[list[dict], str]:
        """混合检索。返回 (结果列表, 实际使用的检索模式)。"""
        self._maybe_rebuild()
        self._ensure_loaded()
        if not self._nodes:
            return [], "empty"

        ranked, bm_scores, vec_scores, mode = self._rank(query, max(top_k * 2, 6))
        if not ranked:
            return [], mode

        # 相关性下限：融合分数只反映排名，不能拿来当阈值（见 Settings.retrieval_min_score）。
        #
        # 两条通道任一达标即保留：
        #   向量通道 —— 语义相近即可，管口语化提问
        #   字面通道 —— 整词或二元组出现在原文里，管标准号、型号这类语义相似度偏低
        #               但必须命中的精确词
        # 字面通道只认 discriminative_literal_tokens：整词与二元组、不含单个汉字，
        # 且在语料里要有区分度。单字命中在中文里几乎全是噪声，而 BM25 的长度归一化
        # 会把它放大到和标准号同一量级，靠阈值切不开；「这是」这类虚词二元组同理。
        floor = self._settings.retrieval_min_score
        by_id = {n.node_id: n for n in self._nodes}
        strong = discriminative_literal_tokens(query, [n.text for n in self._nodes])

        keep: list[str] = []
        for node_id, _ in ranked:
            if vec_scores.get(node_id, 0.0) >= floor:
                keep.append(node_id)
                continue
            if any(tok in by_id[node_id].text.lower() for tok in strong):
                keep.append(node_id)

        if not keep:
            # 检索执行过了，只是没有任何候选达到下限——与「索引为空」是两回事，
            # 前端据此显示不同的话术。
            return [], "low_relevance"

        # ranked 本就按融合分降序，过滤即保序，无需重排
        keep_set = set(keep)
        ranked = [(node_id, rrf) for node_id, rrf in ranked if node_id in keep_set][:top_k]

        out: list[dict] = []
        for node_id, rrf in ranked:
            node = by_id.get(node_id)
            if node is None:
                continue
            out.append(
                {
                    "text": node.text,
                    "score": round(rrf, 6),
                    "source": node.metadata.get("source", "MEMORY.md"),
                    "section": node.metadata.get("section", ""),
                    "entry": node.metadata.get("entry", ""),
                    "bm25_score": round(bm_scores.get(node_id, 0.0), 4),
                    "vector_score": round(vec_scores.get(node_id, 0.0), 4),
                }
            )
        return out, mode

    # ---- 诊断 ----

    def inspect(self, query: str, top_k: int = 8) -> dict[str, Any]:
        """诊断用：跑一次完整检索，把**全部候选**（含未过闸的）连同分数与向量一起返回。

        与 retrieve() 只有两点差别：不做相关性过滤、附带每个候选自身的向量。
        分数走同一条 `_rank` 路径，故与生产检索看到的完全一致——诊断页若用另一套
        计算，管理员据此调的阈值就会和生产对不上。
        """
        self._maybe_rebuild()
        self._ensure_loaded()

        floor = self._settings.retrieval_min_score
        if not self._nodes:
            return {
                "mode": "empty", "chunk_count": 0, "vector_available": False,
                "min_score": floor, "strong_tokens": [],
                "query_vector": vector_summary([]), "candidates": [],
            }

        query_vec: list[float] = []
        if self._vector_ok:
            try:
                query_vec = list(self._embedder().get_query_embedding(query))
            except Exception as exc:  # noqa: BLE001 — 取不到向量不该让整页失败
                logger.warning("诊断页获取查询向量失败：%s", exc)

        # 查询向量只算一次：既用于召回打分，也用于页面展示。分两次调用会因服务端
        # 微小抖动而拿到不同的向量，屏幕上显示的就不是实际参与打分的那条。
        ranked, bm_scores, vec_scores, mode = self._rank(
            query, max(top_k * 3, 20), query_embedding=query_vec or None
        )
        by_id = {n.node_id: n for n in self._nodes}
        strong = discriminative_literal_tokens(query, [n.text for n in self._nodes])
        store = self._vector.vector_store if (self._vector_ok and self._vector) else None

        candidates: list[dict[str, Any]] = []
        for node_id, rrf in ranked[:top_k]:
            node = by_id.get(node_id)
            if node is None:
                continue
            vec_score = float(vec_scores.get(node_id, 0.0))
            bm_score = float(bm_scores.get(node_id, 0.0))
            literal = any(tok in node.text.lower() for tok in strong)

            chunk_vec: list[float] = []
            if store is not None:
                try:
                    chunk_vec = list(store.get(node_id) or [])
                except Exception:  # noqa: BLE001
                    chunk_vec = []

            candidates.append(
                {
                    "text": node.text,
                    "source": node.metadata.get("source", "MEMORY.md"),
                    "section": node.metadata.get("section", ""),
                    "entry": node.metadata.get("entry", ""),
                    "vector_score": round(vec_score, 4),
                    "bm25_score": round(bm_score, 4),
                    "rrf_score": round(rrf, 6),
                    "kept": vec_score >= floor or literal,
                    "hit": "vector" if vec_score >= floor else ("literal" if literal else None),
                    "vector": vector_summary(chunk_vec),
                }
            )

        return {
            "mode": mode,
            "chunk_count": len(self._nodes),
            "vector_available": self._vector_ok,
            "min_score": floor,
            "strong_tokens": strong,
            "query_vector": vector_summary(query_vec),
            "candidates": candidates,
        }

    def stats(self) -> dict[str, Any]:
        self._ensure_loaded()
        return {
            "chunks": len(self._nodes or []),
            "vector_available": self._vector_ok,
            "built_at": self._manifest().get("built_at"),
            "md5": self._manifest().get("md5"),
        }
