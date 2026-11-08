import os
import uuid
import json
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from openai import AsyncOpenAI, APIStatusError, APITimeoutError
from dotenv import load_dotenv
from db import append_message, load_history

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ---------- 配置 ----------


API_KEY = os.environ.get("API_KEY", "")   # 从 .env 读取，不再硬编码

BASE_URL = os.environ.get("BASE_URL", "https://maas.qianwenaiapi.com/compatible-mode/v1")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "deepseek-v4-pro-0813")
QA_MODEL = os.environ.get("QA_MODEL", "deepseek-v4-pro-0813")
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "10"))

client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=90)

# ---------- 会话存储（内存版，生产换 Redis/DB）----------
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
def _trim(messages: list[dict], max_turns: int = MAX_HISTORY) -> list[dict]:
    """裁剪会话历史：保留首条 system 消息 + 最近 max_turns 轮对话。

    一轮 = 用户 1 条 + 助手 1 条，故截取末尾 max_turns * 2 条消息，
    防止历史无限增长导致 token 超限。
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

# ---------- 接口 1：闲聊（多轮，有状态）----------
@app.post("/api/chat")
async def chat(body: ChatIn):
    """闲聊接口（多轮、有状态）：按 session_id 读写会话历史，流式返回模型回复。"""
    sid = body.session_id or str(uuid.uuid4())
    # 1) 落库本轮用户消息
    await append_message(sid, "user", body.message)

    # 2) 取最近 N 轮历史（DB 层已完成窗口裁剪）
    history = await load_history(sid, MAX_HISTORY)
    msgs = [{"role": "system", "content": SYSTEM_CHAT}] + history

    stream = await client.chat.completions.create(
        model=CHAT_MODEL, messages=msgs,
        temperature=0.7, max_tokens=1024, stream=True,
    )
    # 3) 关键：把 session 传入，流结束后回写 assistant 回复
    return StreamingResponse(
        _stream_to_browser(stream, on_complete=lambda full: append_message(sid, "assistant", full)),
        media_type="text/event-stream",
        headers={"X-Session-Id": sid},
    )




# ---------- 接口 2：单轮问答（无记忆，每次独立）----------
@app.post("/api/ask")
async def ask(body: AskIn):
    """单轮问答接口（无记忆，每次独立），流式返回模型回复。

    关键区别：不写入 sessions，不携带历史。
    适合 FAQ、题库、基于材料的问答（后续在此处挂 RAG）。
    """
    system = body.system_prompt or SYSTEM_QA
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": body.question},
    ]
    log.info("收到问答请求 | 问题=%r | 自定义提示词=%s", body.question[:50], bool(body.system_prompt))
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


# ---------- 会话管理（可选，前端"新建对话"用）----------
@app.get("/api/sessions")
def list_sessions():
    """返回当前所有会话 id，供前端展示/切换。"""
    ids = list(sessions.keys())
    log.info("查询会话列表 | 共 %d 个", len(ids))
    return {"ids": ids}


@app.delete("/api/session/{sid}")
def clear_session(sid: str):
    """清空指定会话的历史：重置为初始 system 消息（会话本身保留）。"""
    log.info("清空会话 | sid=%s | 会话存在=%s", sid, sid in sessions)
    if sid in sessions:
        sessions[sid] = [{"role": "system", "content": SYSTEM_CHAT}]
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)