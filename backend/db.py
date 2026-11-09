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
    """按最近活跃时间倒序分页返回【指定用户】的会话列表 [(sid, last_active), ...]。"""
    async with SessionLocal() as s:
        r = await s.execute(
            text("SELECT sid, last_active FROM sessions WHERE uid=:uid "
                 "ORDER BY last_active DESC LIMIT :lim OFFSET :off"),
            {"uid": uid, "lim": limit, "off": offset},
        )
        return r.all()

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