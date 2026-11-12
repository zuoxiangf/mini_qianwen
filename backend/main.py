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
    list_sessions as db_list_sessions, get_session_owner, get_session_messages,
    create_user, get_user_by_username,
)
from vector import retrieve, add_docs, hybrid_retrieve            # noqa: E402  向量检索 / 入库 / 混合检索
import bm25                                                   # noqa: E402  关键字索引（复用 vector 的客户端，无锁冲突）
from extract import extract_text                 # noqa: E402  PDF/DOCX/文本编码嗅探
from tools import TOOL_SCHEMAS, execute_tool        # noqa: E402  Function Calling 工具层
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
# 检索模式开关：hybrid（向量+关键字融合，默认）/ vector（纯向量）/ bm25（纯关键字），便于 A/B 对比效果
RETRIEVAL_MODE = os.environ.get("RETRIEVAL_MODE", "hybrid").strip().lower()
# CORS 白名单：逗号分隔的前端来源，如 http://1.2.3.4:8080；留空则退回 *（仅限本地开发）
FRONTEND_ORIGINS = [o.strip() for o in os.environ.get("FRONTEND_ORIGINS", "").split(",") if o.strip()]
# Function Calling 开关：网关/模型不支持 tools 参数时可置 0 快速回退纯闲聊
ENABLE_TOOLS = os.environ.get("ENABLE_TOOLS", "1").strip() == "1"
# 单轮对话最多允许几轮工具调用；最后一轮会去掉 tools 强制模型文字收尾，防死循环
MAX_TOOL_ROUNDS = max(1, int(os.environ.get("MAX_TOOL_ROUNDS", "4")))

client = AsyncOpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=90)

SYSTEM_CHAT = """你是千问小助手，一个友好、幽默、乐于助人的 AI 伙伴。
要求：回答简洁自然，像真人聊天；不知道的事直接承认，不编造；
单次回复尽量控制在 500 字以内，除非用户要求详细展开。
你可以调用工具获取实时信息：查天气用 get_weather，查账号资料用 get_user_info，
检索用户过往对话用 search_chat_history。需要时先调工具再作答，不要臆造数据；
工具返回失败时，如实告知用户而不是编造结果。"""

