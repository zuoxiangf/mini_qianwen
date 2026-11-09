# backend/auth.py
# 本模块职责：鉴权基础设施——密码哈希、JWT 签发/校验、FastAPI 取当前用户的依赖。
# 采用业内统一的无状态 Bearer Token 方案：登录换取 JWT，后续请求带 Authorization 头。
import os
import secrets
import logging
import datetime as dt
from pathlib import Path

import bcrypt
import jwt
from dotenv import load_dotenv
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

# ★ 先锚定本目录 .env 再读环境变量（与 db.py / vector.py 一致，避免 import 期 KeyError）
load_dotenv(Path(__file__).resolve().parent / ".env")

log = logging.getLogger("auth")

# ---------- 配置 ----------
JWT_ALG = "HS256"
JWT_EXPIRE_MINUTES = int(os.environ.get("JWT_EXPIRE_MINUTES", "1440"))   # 默认 24 小时
# 生产必须在 .env 配 JWT_SECRET；未配置则生成临时随机密钥并告警（重启即失效，仅限本地开发）
_secret = os.environ.get("JWT_SECRET", "").strip()
if not _secret:
    _secret = secrets.token_hex(32)
    log.warning("未配置 JWT_SECRET，已生成临时随机密钥；重启后旧 token 全部失效，生产务必在 .env 设置！")
JWT_SECRET = _secret

# 用户名/密码基本校验规则
USERNAME_MIN, USERNAME_MAX = 3, 32
PASSWORD_MIN = 6


# ---------- 密码哈希（bcrypt，自带盐、抗彩虹表）----------
def hash_password(password: str) -> str:
    """生成 bcrypt 哈希串（固定 60 字符，内含随机盐）；计算量百毫秒级，调用方须放线程池。"""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False          # 哈希串损坏时按校验失败处理，不抛栈


# ---------- JWT 签发 / 校验 ----------
def create_token(uid: int, username: str) -> str:
    """签发 HS256 JWT：payload 携带 uid（sub）与用户名，有效期由 JWT_EXPIRE_MINUTES 控制。"""
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": str(uid),           # 标准声明：主体（用户 id，JWT 规范要求字符串）
        "username": username,
        "iat": now,                # 签发时间
        "exp": now + dt.timedelta(minutes=JWT_EXPIRE_MINUTES),   # 过期时间
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)


def decode_token(token: str) -> dict:
    """校验签名与有效期，返回 payload；任何异常统一抛 401。"""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="登录已过期，请重新登录")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="无效的身份凭证")


# ---------- FastAPI 依赖：取当前登录用户 ----------
_bearer = HTTPBearer(auto_error=False)     # 关自动报错，手动统一返回 401 而非默认的 403


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    """从 Authorization: Bearer <token> 解析出 {uid, username}；缺失或非法一律 401。
    无状态设计：只验签名不查库，所有业务接口（chat/ask/kb_*）均依赖此函数。"""
    if creds is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="未登录或缺少身份凭证")
    payload = decode_token(creds.credentials)
    return {"uid": int(payload["sub"]), "username": payload.get("username", "")}
