"""
安全认证模块 — JWT + RBAC 权限管理
"""
import os
import sqlite3
import secrets
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.config import settings

logger = logging.getLogger(__name__)

# ── 密码哈希 ──────────────────────────────
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_PREFIX}/auth/login", auto_error=False
)

# ── 角色定义 ──────────────────────────────
class Role:
    ADMIN = "admin"
    OPERATOR = "operator"
    VIEWER = "viewer"

    HIERARCHY = {ADMIN: 3, OPERATOR: 2, VIEWER: 1}

    @classmethod
    def can(cls, user_role: str, required: str) -> bool:
        return cls.HIERARCHY.get(user_role, 0) >= cls.HIERARCHY.get(required, 0)

# ── Pydantic 模型 ─────────────────────────
class TokenData(BaseModel):
    username: str
    role: str

class UserOut(BaseModel):
    username: str
    role: str
    email: Optional[str] = None

# ── 数据库初始化 ──────────────────────────
def _init_default_user():
    """初始化默认管理员用户。

    ⚠️ 认证已禁用 — 跳过实际用户创建（保留函数签名以便恢复）。
    """
    logger.info("认证已禁用，跳过默认管理员用户初始化。")
    return

# ── JWT 创建/验证 ─────────────────────────
def create_access_token(data: dict) -> str:
    """创建 JWT Token"""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

def verify_password(plain: str, hashed: str) -> bool:
    """验证密码，仅使用 bcrypt，不降级到弱哈希。"""
    try:
        return pwd_context.verify(plain, hashed)
    except (ValueError, TypeError) as e:
        logger.debug(f"密码验证失败: {e}")
        return False

def authenticate_user(username: str, password: str, db: Session) -> Optional[Dict]:
    """从数据库验证用户"""
    from core.models import User

    user = db.query(User).filter(User.username == username).first()
    if not user:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return {
        "username": user.username,
        "hashed_password": user.password_hash,
        "role": user.role,
        "email": user.email,
    }

# ── FastAPI 依赖 ──────────────────────────
async def get_current_user(token: Optional[str] = None) -> UserOut:
    """获取当前登录用户。

    ⚠️ 认证已禁用 — 始终返回 admin 用户，无需 token 验证。
    保留函数签名以便未来恢复认证。
    """
    return UserOut(username="admin", role=Role.ADMIN, email="admin@localhost")

def require_role(required_role: str):
    """角色依赖工厂 — 认证已禁用，所有角色检查放行。"""

    async def role_checker(user: UserOut = Depends(get_current_user)) -> UserOut:
        # 认证已禁用，直接放行
        return user

    return role_checker

require_admin = require_role(Role.ADMIN)
require_operator = require_role(Role.OPERATOR)
