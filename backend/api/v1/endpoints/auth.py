"""
认证 API — 登录/注销/用户信息

⚠️ 认证已禁用 — 登录端点接收任何输入都返回 admin token。
保留端点以便旧客户端不报错。
"""
import logging
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.security import (
    create_access_token, get_current_user,
    UserOut,
)
from core.database import get_db

logger = logging.getLogger(__name__)
router = APIRouter()

class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut

@router.post("/login", response_model=LoginResponse)
async def login(
    username: str = "anonymous",
    password: str = "anonymous",
    db: Session = Depends(get_db),
):
    """用户登录 — 认证已禁用，接收任何输入都返回 admin token。"""
    # 认证已禁用：直接签发 token
    token = create_access_token({
        "sub": "admin",
        "role": "admin",
    })
    return LoginResponse(
        access_token=token,
        user=UserOut(
            username="admin",
            role="admin",
            email="admin@localhost",
        ),
    )

@router.get("/me", response_model=UserOut)
async def get_me(user: UserOut = Depends(get_current_user)):
    """获取当前用户信息 — 认证已禁用，始终返回 admin。"""
    return user

@router.get("/users")
async def list_users(
    user: UserOut = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """列出用户（管理员）— 认证已禁用，直接放行。"""
    # 认证已禁用：返回固定 admin 用户
    return [
        {"username": "admin", "role": "admin", "email": "admin@localhost"}
    ]
