# backend/bm25.py
# 本模块职责：进程内 BM25 关键字索引，与 vector.py 的向量检索并联做混合检索。
# 数据来源：直接复用 vector.py 的全局 Qdrant 客户端与 COLLECTION 常量，
#           启动时从 payload scroll 全量重建，入库时增量追加——不另建 QdrantClient，
#           避免本地嵌入模式下抢占同一目录的文件锁。
import asyncio
import logging

from rank_bm25 import BM25Okapi
import jieba

# ★ 复用 vector.py 的客户端单例与集合常量（同一进程同一连接，无锁冲突）
from vector import _get_client, COLLECTION

log = logging.getLogger("bm25")

# 索引状态：_ids / _texts / _tokenized 三者一一对应
_ids: list[str] = []
_texts: list[str] = []
_tokenized: list[list[str]] = []
_engine: "BM25Okapi | None" = None


def _tokenize(text: str) -> list[str]:
    """jieba 分词，过滤空白 token。BM25 本身不懂中文，分词质量直接决定关键字检索效果。"""
    return [t for t in jieba.lcut(text) if t.strip()]


def _rebuild_engine():
    """用当前全部文本重建 BM25 打分矩阵（O(N)，万级块以内毫秒~秒级，可接受）。"""
    global _engine
    _engine = BM25Okapi(_tokenized) if _tokenized else None
    log.info("BM25 索引重建完成 | 块数=%d", len(_texts))


async def load_from_qdrant():
    """服务启动时调用：scroll 拉取 Qdrant 全部点的 payload，初始化内存 BM25 索引。

    只取 payload 不取向量（with_vectors=False），拉量更小；分页 offset 循环直到取空。
    """
    global _ids, _texts, _tokenized
    client = _get_client()
    if not await asyncio.to_thread(client.collection_exists, COLLECTION):
        log.info("BM25 初始化跳过：集合尚不存在 | collection=%s", COLLECTION)
        return
    docs: list[tuple[str, str]] = []
    offset = None
    while True:
        points, offset = await asyncio.to_thread(
            client.scroll, collection_name=COLLECTION, limit=256,
            with_payload=True, with_vectors=False, offset=offset,
        )
        for p in points:
            payload = p.payload or {}
            docs.append((payload.get("id", "未知来源"), payload.get("text", "")))
        if offset is None:
            break
    _ids = [d[0] for d in docs]
    _texts = [d[1] for d in docs]
    _tokenized = [_tokenize(t) for t in _texts]
    _rebuild_engine()
    log.info("BM25 索引已从 Qdrant 加载 | collection=%s | 块数=%d", COLLECTION, len(_ids))


async def add_chunks(ids: list[str], texts: list[str]):
    """文档入库后增量追加，随后重建（块量小，整体重建比维护复杂增量结构更可靠）。"""
    _ids.extend(ids)
    _texts.extend(texts)
    _tokenized.extend([_tokenize(t) for t in texts])
    _rebuild_engine()


def search(query: str, k: int = 10) -> list[dict]:
    """BM25 关键字检索，返回 [{"id", "text", "score"}]，结构与 vector.retrieve 对齐。
    得分为 0 表示查询词在库中无任何命中，直接丢弃，避免污染融合结果。"""
    if _engine is None or not _texts:
        return []
    tokens = _tokenize(query)
    if not tokens:
        return []
    scores = _engine.get_scores(tokens)
    top_idx = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]
    hits = [{"id": _ids[i], "text": _texts[i], "score": float(scores[i])}
            for i in top_idx if scores[i] > 0]
    log.info("BM25 检索完成 | query=%r | 命中 %d 条 | 来源=%s",
             query[:50], len(hits), [h["id"] for h in hits])
    return hits
