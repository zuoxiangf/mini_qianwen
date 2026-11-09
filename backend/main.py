import os
import json
import uuid
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile, File, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from openai import AsyncOpenAI, APIStatusError, APITimeoutError
from dotenv import load_dotenv

# ★ 先加载本目录 .env（锚定文件位置，不依赖工作目录），再读取环境变量 / 导入依赖配置的模块
load_dotenv(Path(__file__).resolve().parent / ".env")

from db import (                                       # noqa: E402  MySQL 会话/用户读写
    append_message, load_history, clear_session,
    list_sessions as db_list_sessions, get_session_owner,
    create_user, get_user_by_username,
)
from vector import retrieve, add_docs            # noqa: E402  向量检索 / 入库
from extract import extract_text                 # noqa: E402  PDF/DOCX/文本编码嗅探
from auth import (                               # noqa: E402  密码哈希 / JWT / 当前用户依赖
    hash_password, verify_password, create_token, get_current_user,
    USERNAME_MIN, USERNAME_MAX, PASSWORD_MIN,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ---------- 配置（均来自 .env）----------
API_KEY = os.environ.get("API_KEY", "")                 # 大模型密钥：仅存在于后端，绝不下发到前端
BASE_URL = os.environ.get("BASE_URL", "https://maas.qianwenaiapi.com/compatible-mode/v1")
CHAT_MODEL = os.environ.get("CHAT_MODEL", "deepseek-v4-pro-0813")
QA_MODEL = os.environ.get("QA_MODEL", "deepseek-v4-pro-0813")
MAX_HISTORY = int(os.environ.get("MAX_HISTORY", "10"))
# 知识库单文件上传大小上限（MB）
MAX_UPLOAD_MB = int(os.environ.get("KB_MAX_UPLOAD_MB", "20"))
# RAG 检索相似度下限：留空则不过滤（全库 Top-K，旧资料可能挤掉新资料）；
# 建议先设 0.2~0.35 观察命中分布，过低会把不相关内容也塞进上下文
KB_SCORE_THRESHOLD = os.environ.get("KB_SCORE_THRESHOLD", "").strip()
KB_SCORE_THRESHOLD = float(KB_SCORE_THRESHOLD) if KB_SCORE_THRESHOLD else None
# CORS 白名单：逗号分隔的前端来源，如 http://1.2.3.4:8080；留空则退回 *（仅限本地开发）
FRONTEND_ORIGINS = [o.strip() for o in os.environ.get("FRONTEND_ORIGINS", "").split(",") if o.strip()]

client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=90)

SYSTEM_CHAT = """你是千问小助手，一个友好、幽默、乐于助人的 AI 伙伴。
要求：回答简洁自然，像真人聊天；不知道的事直接承认，不编造；
单次回复尽量控制在 500 字以内，除非用户要求详细展开。"""

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
    allow_origins=FRONTEND_ORIGINS or ["*"],  # 生产在 .env 配 FRONTEND_ORIGINS 收敛白名单
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


class RegisterIn(BaseModel):
    username: str = Field(min_length=USERNAME_MIN, max_length=USERNAME_MAX)
    password: str = Field(min_length=PASSWORD_MIN)


class LoginIn(RegisterIn):
    pass


# ---------- 工具函数 ----------


def _chunk(text: str, size=500, overlap=80) -> list[str]:
    """按字符窗口切分并保留重叠，避免答案正好横跨切缝被截断。"""
    out, i = [], 0
    while i < len(text):
        out.append(text[i:i+size])
        i += size - overlap
    return [c for c in out if c.strip()]

def _make_context(hits: list[dict]) -> str:
    """把命中资料拼进 system 提示；资料段标注来源 id，便于模型引用时带出处。"""
    joined = "\n\n".join(f"【资料{i+1}｜来源：{h['id']}】{h['text']}" for i, h in enumerate(hits))
    return (SYSTEM_QA + "\n\n请仅依据以下资料作答，资料里没有的明确说不知道；"
                    "引用资料时请在句末用【资料编号】标注出处：\n" + joined)


