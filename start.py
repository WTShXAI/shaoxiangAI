#!/usr/bin/env python
"""
哨响AI v4.0 — 单文件一键启动
===============================
单人维护版: 一行命令启动全部服务。

用法:
  python start.py              # 启动 FastAPI 后端
  python start.py --cli        # 启动 CLI 对话模式
  python start.py --demo       # 运行6层架构演示
  python start.py --train      # 进入训练模式

配置:
  所有参数在 config/settings.yaml 中管理

版本: v4.1-solo · 2026-06-19
"""
import sys
import os
import argparse

# 确保项目根在路径中
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

def main():
    parser = argparse.ArgumentParser(
        description="哨响AI v4.0 — 单人维护版一键启动",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python start.py              # FastAPI 后端 (http://localhost:8000)
  python start.py --cli        # CLI 对话模式
  python start.py --demo       # 6层架构演示
  python start.py --port 8080  # 自定义端口
""")

    parser.add_argument("--cli", action="store_true", help="CLI 对话模式")
    parser.add_argument("--demo", action="store_true", help="运行6层架构演示")
    parser.add_argument("--port", type=int, default=None, help="服务端口 (默认: 8000)")
    parser.add_argument("--host", type=str, default=None, help="服务地址 (默认: 0.0.0.0)")
    parser.add_argument("--pure", action="store_true", help="纯净v3.2模式 (所有新功能关闭)")

    args = parser.parse_args()

    # ── 加载配置 ──
    from config.settings import load_config, get_setting
    cfg = load_config()

    # 纯净模式
    if args.pure:
        import yaml
        config_path = os.path.join(PROJECT_ROOT, 'config', 'settings.yaml')
        with open(config_path, 'r', encoding='utf-8') as f:
            cfg = yaml.safe_load(f)
        cfg['global_switches']['pure_v32_mode'] = True
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)
        print("🔵 已切换至 v3.2 纯净模式 (所有扩展功能关闭)")
        print("   恢复: 编辑 config/settings.yaml, 将 pure_v32_mode 改为 false")

    port = args.port or get_setting('server.port', 8000)
    host = args.host or get_setting('server.host', '0.0.0.0')

    # ── CLI 模式 ──
    if args.cli:
        from modules.six_layer_conversation import SixLayerConversationEngine
        engine = SixLayerConversationEngine()
        engine.run_conversation()
        return

    # ── 演示模式 ──
    if args.demo:
        from modules.six_layer_conversation import SixLayerConversationEngine, _run_demo
        engine = SixLayerConversationEngine()
        _run_demo(engine)
        return

    # ── FastAPI 后端 (默认) ──
    print(f"""
╔══════════════════════════════════════════════╗
║    哨响AI v4.1 — 单人维护版                  ║
║    FastAPI 后端                              ║
║    http://{host}:{port}                          ║
║    Swagger: http://{host}:{port}/api/v1/docs     ║
╚══════════════════════════════════════════════╝
""")
    import uvicorn
    from backend.main import app
    uvicorn.run(app, host=host, port=port, reload=False)

if __name__ == "__main__":
    main()
