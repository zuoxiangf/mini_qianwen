import os
import json
import uuid
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from openai import AsyncOpenAI, APIStatusError, APITimeoutError
from dotenv import load_dotenv

# ★ 先加载本目录 .env（锚定文件位置，不依赖工作目录），再读取环境变量 / 导入依赖配置的模块
load_dotenv(Path(__file__).resolve().parent / ".env")

from db import append_message, load_history      # noqa: E402  MySQL 会话读写
from vector import retrieve, add_docs            # noqa: E402  向量检索 / 入库

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ---------- 配置（均来自 .env）----------
API_KEY = os.environ.get("API_KEY", "")                 # 大模型密钥：仅存在于后端，绝不下发到前端
BASE_URL = os.environ.get("BASE_URL", "https://maas.qianwenaiapi.com/compatible-mode/v1")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "deepseek-v4-pro-0813")
QA_MODEL = os.environ.get("QA_MODEL", "deepseek-v4-pro-0813")
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "10"))

client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=90)

# ---------- 会话存储（遗留：闲聊已改存 MySQL，此内存字典仅供下面两个旧接口占位）----------
sessions: dict[str, list[dict]] = {}

SYSTEM_CHAT = """你是千问小助手，一个友好、幽默、乐于助人的 AI 伙伴。
要求：回答简洁自然，像真人聊天；不知道的事直接承认，不编造；
单次回复尽量控制在 200 字以内，除非用户要求详细展开。"""