async def _stream_to_browser(stream, on_complete=None, pre_events=None):
    """把 OpenAI chunk 流转成 SSE `data:` 行逐段下发给浏览器。两个可选钩子：
    - pre_events：流开始前的一次性事件列表（如 RAG 命中来源 {"sources": [...]}）；
    - on_complete：流正常结束后收到拼接的完整正文，用于回写持久化（闲聊历史）。
    异常不掐断连接，改为下发 {"error": ...} 事件；无论成败最后必发 data: [DONE]。
    """
    collected = []
    try:
        for pe in pre_events or []:                     # ★ 流开始前的一次性事件（如来源列表）
            yield f"data: {json.dumps(pe, ensure_ascii=False)}\n\n"
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


# ---------- 鉴权：注册 / 登录 / 当前用户（公开接口除外均需 Bearer Token）----------
@app.post("/api/auth/register", status_code=201)
async def register(body: RegisterIn):
    """新用户注册：bcrypt 哈希密码后写 users 表，注册成功直接颁发 token 免二次登录。"""
    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="用户名不能为空")
    # bcrypt 计算量较大（百毫秒级），放线程池避免阻塞事件循环
    pw_hash = await asyncio.to_thread(hash_password, body.password)
    try:
        uid = await create_user(username, pw_hash)
    except IntegrityError:
        log.info("注册失败：用户名已存在 | username=%s", username)
        raise HTTPException(status_code=409, detail="用户名已被占用")
    log.info("注册成功 | uid=%d | username=%s", uid, username)
    return {"token": create_token(uid, username), "username": username}


@app.post("/api/auth/login")
async def login(body: LoginIn):
    """登录：验证用户名+密码，成功颁发 JWT；失败统一 401（不区分用户不存在/密码错）。"""
    row = await get_user_by_username(body.username.strip())
    # row 为 (uid, pw_hash)；无论命中与否都跑一次 bcrypt，拉平响应时间防用户名探测
    ok = await asyncio.to_thread(
        verify_password, body.password, row[1] if row else "$2b$12$" + "0" * 53
    )
    if not row or not ok:
        log.info("登录失败 | username=%s", body.username)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    log.info("登录成功 | uid=%d | username=%s", row[0], body.username)
    return {"token": create_token(row[0], body.username.strip()), "username": body.username.strip()}


@app.get("/api/auth/me")
async def me(user: dict = Depends(get_current_user)):
    """回显当前登录用户，供前端启动时校验 token 是否仍有效。"""
    return {"uid": user["uid"], "username": user["username"]}


# ---------- 知识库状态查询（排查"文本块是否真的写进了 Qdrant"）----------
@app.get("/api/kb/stats")
async def kb_stats(limit_sources: int = 30, user: dict = Depends(get_current_user)):
    """返回集合是否存在、向量维度、已存点数，并**按来源聚合块数**。

    为何要聚合：`points_count` 只能看总量，重复上传同名文件是幂等覆盖不会涨数；
    而只抽 3 条 scroll 样例也看不出新文件。改成按 payload 里 `文件名#块号` 的文件名
    前缀分组，就能直接判断“今天这份文件到底在不在库里”。

    复用后端进程内的 Qdrant 客户端，避免另起进程抢占本地模式的文件锁；
    同步的 Qdrant 调用用 asyncio.to_thread 包裹，避免阻塞事件循环。
    """
    from vector import _get_client, COLLECTION
    c = _get_client()
    if not await asyncio.to_thread(c.collection_exists, COLLECTION):
        log.info("知识库状态查询 | 集合不存在 | collection=%s", COLLECTION)
        return {"exists": False, "collection": COLLECTION}
    info = await asyncio.to_thread(c.get_collection, COLLECTION)
    # 全量 scroll 汇总各来源块数（本地模式数据量小；设上限防止无限循环拉大表）
    sources: dict[str, int] = {}
    samples: list[dict] = []
    scanned, offset = 0, None
    while scanned < 20000:
        pts, offset = await asyncio.to_thread(
            c.scroll, collection_name=COLLECTION, limit=256,
            with_payload=True, offset=offset,
        )
        if not pts:
            break
        for p in pts:
            pid = (p.payload or {}).get("id", "")
            src = pid.split("#", 1)[0] or "未知来源"
            sources[src] = sources.get(src, 0) + 1
            if len(samples) < 5:
                samples.append({"id": pid, "text": (p.payload or {}).get("text", "")[:40]})
        scanned += len(pts)
    ranked = sorted(sources.items(), key=lambda kv: -kv[1])[:limit_sources]
    log.info("知识库状态查询 | collection=%s | dim=%d | points_count=%s | 扫描=%d | 来源数=%d",
             COLLECTION, info.config.params.vectors.size, info.points_count, scanned, len(sources))
    return {
        "exists": True,
        "collection": COLLECTION,
        "dim": info.config.params.vectors.size,
        "points_count": info.points_count,
        "scanned": scanned,
        "source_count": len(sources),
        "sources": [{"source": s, "chunks": n} for s, n in ranked],
        "samples": samples,
        "score_threshold": KB_SCORE_THRESHOLD,
    }


