"""
哨响AI 后端启动器 (serve.py)
============================
隔离式启动包装，解决 backend/ 与根目录同名包冲突。

用法:
    python serve.py                        # 默认 0.0.0.0:9000
    python serve.py --port 9000
    python serve.py --reload               # 开发模式（热重载）

环境变量:
    API_HOST / API_PORT / DEBUG / SECRET_KEY / CUDA_VISIBLE_DEVICES
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.join(PROJECT_ROOT, "backend")

# backend/ 优先，解决 core/database 同名包冲突
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.append(PROJECT_ROOT)

# 清理已缓存的同名顶层包，避免根目录 core 与 backend/core 冲突
for mod in list(sys.modules):
    if mod in ("core", "database") or mod.startswith("core.") or mod.startswith("database."):
        del sys.modules[mod]

os.chdir(PROJECT_ROOT)


def main() -> None:
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="哨响AI 后端启动器")
    parser.add_argument("--host", default=os.getenv("API_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("API_PORT", "9000")))
    parser.add_argument("--reload", action="store_true", help="热重载（仅开发时使用）")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

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