SYSTEM_QA = """你是一个严谨的问答助手。请针对用户的当次提问给出准确、结构清晰的回答。
要求：分点作答、有依据；不确定的内容明确标注；不主动延伸话题。"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("服务启动 | CHAT=%s QA=%s | 检索模式=%s", CHAT_MODEL, QA_MODEL, RETRIEVAL_MODE)
    # 从 Qdrant payload 全量重建内存 BM25 索引（向量检索无需预热，随时可用）
    try:
        await bm25.load_from_qdrant()
    except Exception:
        log.exception("BM25 索引启动加载失败，混合检索将退化为纯向量")
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


async def _chat_sse(initial_stream, msgs: list[dict], ctx: dict, on_complete=None):
    """带 Function Calling 循环的闲聊 SSE 生成器（兼容 stream=True 的工具调用分片）。

    每轮：content 增量直接下发浏览器，tool_calls 增量按 index 拼接（name/arguments
    会分散在多个 chunk）；轮末若存在工具调用——下发 {"tool": ...} 事件供前端展示，
    执行后按 OpenAI 协议把 assistant(tool_calls) 与 tool 结果回填 msgs，再发起下一轮。
    达到 MAX_TOOL_ROUNDS 后不再传 tools，强制模型基于已有结果文字收尾，防死循环。
    首轮请求已由调用方建立（失败可正常返回 HTTP 状态码），后续轮异常走 SSE error 事件。
    """
    stream = initial_stream
    collected: list[str] = []
    try:
        for round_i in range(MAX_TOOL_ROUNDS + 1):
            tool_calls: dict[int, dict] = {}              # index -> {id, name, args}
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                payload = {}
                if delta.content:
                    payload["content"] = delta.content
                    collected.append(delta.content)
                if getattr(delta, "reasoning_content", None):
                    payload["reasoning"] = delta.reasoning_content
                for tc in delta.tool_calls or []:
                    slot = tool_calls.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        slot["id"] = tc.id
                    if tc.function and tc.function.name:
                        slot["name"] += tc.function.name
                    if tc.function and tc.function.arguments:
                        slot["args"] += tc.function.arguments
                if payload:
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if not tool_calls:
                break                                     # 本轮无工具调用 → 回答完成
            calls = [tool_calls[i] for i in sorted(tool_calls)]
            msgs.append({"role": "assistant", "content": None, "tool_calls": [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["args"]}}
                for c in calls]})
            for c in calls:
                log.info("工具调用 | sid=%s | 轮=%d | %s(%s)", ctx["sid"], round_i + 1, c["name"], c["args"])
                yield f"data: {json.dumps({'tool': {'name': c['name'], 'args': c['args']}}, ensure_ascii=False)}\n\n"
                result = await execute_tool(c["name"], c["args"], ctx)
                msgs.append({"role": "tool", "tool_call_id": c["id"], "content": result})
            # 发起下一轮；到达末轮不再传 tools，强制模型给出最终文字回答
            kwargs = dict(model=CHAT_MODEL, messages=msgs,
                          temperature=0.7, max_tokens=1024, stream=True)
            if ENABLE_TOOLS and round_i + 1 < MAX_TOOL_ROUNDS:
                kwargs["tools"] = TOOL_SCHEMAS
            elif round_i + 1 >= MAX_TOOL_ROUNDS:
                log.warning("工具调用达轮次上限，本轮强制收尾 | sid=%s | 轮=%d", ctx["sid"], round_i + 1)
            stream = await client.chat.completions.create(**kwargs)
    except APIStatusError as e:
        log.warning("闲聊模型返回状态错误 | sid=%s | status=%d", ctx["sid"], e.status_code)
        yield f"data: {json.dumps({'error': f'模型错误：{e}'}, ensure_ascii=False)}\n\n"
    except APITimeoutError:
        log.error("闲聊模型响应超时 | sid=%s", ctx["sid"])
        yield f"data: {json.dumps({'error': '模型响应超时'}, ensure_ascii=False)}\n\n"
    except Exception as e:
        log.exception("转发闲聊流失败")
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
    # 向量已成功落库，BM25 增量同步失败不应连带否决入库（下次启动 load_from_qdrant 会自愈）
    try:
        await bm25.add_chunks(ids, chunks)
    except Exception:
        log.exception("BM25 增量更新失败（向量已入库，不影响本次）| 来源=%s", source)
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
    """闲聊接口（多轮、有状态、支持 Function Calling）：按 session_id 读写 MySQL 会话历史。

    流程：落库用户消息 → 取最近 N 轮历史 → 调模型(stream, 携 tools) → 工具循环若干轮
    → 流结束后回写助手消息。工具调用过程（assistant/tool 消息）仅存在于本次请求，
    落库的仍是干净的一问一答，下一轮历史不会超长膨胀。
    会话首次创建时绑定当前用户 uid，历史与会话归属于该用户，不跨用户可见。
    """
    uid = user["uid"]
    sid = body.session_id or str(uuid.uuid4())
    # 1) 落库本轮用户消息（同时将会话归属到当前用户）
    await append_message(sid, "user", body.message, uid)

    # 2) 取最近 N 轮历史（窗口裁剪已在 DB 层完成）
    history = await load_history(sid, MAX_HISTORY)
    msgs = [{"role": "system", "content": SYSTEM_CHAT}] + history
    # 工具上下文由后端注入（来自 JWT），模型无权伪造 uid/username，防止越权查他人数据
    ctx = {"uid": uid, "username": user["username"], "sid": sid}
    log.info("收到闲聊请求 | sid=%s | 历史条数=%d | 工具=%s | 消息=%r",
             sid, len(history), "on" if ENABLE_TOOLS else "off", body.message[:50])

    # 3) 首轮请求在返回响应头之前建立，模型侧异常仍可按 HTTP 状态码标准返回
    kwargs = dict(model=CHAT_MODEL, messages=msgs,
                  temperature=0.7, max_tokens=1024, stream=True)
    if ENABLE_TOOLS:
        kwargs["tools"] = TOOL_SCHEMAS
    try:
        stream = await client.chat.completions.create(**kwargs)
        log.info("闲聊模型流已建立，开始返回 | sid=%s | model=%s", sid, CHAT_MODEL)
        return StreamingResponse(
            # 流结束后把完整助手回复回写 MySQL，形成多轮记忆闭环
            _chat_sse(stream, msgs, ctx,
                      on_complete=lambda full: append_message(sid, "assistant", full, uid)),
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

    # 按检索模式选择通路：hybrid / bm25 / vector（默认 hybrid，异常时回退纯向量）
    if RETRIEVAL_MODE == "hybrid":
        hits = await hybrid_retrieve(body.question, k=4, score_threshold=KB_SCORE_THRESHOLD)
    elif RETRIEVAL_MODE == "bm25":
        hits = await asyncio.to_thread(bm25.search, body.question, 4)
    else:
        hits = await retrieve(body.question, k=4, score_threshold=KB_SCORE_THRESHOLD)
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
    """分页返回【当前用户】的会话列表，按最近活跃时间倒序（limit/offset 均有默认值，防全表拉取）。
    额外返回 title（首条用户消息摘要）与 msg_count，供前端侧栏直接渲染。"""
    rows = await db_list_sessions(user["uid"], limit, offset)
    items = [
        {"sid": sid, "last_active": la.isoformat() if la else None,
         "title": title, "msg_count": cnt}
        for sid, la, title, cnt in rows
    ]
    log.info("查询会话列表 | uid=%d | limit=%d offset=%d | 命中 %d 条", user["uid"], limit, offset, len(items))
    return {"items": items, "limit": limit, "offset": offset}


@app.get("/api/session/{sid}/messages")
async def session_messages(sid: str, user: dict = Depends(get_current_user)):
    """返回【指定会话】的历史消息（时间正序），先校验会话归属，防越权读取他人聊天记录。"""
    owner = await get_session_owner(sid)
    if owner is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if owner != user["uid"]:
        log.warning("越权读取会话历史被拦截 | sid=%s | 归属=%d | 当前=%d", sid, owner, user["uid"])
        raise HTTPException(status_code=403, detail="无权操作该会话")
    items = await get_session_messages(sid)
    log.info("查询会话历史 | sid=%s | uid=%d | 消息条数=%d", sid, user["uid"], len(items))
    return {"sid": sid, "items": items}


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