# ---------- 知识库入库（RAG 的“喂数据”入口，支持纯文本 / PDF / DOCX / HTML）----------
async def _ingest_text(text: str, source: str) -> dict:
    """公共入库流水线：切块 → 向量化 → 写 Qdrant。source 用于生成块 id 前缀。

    注意：抽取不到文本时必须显式报错，不能返回 ok=True，
    否则扫描版/图片型 PDF 会呈现为“入库成功但库里没有”，极难排查。
    """
    chunks = _chunk(text)
    if not chunks:
        log.warning("知识库入库失败：抽取文本为空 | 来源=%s | 抽取字符=%d", source, len(text or ""))
        raise HTTPException(
            status_code=422,
            detail="未能从内容中抽取到文本（扫描版/图片型 PDF 或全文为图片时会出现），本次未入库",
        )
    ids = [f"{source}#{i}" for i in range(len(chunks))]
    try:
        await add_docs(chunks, ids)
    except Exception as e:
        log.exception("知识库入库失败 | 来源=%s", source)
        raise HTTPException(status_code=502, detail=f"向量化/入库失败：{e}")
    log.info("知识库入库 | 来源=%s | 抽取字符=%d | 块数=%d", source, len(text), len(chunks))
    return {"ok": True, "source": source, "chars": len(text), "chunks": len(chunks)}


