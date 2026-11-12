# backend/db.py
# 本模块职责：MySQL 存储层——会话历史（messages / sessions 两张表）与用户表（users）的读写。
# 全程使用 SQLAlchemy 异步引擎 + aiomysql 驱动，与 FastAPI 的事件循环配合。
import os
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import text
load_dotenv(Path(__file__).resolve().parent / ".env")   # ★ 读取本目录下的 .env，与运行目录/导入顺序无关

engine = create_async_engine(os.environ["DB_URL"], pool_pre_ping=True, pool_size=5)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

async def append_message(sid: str, role: str, content: str, uid: int | None = None):
    """写入一条消息，并同步维护会话表：
    - messages：插入 (sid, role, content)；
    - sessions：不存在则以 uid 为归属新建（首次闲聊时绑定当前用户），
      已存在仅刷新 last_active——绝不覆写归属，防止会话被劫持。
    两步在同一事务中提交，保证消息与会话记录一致。
    """
    async with SessionLocal() as s:
        await s.execute(
            text("INSERT INTO messages (sid, role, content) VALUES (:sid, :role, :c)"),
            {"sid": sid, "role": role, "c": content},
        )
        # 会话不存在则新建并记录归属 uid；已存在则仅刷新活跃时间（不改归属，防覆写）
        await s.execute(
            text("INSERT INTO sessions (sid, uid) VALUES (:sid, :uid) "
                 "ON DUPLICATE KEY UPDATE last_active = NOW()"),
            {"sid": sid, "uid": uid},
        )
        await s.commit()

async def load_history(sid: str, max_turns: int) -> list[dict]:
    """取会话最近 max_turns 轮历史（一轮 = user + assistant 两条），返回 [{role, content}, ...]。
    窗口裁剪下沉到 SQL（倒序取 2N 条再翻正），不在内存里切片。"""
    async with SessionLocal() as s:
        r = await s.execute(
            text("SELECT role, content FROM messages WHERE sid=:sid "
                 "ORDER BY id DESC LIMIT :n"),
            {"sid": sid, "n": max_turns * 2},
        )
        rows = [{"role": role, "content": c} for role, c in r.all()]
        rows.reverse()          # 取的是最近N条倒序，翻正回时间顺序
        return rows

async def clear_session(sid: str):
    """删除会话：messages 与 sessions 中的记录一并清除（由鉴权后的 DELETE /api/session/{sid} 调用）。"""
    async with SessionLocal() as s:
        await s.execute(text("DELETE FROM messages WHERE sid=:sid"), {"sid": sid})
        await s.execute(text("DELETE FROM sessions WHERE sid=:sid"), {"sid": sid})
        await s.commit()

async def list_sessions(uid: int, limit: int, offset: int) -> list[tuple]:
    """按最近活跃时间倒序分页返回【指定用户】的会话列表
    [(sid, last_active, title, msg_count), ...]。
    title 取会话首条用户消息前 40 字，供前端侧栏做摘要展示；无用户消息时为 NULL。"""
    async with SessionLocal() as s:
        r = await s.execute(
            text("SELECT s.sid, s.last_active, "
                 "(SELECT LEFT(m.content, 40) FROM messages m "
                 "  WHERE m.sid = s.sid AND m.role = 'user' ORDER BY m.id ASC LIMIT 1) AS title, "
                 "(SELECT COUNT(*) FROM messages m WHERE m.sid = s.sid) AS msg_count "
                 "FROM sessions s WHERE s.uid=:uid "
                 "ORDER BY s.last_active DESC LIMIT :lim OFFSET :off"),
            {"uid": uid, "lim": limit, "off": offset},
        )
        return r.all()

async def get_session_messages(sid: str, limit: int = 200) -> list[dict]:
    """按时间正序返回会话的历史消息（最多最近 limit 条），供前端侧栏切换会话时回放。
    倒序取 N 条再翻正，与 load_history 同样的窗口裁剪策略。"""
    async with SessionLocal() as s:
        r = await s.execute(
            text("SELECT role, content, created_at FROM messages WHERE sid=:sid "
                 "ORDER BY id DESC LIMIT :lim"),
            {"sid": sid, "lim": limit},
        )
        rows = [{"role": role, "content": c, "created_at": str(created_at)}
                for role, c, created_at in r.all()]
        rows.reverse()
        return rows

async def get_session_owner(sid: str) -> int | None:
    """返回会话的归属 uid（用于校验会话所有权）；会话不存在返回 None。"""
    async with SessionLocal() as s:
        r = await s.execute(text("SELECT uid FROM sessions WHERE sid=:sid"), {"sid": sid})
        row = r.first()
        return row[0] if row else None


# ---------- 用户表读写 ----------
async def create_user(username: str, pw_hash: str) -> int:
    """插入新用户，返回自增 uid；用户名重复时由 UNIQUE 约束报 IntegrityError，由调用方捕获。"""
    async with SessionLocal() as s:
        r = await s.execute(
            text("INSERT INTO users (username, pw_hash) VALUES (:u, :p)"),
            {"u": username, "p": pw_hash},
        )
        await s.commit()
        return r.lastrowid

async def get_user_by_username(username: str) -> tuple | None:
    """按用户名查用户，返回 (uid, pw_hash) 或 None（不区分“用户不存在/密码错”以避免探测）。"""
    async with SessionLocal() as s:
        r = await s.execute(
            text("SELECT uid, pw_hash FROM users WHERE username=:u"), {"u": username},
        )
        return r.first()


# ---------- 工具调用（Function Calling）专用查询 ----------
async def get_user_profile(uid: int | None = None, username: str | None = None) -> dict | None:
    """按 uid 或用户名查用户资料，只回非敏感字段（uid/username/created），pw_hash 绝不出 DB 层。"""
    if uid is None and not username:
        return None
    sql = ("SELECT uid, username, created FROM users "
           + ("WHERE uid=:uid" if uid is not None else "WHERE username=:u"))
    async with SessionLocal() as s:
        r = await s.execute(text(sql), {"uid": uid} if uid is not None else {"u": username})
        row = r.first()
        if not row:
            return None
        return {"uid": row[0], "username": row[1], "created": row[2]}


async def search_messages(uid: int, keyword: str, days: int, limit: int) -> list[dict]:
    """在【指定用户】的全部会话消息里按关键词模糊检索，供 search_chat_history 工具使用。

    安全要点：messages 表没有 uid 列，必须 JOIN sessions 用 s.uid 过滤，绝不可全表 LIKE，
    否则会搜到其他用户的对话。关键词先转义百分号、下划线、反斜杠再拼 LIKE，避免被当成通配符放大匹配。
    """
    escaped = keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    async with SessionLocal() as s:
        r = await s.execute(
            text("SELECT m.sid, m.role, m.content, m.created_at FROM messages m "
                 "JOIN sessions ss ON m.sid = ss.sid "
                 "WHERE ss.uid=:uid AND m.content LIKE :kw ESCAPE '\\\\' "
                 "  AND m.created >= NOW() - INTERVAL :days DAY "
                 "ORDER BY m.id DESC LIMIT :lim"),
            {"uid": uid, "kw": f"%{escaped}%", "days": days, "lim": limit},
        )
        return [
            {"sid": sid, "role": role, "created_at": str(created_at), "content": content[:200]}
            for sid, role, content, created_at in r.all()
        ]