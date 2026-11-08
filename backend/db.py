# backend/db.py
import os
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import text
load_dotenv(Path(__file__).resolve().parent / ".env")   # ★ 读取本目录下的 .env，与运行目录/导入顺序无关

engine = create_async_engine(os.environ["DB_URL"], pool_pre_ping=True, pool_size=5)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)

async def append_message(sid: str, role: str, content: str):
    async with SessionLocal() as s:
        await s.execute(
            text("INSERT INTO messages (sid, role, content) VALUES (:sid, :role, :c)"),
            {"sid": sid, "role": role, "c": content},
        )
        await s.execute(
            text("INSERT INTO sessions (sid) VALUES (:sid) "
                 "ON DUPLICATE KEY UPDATE last_active = NOW()"),
            {"sid": sid},
        )
        await s.commit()

async def load_history(sid: str, max_turns: int) -> list[dict]:
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
    async with SessionLocal() as s:
        await s.execute(text("DELETE FROM messages WHERE sid=:sid"), {"sid": sid})
        await s.commit()