@app.post("/api/kb/upload")
async def upload(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    """接收文件 → 抽取纯文本（文本编码嗅探 / PDF / DOCX / HTML）→ 切块 → 向量化 → 写入 Qdrant。"""
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_MB * 1024 * 1024:
        log.warning("知识库入库拒绝：文件超限 | 文件=%s | 大小=%dMB",
                    file.filename, len(raw) // 1024 // 1024)
        raise HTTPException(status_code=413, detail=f"文件超过 {MAX_UPLOAD_MB}MB 上限")
    log.info("收到知识库上传 | 文件=%s | 原始字节=%d", file.filename, len(raw))
    try:
        # Qdrant/解析均为同步操作，放线程池避免阻塞事件循环
        text = await asyncio.to_thread(extract_text, raw, file.filename or "")
    except ValueError as e:            # 格式不支持 / 文件损坏等用户侧问题
        log.warning("知识库解析失败（用户侧）| 文件=%s | %s", file.filename, e)
        raise HTTPException(status_code=400, detail=f"文件解析失败：{e}")
    except RuntimeError as e:          # 解析库缺失等服务端问题
        log.error("知识库解析失败（服务端）| 文件=%s | %s", file.filename, e)
        raise HTTPException(status_code=500, detail=str(e))
    return await _ingest_text(text, file.filename or "noname")


class WebIn(BaseModel):
    """网页入库请求体：仅支持 http/https 链接。"""
    url: str


@app.post("/api/kb/web")
async def ingest_web(body: WebIn, user: dict = Depends(get_current_user)):
    """抓取网页 → 抽取正文（去标签）→ 切块 → 向量化 → 写入 Qdrant。仅支持 http/https。"""
    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="仅支持 http/https 链接")
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as cx:
            resp = await cx.get(body.url, headers={"User-Agent": "Mozilla/5.0 (compatible; MiniQwenBot/1.0)"})
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        log.warning("网页抓取失败 | url=%s | status=%d", body.url, e.response.status_code)
        raise HTTPException(status_code=502, detail=f"目标站点返回 {e.response.status_code}")
    except httpx.HTTPError as e:
        log.warning("网页抓取异常 | url=%s | %s", body.url, e)
        raise HTTPException(status_code=502, detail=f"网页抓取失败：{e}")
    try:
        text = await asyncio.to_thread(extract_text, resp.content, "page.html")
    except (ValueError, RuntimeError) as e:
        raise HTTPException(status_code=400, detail=f"网页解析失败：{e}")
    # 用 URL 哈希前 8 位作块 id 前缀，避免超长/带参数的 URL 塞进 id
    source = "web_" + uuid.uuid5(uuid.NAMESPACE_URL, body.url).hex[:8]
    log.info("网页抓取成功 | url=%s | 正文字符=%d", body.url, len(text))
    return await _ingest_text(text, source)



# ---------- 接口 1：闲聊（多轮，有状态，历史存 MySQL）----------
@app.post("/api/chat")
async def chat(body: ChatIn, user: dict = Depends(get_current_user)):
    """闲聊接口（多轮、有状态）：按 session_id 读写 MySQL 会话历史，流式返回模型回复。

    流程：落库用户消息 → 取最近 N 轮历史 → 调模型(stream) → 流结束后回写助手消息。
    会话首次创建时绑定当前用户 uid，历史与会话归属于该用户，不跨用户可见。
    """
    uid = user["uid"]
    sid = body.session_id or str(uuid.uuid4())
    # 1) 落库本轮用户消息（同时将会话归属到当前用户）
    await append_message(sid, "user", body.message, uid)

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
            _stream_to_browser(stream, on_complete=lambda full: append_message(sid, "assistant", full, uid)),
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
async def ask(body: AskIn, user: dict = Depends(get_current_user)):
    """单轮问答接口，接入 Qdrant RAG：先检索命中资料，再注入 system 提示，并把来源透出给前端。"""

    hits = await retrieve(body.question, k=4, score_threshold=KB_SCORE_THRESHOLD)   # ★ 先检索，元素为含 id/text 的字典
    system = _make_context(hits) if hits else (body.system_prompt or SYSTEM_QA)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": body.question},
    ]
    sources = [h["id"] for h in hits]
    log.info("问答请求 | 命中资料 %d 段 | 来源=%s | threshold=%s | 问题=%r",
             len(hits), sources, KB_SCORE_THRESHOLD, body.question[:50])
    try:
        stream = await client.chat.completions.create(
            model=QA_MODEL,
            messages=messages,
            temperature=0.3,          # 问答要稳，温度压低
            max_tokens=2048,
            stream=True,
        )
        log.info("问答模型流已建立，开始返回 | model=%s", QA_MODEL)
        # pre_events 先于正文下发 sources，前端可展示答案依据哪份资料
        pre = [{"sources": sources}] if sources else None
        return StreamingResponse(_stream_to_browser(stream, pre_events=pre), media_type="text/event-stream")
    except APIStatusError as e:
        log.warning("问答模型返回状态错误 | status=%d", e.status_code)
        raise HTTPException(status_code=e.status_code, detail=str(e.body))
    except APITimeoutError:
        log.error("问答模型响应超时")
        raise HTTPException(status_code=504, detail="模型响应超时")


# ---------- 会话管理（基于 MySQL sessions 表，仅返回/操作当前登录用户的会话）----------
@app.get("/api/sessions")
async def list_sessions(limit: int = 20, offset: int = 0, user: dict = Depends(get_current_user)):
    """分页返回【当前用户】的会话列表，按最近活跃时间倒序（limit/offset 均有默认值，防全表拉取）。"""
    rows = await db_list_sessions(user["uid"], limit, offset)
    items = [{"sid": sid, "last_active": la.isoformat() if la else None} for sid, la in rows]
    log.info("查询会话列表 | uid=%d | limit=%d offset=%d | 命中 %d 条", user["uid"], limit, offset, len(items))
    return {"items": items, "limit": limit, "offset": offset}


@app.delete("/api/session/{sid}")
async def delete_session(sid: str, user: dict = Depends(get_current_user)):
    """删除指定会话：先校验会话归属，再清除 MySQL 中的历史消息及 sessions 表记录。"""
    owner = await get_session_owner(sid)
    if owner is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if owner != user["uid"]:
        log.warning("越权删除会话被拦截 | sid=%s | 归属=%d | 当前=%d", sid, owner, user["uid"])
        raise HTTPException(status_code=403, detail="无权操作该会话")
    await clear_session(sid)
    log.info("删除会话 | sid=%s | uid=%d", sid, user["uid"])
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)