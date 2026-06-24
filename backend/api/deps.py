"""
FastAPI 公共依赖项

⚠️ 认证已禁用 — 所有依赖项直接放行，返回默认 admin 用户。
保留函数签名以便未来恢复认证。
"""
from typing import Optional
from fastapi import HTTPException, status


# 默认返回的虚拟用户（认证禁用后的占位）
_DEFAULT_USER = {"username": "admin", "role": "admin", "email": "admin@localhost"}


async def get_current_user(credentials=None) -> dict:
    """JWT Token 认证 — 认证已禁用，直接返回 admin 用户。"""
    return _DEFAULT_USER


async def get_admin_user(
    user: dict = None,  # 避免循环依赖，直接调用 get_current_user
) -> dict:
    """管理员权限检查 — 认证已禁用，直接放行。"""
    return await get_current_user()