SYSTEM_QA = """你是一个严谨的问答助手。请针对用户的当次提问给出准确、结构清晰的回答。
要求：分点作答、有依据；不确定的内容明确标注；不主动延伸话题。"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("服务启动 | CHAT=%s QA=%s", CHAT_MODEL, QA_MODEL)
    yield
    log.info("服务关闭")


app = FastAPI(title="MiniQwen", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # 生产环境收敛到具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Session-Id"],  # 允许前端 JS 读取该自定义响应头

)


# ---------- 数据模型 ----------
class ChatIn(BaseModel):
    message: str
    session_id: str | None = None


class AskIn(BaseModel):
    question: str
    system_prompt: str | None = None


# ---------- 工具函数 ----------


def _chunk(text: str, size=500, overlap=80) -> list[str]:
    """按字符窗口切分并保留重叠，避免答案正好横跨切缝被截断。"""
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i+size])
        i += size - overlap
    return [c for c in out if c.strip()]

def _make_context(snippets: list[str]) -> str:
    joined = "\n\n".join(f"【资料{i+1}】{s}" for i, s in enumerate(snippets))
    return SYSTEM_QA + "\n\n请仅依据以下资料作答，资料里没有的明确说不知道：\n" + joined



def _trim(messages: list[dict], max_turns: int = MAX_HISTORY) -> list[dict]:
    """裁剪会话历史：保留首条 system 消息 + 最近 max_turns 轮对话。

    一轮 = 用户 1 条 + 助手 1 条，故截取末尾 max_turns * 2 条消息，
    防止历史无限增长导致 token 超限。

    注：/api/chat 已改由 DB 层裁剪（load_history），此函数目前保留作内存模式备用。
    """
    if len(messages) <= 1:
        return messages
    kept = [messages[0]] + messages[-max_turns * 2 :]
    return kept


async def _stream_to_browser(stream, on_complete=None):
    collected = []
    try:
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            payload = {}
            if delta.content:
                payload["content"] = delta.content
                collected.append(delta.content)          # ★ 累计正文
            if getattr(delta, "reasoning_content", None):
                payload["reasoning"] = delta.reasoning_content
            if chunk.choices[0].finish_reason:
                payload["done"] = True
            if payload:
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    except Exception as e:
        log.exception("转发流失败")
        yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
    finally:
        if on_complete and collected:
            await on_complete("".join(collected))         # ★ 流结束后持久化 AI 回复
        yield "data: [DONE]\n\n"


# ---------- 知识库状态查询（排查"文本块是否真的写进了 Qdrant"）----------
@app.get("/api/kb/stats")
async def kb_stats():
    """返回集合是否存在、向量维度、已存点数以及抽样文本。

    复用后端进程内的 Qdrant 客户端，避免另起进程抢占本地模式的文件锁；
    同步的 Qdrant 调用用 asyncio.to_thread 包裹，避免阻塞事件循环。
    """
    from vector import _get_client, COLLECTION
    c = _get_client()
    if not await asyncio.to_thread(c.collection_exists, COLLECTION):
        log.info("知识库状态查询 | 集合不存在 | collection=%s", COLLECTION)
        return {"exists": False, "collection": COLLECTION}
    info = await asyncio.to_thread(c.get_collection, COLLECTION)
    pts, _ = await asyncio.to_thread(c.scroll, COLLECTION, limit=3, with_payload=True)
    samples = [{"id": p.id, "text": (p.payload or {}).get("text", "")[:40]} for p in pts]
    log.info("知识库状态查询 | collection=%s | dim=%d | points_count=%d",
             COLLECTION, info.config.params.vectors.size, info.points_count)
    return {
        "exists": True,
        "collection": COLLECTION,
        "dim": info.config.params.vectors.size,
        "points_count": info.points_count,
        "samples": samples,
    }


# ---------- 知识库入库（RAG 的"喂数据"入口，调试用）----------
@app.post("/api/kb/upload")
async def upload(file: UploadFile = File(...)):
    """接收一个 UTF-8 文本文件 → 切块 → 向量化 → 写入 Qdrant。"""
    try:
        text = (await file.read()).decode("utf-8")
    except UnicodeDecodeError:
        log.warning("知识库入库失败：文件非 UTF-8 编码 | 文件=%s", file.filename)
        raise HTTPException(status_code=400, detail="仅支持 UTF-8 文本文件")

    chunks = _chunk(text)
    if not chunks:
        log.info("知识库入库跳过：文件为空 | 文件=%s", file.filename)
        return {"ok": True, "chunks": 0}

    ids = [f"{file.filename}#{i}" for i in range(len(chunks))]
    try:
        await add_docs(chunks, ids)
    except Exception as e:
        log.exception("知识库入库失败 | 文件=%s", file.filename)
        raise HTTPException(status_code=502, detail=f"向量化/入库失败：{e}")
    log.info("知识库入库 | 文件=%s | 块数=%d", file.filename, len(chunks))
    return {"ok": True, "chunks": len(chunks)}



# ---------- 接口 1：闲聊（多轮，有状态，历史存 MySQL）----------
@app.post("/api/chat")
async def chat(body: ChatIn):
    """闲聊接口（多轮、有状态）：按 session_id 读写 MySQL 会话历史，流式返回模型回复。

    流程：落库用户消息 → 取最近 N 轮历史 → 调模型(stream) → 流结束后回写助手消息。
    """
    sid = body.session_id or str(uuid.uuid4())
    # 1) 落库本轮用户消息
    await append_message(sid, "user", body.message)

    # 2) 取最近 N 轮历史（窗口裁剪已在 DB 层完成）
    history = await load_history(sid, MAX_HISTORY)
    msgs = [{"role": "system", "content": SYSTEM_CHAT}] + history
    log.info("收到闲聊请求 | sid=%s | 历史条数=%d | 消息=%r", sid, len(history), body.message[:50])

    # 3) 调模型并把流转成 SSE；异常按类型转成对用户友好的 HTTP 错误
    try:
        stream = await client.chat.completions.create(
            model=CHAT_MODEL, messages=msgs,
            temperature=0.7, max_tokens=1024, stream=True,
        )
        log.info("闲聊模型流已建立，开始返回 | sid=%s | model=%s", sid, CHAT_MODEL)
        return StreamingResponse(
            # 流结束后把完整助手回复回写 MySQL，形成多轮记忆闭环
            _stream_to_browser(stream, on_complete=lambda full: append_message(sid, "assistant", full)),
            media_type="text/event-stream",
            headers={"X-Session-Id": sid},
        )
    except APIStatusError as e:
        log.warning("闲聊模型返回状态错误 | sid=%s | status=%d", sid, e.status_code)
        raise HTTPException(status_code=e.status_code, detail=str(e.body))
    except APITimeoutError:
        log.error("闲聊模型响应超时 | sid=%s", sid)
        raise HTTPException(status_code=504, detail="模型响应超时")




# ---------- 接口 2：单轮问答（无记忆，每次独立）----------
@app.post("/api/ask")
async def ask(body: AskIn):
    """单轮问答接口，接入 Qdrant RAG：先检索命中资料，再注入 system 提示。"""

    snippets = await retrieve(body.question, k=4)  # ★ 先检索
    system = _make_context(snippets) if snippets else (body.system_prompt or SYSTEM_QA)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": body.question},
    ]
    log.info("问答请求 | 命中资料 %d 段 | 问题=%r", len(snippets), body.question[:50])
    try:
        stream = await client.chat.completions.create(
            model=QA_MODEL,
            messages=messages,
            temperature=0.3,          # 问答要稳，温度压低
            max_tokens=2048,
            stream=True,
        )
        log.info("问答模型流已建立，开始返回 | model=%s", QA_MODEL)
        return StreamingResponse(_stream_to_browser(stream), media_type="text/event-stream")
    except APIStatusError as e:
        log.warning("问答模型返回状态错误 | status=%d", e.status_code)
        raise HTTPException(status_code=e.status_code, detail=str(e.body))
    except APITimeoutError:
        log.error("问答模型响应超时")
        raise HTTPException(status_code=504, detail="模型响应超时")


# ---------- 会话管理（遗留：基于内存 sessions 字典，与 MySQL 会话不同步）----------
@app.get("/api/sessions")
def list_sessions():
    """返回内存字典里的会话 id（注意：闲聊历史已迁 MySQL，此处通常为仅供调试的遗留接口）。"""
    ids = list(sessions.keys())
    log.info("查询会话列表（遗留内存版）| 共 %d 个", len(ids))
    return {"ids": ids}


@app.delete("/api/session/{sid}")
def clear_session(sid: str):
    """清空指定会话的历史：重置为初始 system 消息（会话本身保留）。"""
    log.info("清空会话 | sid=%s | 会话存在=%s", sid, sid in sessions)
    if sid in sessions:
        sessions[sid] = [{"role": "system", "content": SYSTEM_CHAT}]
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)