# backend/vector.py
# 本模块职责：封装向量库(Qdrant)与向量化(DashScope TextEmbedding)，对外只暴露 add_docs / retrieve。
import os
import asyncio
import logging
import uuid as _uuid
from pathlib import Path

from dotenv import load_dotenv
import dashscope
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, VectorParams, PointStruct

# ★ 先加载本目录 .env，再读环境变量（避免 import 期 KeyError: 'API_KEY'）
load_dotenv(Path(__file__).resolve().parent / ".env")

# 模块级 logger：日志级别/格式由 main.py 的 logging.basicConfig 统一控制，
# 用独立命名 "vector" 便于在海量日志里区分来源。
log = logging.getLogger("vector")

EMB_MODEL = os.environ["EMBED_MODEL"]
EMBED_DIM = int(os.environ.get("EMBED_DIM", "1536"))
COLLECTION = os.environ.get("QDRANT_COLLECTION", "kb")

# ---------- Embedding 走 DashScope 原生 SDK ----------
dashscope.api_key = os.environ["API_KEY"]
_emb_base = os.environ.get("DASHSCOPE_EMB_URL", "").strip()
if _emb_base:
    dashscope.base_http_api_url = _emb_base   # ★ 在代码里设置，而不是指望 .env 里那行自动生效


def _make_client() -> QdrantClient:
    """按环境变量选择 Qdrant 连接形态：
    - QDRANT_URL 有值 → 连接 server / 云托管（支持多进程并发访问）
    - 否则 → 本地嵌入模式（单进程文件锁，Windows 免服务）
    """
    url = os.environ.get("QDRANT_URL", "").strip()
    if url:
        log.info("Qdrant 连接形态=server/cloud | url=%s", url)
        return QdrantClient(url=url, api_key=os.environ.get("QDRANT_API_KEY") or None)
    path = os.environ.get("QDRANT_PATH", "./qdrant_data").strip()
    log.info("Qdrant 连接形态=本地嵌入 | path=%s", path)
    return QdrantClient(path=path)

_qdrant = None

def _get_client() -> QdrantClient:
    """返回全局单例客户端。首次调用时才真正创建（懒加载），
    避免模块 import 阶段就抢占 Qdrant 本地目录的文件锁。"""
    global _qdrant
    if _qdrant is None:
        log.info("首次初始化 Qdrant 客户端（懒加载）")
        _qdrant = _make_client()
    return _qdrant



def _pid(id_str: str) -> str:
    """Qdrant 只接受 int 或 UUID 作为点 id，用 uuid5 把 '文件名#块号' 映射成稳定 UUID，
    同一块重复上传会覆盖（幂等），不会重复堆积。"""
    return str(_uuid.uuid5(_uuid.NAMESPACE_URL, id_str))


def _ensure_collection():
    """确保目标集合存在：不存在则按 EMBED_DIM 维度 + COSINE 相似度创建。"""
    _qdrant = _get_client()
    if _qdrant.collection_exists(COLLECTION):
        log.debug("集合已存在，跳过创建 | collection=%s", COLLECTION)
        return
    log.info("创建新集合 | collection=%s | dim=%d | metric=COSINE", COLLECTION, EMBED_DIM)
    _qdrant.create_collection(
        COLLECTION,
        vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
    )

def _get(obj, key):
    """兼容 DashScope 返回体是 dict 还是对象两种形态。"""
    if isinstance(obj, dict):
        return obj[key]
    return getattr(obj, key)



async def _embed(texts: list[str], batch: int = 10) -> list[list[float]]:
    """调用 DashScope 原生 TextEmbedding 批量向量化。
    - SDK 同步阻塞，用 asyncio.to_thread 放到线程池，避免卡住事件循环；
    - 按 batch 分批发送，规避单次 input 数量上限；
    - 返回的 embeddings 与输入 chunk 顺序一致，直接按序取向量。"""
    log.info("开始向量化 | 文本数=%d | 模型=%s | batch=%d", len(texts), EMB_MODEL, batch)
    out: list[list[float]] = []
    for i in range(0, len(texts), batch):
        chunk = texts[i:i + batch]
        resp = await asyncio.to_thread(
            dashscope.TextEmbedding.call,
            model=EMB_MODEL,
            input=chunk,
        )
        if resp.status_code != 200:
            log.error("embedding 调用失败 | status=%s | code=%s | msg=%s",
                      resp.status_code, getattr(resp, 'code', '?'), getattr(resp, 'message', ''))
            raise RuntimeError(
                f"embedding 调用失败: code={getattr(resp, 'code', '?')} "
                f"msg={getattr(resp, 'message', '')}"
            )
        embeddings = _get(resp, "output")
        embeddings = _get(embeddings, "embeddings")
        vecs = [_get(e, "embedding") for e in embeddings]
        # 首批打印一次向量维度，方便核对 EMBED_DIM 是否与模型实际输出一致
        if not out and vecs:
            log.info("embedding 维度校验 | 单条向量长度=%d（应与 EMBED_DIM=%d 一致）",
                     len(vecs[0]), EMBED_DIM)
        out.extend(vecs)
    log.info("向量化完成 | 共生成 %d 条向量", len(out))
    return out


async def add_docs(docs: list[str], ids: list[str]):
    """把文本块向量化后写入 Qdrant。docs 与 ids 一一对应；
    ids 用 '文件名#块号' 经 uuid5 映射成稳定点 id，重复上传同一块会覆盖（幂等）。"""
    log.info("文档入库开始 | 待入库块数=%d", len(docs))
    _qdrant = _get_client()
    _ensure_collection()
    embs = await _embed(docs)
    points = [
        PointStruct(id=_pid(i), vector=v, payload={"id": i, "text": t})
        for i, t, v in zip(ids, docs, embs)
    ]
    await asyncio.to_thread(_qdrant.upsert, collection_name=COLLECTION, points=points)
    log.info("文档入库完成 | collection=%s | 写入点数=%d", COLLECTION, len(points))


async def retrieve(query: str, k: int = 4, score_threshold: float | None = None) -> list[str]:
    """检索与 query 最相似的 Top-K 文本块。
    - 集合不存在（还没灌过库）时优雅返回空列表；
    - score_threshold 给定时，低于该相似度的结果会被过滤掉。"""
    _qdrant = _get_client()
    if not _qdrant.collection_exists(COLLECTION):
        log.info("检索跳过：集合不存在（知识库尚未入库）| collection=%s", COLLECTION)
        return []
    log.info("开始检索 | query=%r | k=%d | threshold=%s", query[:50], k, score_threshold)
    q = (await _embed([query]))[0]
    res = await asyncio.to_thread(
        _qdrant.query_points,
        collection_name=COLLECTION,
        query=q,
        limit=k,
        score_threshold=score_threshold,
        with_payload=True,
    )
    texts = [p.payload["text"] for p in res.points]
    log.info("检索完成 | 命中 %d 条", len(texts))
    return texts





