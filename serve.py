"""
哨响AI 后端启动器 (serve.py)
============================
隔离式启动包装,不改动任何现有源码。
解决两个已知的包冲突问题:
  1. 根目录与 backend/ 同名包冲突 (core / database)
     → 把 backend/ 优先插入 sys.path,让 `from core.config` 解析到 backend/core/
  2. 旧版 start_backend.py 末尾 `from main import build_parser` 误解析到 backend/main.py
     → 本启动器不走 build_parser,直接调 uvicorn 启动

用法 (等价于 start.bat 的启动效果):
    python serve.py                 # 默认 0.0.0.0:9000
    python serve.py --port 9000
    python serve.py --reload        # 开发模式 (热重载)

环境变量 (与 start.bat 一致):
    API_HOST / API_PORT / DEBUG / SECRET_KEY / CUDA_VISIBLE_DEVICES
"""
from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")

# ── 修复: backend/ 优先,解决 core/database 同名包冲突 ──
# (与 start_backend.py 第16-25行一致,这是验证过的核心修复)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(1, PROJECT_ROOT)
# 清理可能已缓存的同名顶层包,避免 backend/core 被根目录空 core/ 抢占
for _mod in list(sys.modules):
    if _mod in ("core", "database") or _mod.startswith("core.") or _mod.startswith("database."):
        del sys.modules[_mod]

# 切到项目根,保证相对路径 (data/ saved_models/ logs/ 等) 正确
os.chdir(PROJECT_ROOT)


def main() -> None:
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="哨响AI 后端启动器")
    parser.add_argument("--host", default=os.getenv("API_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("API_PORT", "9000")))
    parser.add_argument("--reload", action="store_true",
                        help="热重载 (仅供开发改代码时用; 日常使用不要开)")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    # 日常使用默认不热重载 (reload 模式会监听整个项目目录,
    # 日志文件写入会触发 watchfiles 反馈循环, 狂吃 CPU)。
    # 只在显式传 --reload 时才开启。
    reload = args.reload

    print(f"""
    ╔══════════════════════════════════════════╗
    ║  哨响AI 后端服务启动中                    ║
    ║  地址:   http://{args.host}:{args.port:<16} ║
    ║  文档:   /api/v1/docs                     ║
    ║  健康:   /api/v1/monitor/health           ║
    ║  热重载: {reload!s:<27} ║
    ╚══════════════════════════════════════════╝
    """)

    uvicorn.run(
        "backend.main:app",
        host=args.host,
        port=args.port,
        reload=reload,
        log_level="debug" if reload else args.log_level,
    )


if __name__ == "__main__":
    main()
