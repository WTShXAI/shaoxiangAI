"""
Admin 管理端点 — 服务器重启、缓存清理等运维操作

重启原理：
  - 清理所有 __pycache__ 目录和 .pyc 文件
  - 碰触 backend/main.py 的 mtime，触发 uvicorn --reload 优雅重载
  - 前端收到响应后等待 3 秒，然后自动刷新页面
"""
import os
import sys
import subprocess
import shutil
import sqlite3
import logging
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import get_admin_user

router = APIRouter()
logger = logging.getLogger(__name__)


class RestartResponse(BaseModel):
    status: str
    message: str
    action: str


@router.post("/restart", response_model=RestartResponse, tags=["管理"])
async def restart_server(user: dict = Depends(get_admin_user)):
    """
    重启后端服务器（触发 uvicorn --reload 优雅重载）

    步骤：
      1. 清理项目下所有 __pycache__ 目录和 .pyc 文件
      2. 碰触 backend/main.py 触发 uvicorn 监听的重载事件
      3. 立即返回成功响应
      4. uvicorn 在 1-2 秒内完成优雅重载
    """
    try:
        project_root = Path(__file__).resolve().parent.parent.parent.parent.parent
        backend_dir = project_root / "backend"
        main_py = backend_dir / "main.py"

        # ── 1. 清理缓存 ──
        pycache_count = 0
        pyc_count = 0

        for pycache_dir in project_root.rglob("__pycache__"):
            try:
                shutil.rmtree(pycache_dir, ignore_errors=True)
                pycache_count += 1
            except (OSError, PermissionError) as e:
                logger.debug(f"清理缓存目录失败 {pycache_dir}: {e}")

        for pyc_file in project_root.rglob("*.pyc"):
            try:
                pyc_file.unlink(missing_ok=True)
                pyc_count += 1
            except OSError as e:
                logger.debug(f"删除.pyc文件失败 {pyc_file}: {e}")

        # ── 2. 触发 uvicorn --reload 重载 ──
        # 原理：uvicorn --reload 监听 .py 文件 mtime，碰一下就触发重载
        try:
            # 碰触 main.py（更新 mtime，内容不变）
            os.utime(str(main_py), None)
            logger.warning(
                f"[ADMIN] {user.get('username', '?')} 触发重启 | "
                f"清理了 {pycache_count} 个 __pycache__, {pyc_count} 个 .pyc | "
                f"已触发 uvicorn reload（main.py mtime 已更新）"
            )
        except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError) as touch_err:
            logger.warning(f"[ADMIN] 碰触 main.py 失败（不影响重启）: {touch_err}")
            # fallback: 写一个临时 .py 文件触发 reload
            trigger = backend_dir / ".restart_trigger.py"
            trigger.write_text(f"# restart trigger {os.getpid()}\n")
            logger.warning(f"[ADMIN] 已写入触发文件 {trigger}")

        return RestartResponse(
            status="restarting",
            message=(
                f"服务器正在重启，已清理 {pycache_count} 个缓存目录。"
                f" 请等待 2-3 秒后刷新页面。"
            ),
            action="server_reload",
        )

    except (sqlite3.Error, sqlalchemy.exc.SQLAlchemyError) as e:
        logger.error(f"[ADMIN] 重启失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"重启失败: {str(e)}")


@router.post("/clear-cache", tags=["管理"])
async def clear_cache(user: dict = Depends(get_admin_user)):
    """仅清理 __pycache__ 缓存（不触发重启）"""
    try:
        project_root = Path(__file__).resolve().parent.parent.parent.parent.parent
        pycache_count = 0
        pyc_count = 0

        for pycache_dir in project_root.rglob("__pycache__"):
            try:
                shutil.rmtree(pycache_dir, ignore_errors=True)
                pycache_count += 1
            except (OSError, PermissionError) as e:
                logger.debug(f"清理缓存目录失败 {pycache_dir}: {e}")

        for pyc_file in project_root.rglob("*.pyc"):
            try:
                pyc_file.unlink(missing_ok=True)
                pyc_count += 1
            except OSError as e:
                logger.debug(f"删除.pyc文件失败 {pyc_file}: {e}")

        logger.info(
            f"[ADMIN] {user.get('username', '?')} 清理缓存 | "
            f"__pycache__={pycache_count}, .pyc={pyc_count}"
        )

        return {
            "status": "ok",
            "message": f"缓存已清理：{pycache_count} 个目录, {pyc_count} 个 .pyc 文件",
            "pycache_removed": pycache_count,
            "pyc_removed": pyc_count,
        }

    except (ValueError, KeyError, FileNotFoundError) as e:
        logger.error(f"[ADMIN] 清理缓存失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"清理缓存失败: {str(e)}")
