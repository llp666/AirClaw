"""知识库索引 —— BM25 关键词检索 + 向量语义检索的混合检索。

对应 PRD 第二章第 4 项「RAG 检索工具」与 README「search_knowledge_tool」：
扫描 knowledge/ 下的语料，构建本地索引并持久化到 storage/，混合检索取 top-k。

## 中文分词

llama-index 的 BM25Retriever 默认 token_pattern 为 `(?u)\\b\\w\\w+\\b`，
中文没有词边界，整句会被当成一个 token，导致**所有中文查询得分恒为 0**
（实测：英文查询得分正常 0.69~0.84，中文查询一律 0.000）。
其 `tokenizer` 参数在本版本已废弃且不生效，故不使用 BM25Retriever，
直接调用底层的 bm25s 并自行分词。

分词策略为混合式，不依赖 jieba：

    ASCII 串（型号编号 / 标准号 / 指标值）—— 整词保留，如 GJB9001C、240MPa、6061
    CJK 串                              —— 单字 + 相邻二元组，无需词典即可覆盖未登录术语

选择它而非 jieba 的理由：军工语料充满型号编号与标准号，整词保留比切碎更利于精确匹配；
且省去一个约 19MB 的第三方依赖（内网部署需逐个过保密审查）。
分词器是可替换的单一函数，如需换回 jieba 只需改 tokenize()。

## 向量检索

依赖 Embedding 服务。服务不可用时**自动降级为纯 BM25**，并在结果中如实标注，
不静默失败。

## 表格语料（.xlsx / .csv）

表格与文档走不同的加载与切块路径，见 `load_table_documents`：表格的语义单位是
「行」而不是「段落」，按字符切会把一行数据拦腰截断，且表头只留在第一块里。
改为每 20 行一块、每块重复「列名: 值」之后，命中粒度是「某表第 12-31 行」，
既看得懂，也让 BM25 与字面闸门能精确命中牌号、标准号这类键。

## 相关性下限

一个候选要被留下，须满足两者之一：余弦相似度达到 `Settings.retrieval_min_score`，
或它的原文里出现了查询的「实词」（`literal_tokens`：ASCII 整词 + CJK 二元组，
**不含单个汉字**）。两条都不满足即视为不相关；若全部候选都不满足，返回空结果并把
模式标为 `low_relevance`。

语义检索在向量空间里总能找到「最近的邻居」，没有这道闸就永远会返回 top-k，
哪怕一条都不相关。而单字命中的门槛必须单独处理——详见 `literal_tokens` 的说明。
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
from pathlib import Path
from typing import Any

import bm25s
import pandas as pd
from llama_index.core import (
    Document,
    SimpleDirectoryReader,
    StorageContext,
    VectorStoreIndex,
    load_index_from_storage,
)
from llama_index.core.node_parser import SentenceSplitter
from llama_index.core.schema import TextNode
from llama_index.core.vector_stores import VectorStoreQuery

from config import Settings
from graph.embedding import build_embedding, vector_summary

logger = logging.getLogger("airclaw.knowledge")

# 支持的语料扩展名
SUPPORTED_SUFFIXES = {".txt", ".md", ".pdf", ".docx", ".xlsx", ".csv"}

#: 表格类语料。它们的语义单位是「行」而不是「段落」，走另一条加载与切块路径
TABLE_SUFFIXES = {".xlsx", ".csv"}

#: 表格每个分块包含多少行。命中会精确到「某表第 12-31 行」
TABLE_ROWS_PER_CHUNK = 20

CHUNK_SIZE = 512
CHUNK_OVERLAP = 64
RRF_K = 60  # 倒数排名融合常数

_ASCII_RE = r"[A-Za-z0-9][A-Za-z0-9_.\-/]*"
_CJK_RE = r"[一-鿿]+"
_TOKEN_RE = re.compile(f"{_ASCII_RE}|{_CJK_RE}")


def tokenize(text: str) -> list[str]:
    """混合分词：ASCII 整词 + CJK 字符与二元组。"""
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        seg = match.group(0)
        if seg[0].isascii():
            tokens.append(seg.lower())
        else:
            tokens.extend(seg)
            tokens.extend(seg[i : i + 2] for i in range(len(seg) - 1))
    return tokens


def literal_tokens(text: str) -> list[str]:
    """检索查询里够「实」的字面 token：ASCII 整词 + CJK 二元组，**不含单个汉字**。

    用于相关性判定里的「字面命中」通道。单个汉字命中在中文里几乎全是噪声
    （的、一、是、我、期），而 BM25 的长度归一化又会把它放大——实测「帮我写一首诗」
    靠一个「一」字拿到 BM25 1.31，与标准号 GJB1580A 的 1.26 基本相当，
    靠调阈值根本切不开。故这条通道只认整词与二元组。

    索引与排序仍旧用完整的 tokenize（单字对召回仍有贡献），只有判定闸门用这个。
    """
    return [t for t in tokenize(text) if t.isascii() or len(t) >= 2]


def discriminative_literal_tokens(text: str, corpus: list[str]) -> list[str]:
    """在 `literal_tokens` 基础上，滤掉在语料里出现得过于普遍的 token。

    布尔式的「是否出现在原文里」丢掉了频率信息：像「这是」「的气」这类由虚词组成的
    二元组，在多数分块里都出现，完全没有区分度，却足以让无关查询通过字面闸门
    （实测「这是怎么做的」靠「这是」命中了占位条目）。BM25 靠 IDF 处理这件事，
    而闸门手上没有分数可用，故这里补一次词频判断。

    出现在**超过半数**分块里的 token 视为无区分度，从闸门里剔除。
    """
    tokens = literal_tokens(text)
    if not tokens or not corpus:
        return tokens
    lowered = [c.lower() for c in corpus]
    half = len(corpus) / 2
    return [t for t in tokens if sum(t in c for c in lowered) <= half]


# --------------------------------------------------------------------------
# 表格语料
# --------------------------------------------------------------------------


def _cell(value: Any) -> str:
    """单元格转文本。空值返回空串；整数值的浮点数去掉小数点（310.0 → 310）。"""
    if value is None:
        return ""
    if isinstance(value, float):
        if math.isnan(value):
            return ""
        if value.is_integer():
            return str(int(value))
    text = str(value).strip()
    return "" if text.lower() in {"nan", "nat"} else text


def _read_csv(path: Path) -> Any:
    """读 CSV。中文内网里 GBK/GB18030 编码的表格很常见，故逐个试。"""
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return pd.read_csv(path, encoding=encoding)
        except UnicodeDecodeError:
            continue
        except Exception as exc:  # noqa: BLE001 — 解析失败不该中断整个构建
            logger.warning("读取 CSV 失败，已跳过：%s（%s）", path.name, exc)
            return None
    logger.warning("CSV 编码无法识别（试过 utf-8 / gb18030），已跳过：%s", path.name)
    return None


def _render_table(df: Any, file_name: str, sheet: str | None) -> list[Document]:
    """把一张表按行分组渲染成文本块，每块重复列名。"""
    df = df.dropna(how="all")
    if df.empty:
        return []

    columns = [str(c).strip() or f"列{i + 1}" for i, c in enumerate(df.columns)]
    total = len(df)
    docs: list[Document] = []

    for start in range(0, total, TABLE_ROWS_PER_CHUNK):
        block = df.iloc[start : start + TABLE_ROWS_PER_CHUNK]
        where = file_name + (f" · {sheet}" if sheet else "")
        where += f" · 第 {start + 1}-{start + len(block)} 行（共 {total} 行）"

        lines = [where]
        for _, row in block.iterrows():
            pairs = []
            for name, value in zip(columns, row):
                text = _cell(value)
                if text:
                    pairs.append(f"{name}: {text}")
            if pairs:
                lines.append(" | ".join(pairs))

        if len(lines) > 1:  # 整块全空就丢弃
            docs.append(
                Document(
                    text="\n".join(lines),
                    metadata={
                        "file_name": file_name,
                        "sheet": sheet or "",
                        "row_start": start + 1,
                        "row_end": start + len(block),
                    },
                )
            )
    return docs


def load_table_documents(root: Path) -> list[Document]:
    """把 .xlsx / .csv 读成按行分组的「列名: 值」文本块。

    刻意不用 SimpleDirectoryReader 的 PandasExcelReader：它把整个工作表转成一张
    markdown 表塞进**一个** Document，大表随后会被按字符切分成互不相关的碎片，
    而且表头只存在于第一块里，检索到的片段看不出列含义。

    改为每 TABLE_ROWS_PER_CHUNK 行一块之后：

      - 命中粒度是「某表第 12-31 行」，人看得懂，也便于回表核对
      - 「列名: 值」的形态让 BM25 与字面闸门能精确命中牌号、标准号这类词
        （纯 markdown 表格行只有裸值，`6061-T6` 与列名的对应关系会丢失）
      - 每行自带列名，片段脱离原表也说得清哪一列是什么
    """
    docs: list[Document] = []
    for path in sorted(root.rglob("*")):
        suffix = path.suffix.lower()
        if not path.is_file() or suffix not in TABLE_SUFFIXES:
            continue

        sheets: list[tuple[str | None, Any]] = []
        try:
            if suffix == ".csv":
                frame = _read_csv(path)
                if frame is not None:
                    sheets = [(None, frame)]
            else:
                sheets = list(pd.read_excel(path, sheet_name=None).items())
        except Exception as exc:  # noqa: BLE001 — 单份表格出错不该拖垮整个知识库
            logger.warning("读取表格失败，已跳过：%s（%s）", path.name, exc)
            continue

        for sheet, frame in sheets:
            if frame is None or frame.empty:
                continue
            docs.extend(_render_table(frame, path.name, sheet))

    return docs


class KnowledgeIndex:
    """knowledge/ 语料的混合索引。惰性加载，文件变更时自动重建。"""


    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._dir = settings.storage_path / "knowledge_index"
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

    # ---- 语料状态 ----

    def _corpus_signature(self) -> dict[str, Any]:
        """语料指纹：文件数与最新修改时间。用于判断是否需要重建。"""
        files = [
            p
            for p in self._settings.knowledge_path.rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES
        ]
        return {
            "count": len(files),
            "mtime": max((p.stat().st_mtime for p in files), default=0.0),
            "files": sorted(p.name for p in files),
        }

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
        current = self._corpus_signature()
        saved = self._manifest()
        return (
            not saved
            or saved.get("count") != current["count"]
            or abs(saved.get("mtime", 0) - current["mtime"]) > 1e-6
        )

    # ---- 构建 ----

    def _load_documents(self, root: Path) -> tuple[list[Document], list[str]]:
        """加载全部语料。返回 (文档, 读失败的文件名)。

        逐个文件加载，而不是把文件列表一次性交给 SimpleDirectoryReader：任何一份
        语料读不动（缺可选依赖、文件损坏）都会让整次构建抛出，连带已经可用的索引
        也一并作废——表现为「上传一份 Word 之后知识库整体不可检索」。表格路径
        一直是这样逐份兜错的（见 load_table_documents），文档路径与之对齐。

        读不动的文件只跳过并记录，不静默：`skipped` 会写进 manifest，
        否则「这份标准其实没进索引」没有任何痕迹，Agent 只会回答「未找到」。

        表格另走一条路径（`load_table_documents`）：它的语义单位是「行」，
        理由见该函数的说明。
        """
        docs: list[Document] = []
        skipped: list[str] = []
        plain = sorted(
            p
            for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() in (SUPPORTED_SUFFIXES - TABLE_SUFFIXES)
        )
        for path in plain:
            # 显式传 input_files：SimpleDirectoryReader 在目录下无匹配文件时会抛异常，
            # 而空知识库必须当作「构建成功但没内容」处理
            try:
                docs.extend(
                    SimpleDirectoryReader(input_files=[str(path)]).load_data()
                )
            except Exception as exc:  # noqa: BLE001 — 单份语料出错不该拖垮整个知识库
                logger.warning("读取语料失败，已跳过：%s（%s）", path.name, exc)
                skipped.append(path.name)

        docs.extend(load_table_documents(root))
        return docs, skipped

    def build(self, *, force: bool = False) -> dict[str, Any]:
        """扫描 knowledge/ 构建索引并持久化。

        空语料库也视为构建成功（写入空 manifest），以便区分
        「尚未构建」与「构建了但没内容」。
        """
        if self.is_built and not force and not self.needs_rebuild:
            return {"built": False, "reason": "语料未变更", **self._manifest()}

        knowledge = self._settings.knowledge_path
        knowledge.mkdir(parents=True, exist_ok=True)
        self._dir.mkdir(parents=True, exist_ok=True)

        documents, skipped = self._load_documents(knowledge)

        if documents:
            nodes = SentenceSplitter(
                chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP
            ).get_nodes_from_documents(documents)
        else:
            nodes = []

        # BM25 分支（本地，必定可用）
        self._bm25 = bm25s.BM25()
        if nodes:
            self._bm25.index([tokenize(n.text) for n in nodes], show_progress=False)

        # 向量分支（依赖外部服务，失败不阻断构建）
        self._vector_ok = False
        self._vector = None
        if nodes:
            try:
                embed_model = self._embedder()
                self._vector = VectorStoreIndex(nodes, embed_model=embed_model)
                self._vector.storage_context.persist(persist_dir=str(self._vector_dir))
                self._vector_ok = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "向量索引构建失败，将降级为纯 BM25 关键词检索：%s", exc
                )

        # 持久化 nodes 与 manifest
        self._nodes = nodes
        self._nodes_file.write_text(
            json.dumps(
                [{"id": n.node_id, "text": n.text, "metadata": n.metadata} for n in nodes],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        manifest = {
            **self._corpus_signature(),
            "built_at": time.time(),
            "vector_ok": self._vector_ok,
            "chunks": len(nodes),
            # 读了但没进索引的语料。留痕，否则知识库里「少了一份标准」无从发现
            "skipped": skipped,
        }
        self._manifest_file.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(
            "知识库索引构建完成：%d 篇文档 → %d 个分块，向量分支=%s%s",
            len(documents),
            len(nodes),
            "可用" if self._vector_ok else "不可用",
            f"，跳过 {len(skipped)} 份（{('、'.join(skipped))[:100]}）" if skipped else "",
        )
        return {"built": True, **manifest}

    # ---- 加载 ----

    def _ensure_loaded(self) -> None:
        if self._nodes is not None:
            return

        if not self.is_built:
            self.build()

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
                # 必须用 load_index_from_storage 而非 VectorStoreIndex.from_vector_store：
                # 后者只取向量存储，而节点文本存在 docstore 中，会报
                # "Cannot initialize from a vector store that does not store text"。
                storage = StorageContext.from_defaults(persist_dir=str(self._vector_dir))
                self._vector = load_index_from_storage(
                    storage, embed_model=self._embedder()
                )
                self._vector_ok = True
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "向量索引加载失败，降级为纯 BM25（可用 force=True 重建）：%s", exc
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

    def _vector_hits(self, query: str, k: int, query_embedding: list[float] | None = None) -> list[tuple[str, float]]:
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

        # RRF：score = Σ 1/(K + rank)
        fused: dict[str, float] = {}
        for hits in (bm_hits, vec_hits):
            for rank, (node_id, _score) in enumerate(hits):
                fused[node_id] = fused.get(node_id, 0.0) + 1.0 / (RRF_K + rank + 1)

        ranked = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)
        return ranked, dict(bm_hits), dict(vec_hits), mode

    def retrieve(self, query: str, top_k: int = 3) -> tuple[list[dict], str]:
        """混合检索。返回 (结果列表, 实际使用的检索模式)。

        融合方式为倒数排名融合（RRF），无需在各分支分数间做归一化。
        """
        # 语料可能被前端上传/删除过，检索前先确认索引是否过期。
        # 只有确实变更时才重建，避免每次检索都重复调用 Embedding 服务。
        if self.needs_rebuild:
            logger.info("检测到语料变更，重建知识库索引")
            self.build(force=True)

        self._ensure_loaded()
        if not self._nodes:
            return [], "empty"

        ranked, bm_scores, vec_scores, mode = self._rank(query, max(top_k * 2, 6))
        if not ranked:
            return [], mode

        # 相关性下限：融合分数只反映排名，一个完全不相干的查询在第一名上照样能拿到
        # 1/(60+1)，不能拿来当阈值（见 Settings.retrieval_min_score）。
        #
        # 两条通道任一达标即保留：
        #   向量通道 —— 语义相近即可，管口语化提问
        #   字面通道 —— 整词或二元组出现在原文里，管标准号、型号这类语义相似度偏低
        #               但必须命中的精确词（实测 GJB1580A 的余弦只有 0.39，紧贴下限）
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
            text = by_id[node_id].text.lower()
            if any(tok in text for tok in strong):
                keep.append(node_id)

        if not keep:
            # 检索执行过了，只是没有候选达到下限——与「索引为空」是两回事，
            # 工具据此给出「未找到相关内容」而非「知识库为空」。
            return [], "low_relevance"

        # ranked 本就按融合分降序，过滤即保序，无需重排
        keep_set = set(keep)
        ranked = [(node_id, rrf) for node_id, rrf in ranked if node_id in keep_set][:top_k]

        out = []
        for node_id, rrf in ranked:
            node = by_id.get(node_id)
            if node is None:
                continue
            out.append(
                {
                    "text": node.text,
                    "score": round(rrf, 6),
                    "source": node.metadata.get("file_name")
                    or node.metadata.get("file_path", "unknown"),
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

        `kept` 为 false 的候选正是被相关性下限挡掉的，把它们一并返回，
        管理员才能看清阈值卡在哪、该往哪调。
        """
        if self.needs_rebuild:
            self.build(force=True)
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
                    "source": node.metadata.get("file_name")
                    or node.metadata.get("file_path", "unknown"),
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
            "corpus": self._corpus_signature(),
            "chunks": len(self._nodes or []),
            "vector_available": self._vector_ok,
            "built_at": self._manifest().get("built_at"),
        }
