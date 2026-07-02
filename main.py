"""
哨响AI — 统一入口
==================
提供子命令方式运行各模块。

用法:
    python main.py pipeline              # 运行自动预测+回测管道
    python main.py pipeline --daemon     # 守护模式运行管道
    python main.py pipeline --backtest  # 仅回测
    python main.py pipeline --report     # 生成准确率报告
    python main.py backend [--dev] [--port 9000]  # 启动 FastAPI 后端
    python main.py predict               # 启动预测引擎 (需要已训练模型)
    
    
    
    python main.py conv -q "巴西对阿根廷" # 单次查询
    python main.py eval                  # 运行模型上线评估流水线
    python main.py eval --quick          # 快速评估 (仅核心三项)

环境变量:
    API_HOST / API_PORT  — 覆盖后端监听地址
    DEBUG=true           — 开发模式
"""
from __future__ import annotations

import sys
import os
import argparse
import logging

logger = logging.getLogger(__name__)

# 确保项目根目录在 sys.path 中
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

def cmd_pipeline(args: argparse.Namespace) -> None:
    """运行自动预测+回测管道"""
    from pipeline.auto_pipeline import AutoPipeline

    pipeline = AutoPipeline()

    if args.daemon:
        pipeline.run_daemon(interval_minutes=args.interval)
    elif args.backtest:
        result = pipeline.backtest_finished_matches()
        print(f"回测完成: {result.get('total', 0)} 场, 准确率 {result.get('accuracy', 'N/A')}%")
    elif args.report:
        report = pipeline.generate_report()
        _print_json(report)
    else:
        result = pipeline.run_full_pipeline()
        _print_json(result)

def cmd_backend(args: argparse.Namespace) -> None:
    """启动 FastAPI 后端服务"""
    import uvicorn

    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "9000"))
    debug = args.dev or os.getenv("DEBUG", "").lower() == "true"

    # 命令行覆盖端口
    if args.port:
        port = args.port

    print(f"""
    ╔══════════════════════════════════════════╗
    ║  哨响AI 统一后端 v4.1 (+6层AI架构)      ║
    ║  FastAPI + Flask WSGI 回退              ║
    ║  http://{host}:{port}                      ║
    ║  Swagger: http://{host}:{port}/api/v1/docs ║
    ║  Legacy:  http://{host}:{port}/api/health  ║
    ╚══════════════════════════════════════════╝
    """)

    uvicorn.run(
        "backend.main:app",
        host=host,
        port=port,
        reload=debug,
        log_level="debug" if debug else "info",
    )

def cmd_predict(args: argparse.Namespace) -> None:
    """运行预测引擎"""
    from predictors.prediction_engine import PredictionEngine

    model_path = args.model if args.model else None
    engine = PredictionEngine(model_path=model_path)
    engine.run()

def cmd_agent(args: argparse.Namespace) -> None:
    """运行6层AI智能体对话 (v5.0 Six-Layer Engine)"""
    from modules.six_layer_conversation import SixLayerConversationEngine

    engine = SixLayerConversationEngine()

    if args.query:
        # 单次查询模式
        result = engine.process_single(args.query)
        print(result)
    else:
        # 交互模式
        print("""
╔══════════════════════════════════════════════════╗
║    哨响AI v5.0 — 6层AI智能体                     ║
║    输入 'exit' 退出                               ║
╚══════════════════════════════════════════════════╝
""")
        while True:
            try:
                user_input = input("\n🫵 您: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n👋 再见！")
                break
            if not user_input:
                continue
            if user_input.lower() in ('exit', 'quit', 'q'):
                print("👋 再见！")
                break
            print("\n🤖 分析中...")
            result = engine.process_single(user_input)
            print(f"\n{result}")

def cmd_conversation(args: argparse.Namespace) -> None:
    """启动6层AI对话引擎 (交互模式)"""
    from modules.six_layer_conversation import SixLayerConversationEngine

    engine = SixLayerConversationEngine(enable_l6=not args.no_l6)

    if args.demo:
        # 演示模式
        from modules.six_layer_conversation import _run_demo
        _run_demo(engine)
    elif args.query:
        # 单次查询
        odds = None
        if args.odds_home and args.odds_draw and args.odds_away:
            odds = {"home": args.odds_home, "draw": args.odds_draw, "away": args.odds_away}
        result = engine.process_single(
            args.query, args.home, args.away, args.league, odds
        )
        print(result)
    else:
        # 交互模式
        engine.run_conversation()

def cmd_eval(args: argparse.Namespace) -> None:
    """运行模型上线评估流水线"""
    from eval_pipeline import run_all_evaluators, compute_decision, generate_approval_form, get_model_info

    print("=" * 60)
    print("  哨响AI · 模型上线评估流水线")
    print(f"  模式: {'快速 (E1+E3+E6)' if args.quick else '完整 (E1~E7)'}")
    print("=" * 60)
    print()

    db_path = args.db or os.path.join(PROJECT_ROOT, "data", "football_data.db")
    results = run_all_evaluators(db_path=db_path, quick=args.quick)

    for r in results:
        icon = {"GREEN": "✅", "YELLOW": "⚠️", "RED": "🔴", "ERROR": "❌"}.get(r["rating_class"], "❓")
        print(f"  {icon} {r['expert_id']}: {r['rating']} (samples={r['sample_size']})")
        if r["error"]:
            print(f"     ERROR: {r['error'][:200]}")

    decision, reason, blocking_items = compute_decision(results)
    model_info = get_model_info()
    output_path = os.path.join(PROJECT_ROOT, "MODEL_LAUNCH_APPROVAL_FORM.md")

    if not args.no_form:
        generate_approval_form(results, decision, reason, blocking_items, model_info, output_path)
        print(f"\n  📄 审批表已生成: {output_path}")

    decision_labels = {
        "GO": "✅ GO · 评估通过 — 允许部署",
        "CONDITIONAL_GO": "⚠️ CONDITIONAL GO · 有条件通过 — 建议人工审批后部署",
        "NO_GO": "🚫 NO-GO · 评估不通过 — 部署已被阻止",
        "ERROR": "❌ ERROR · 评估异常 — 需人工介入",
    }

    print()
    print("=" * 60)
    print(f"  决策: {decision_labels.get(decision, decision)}")
    print(f"  理由: {reason}")
    if blocking_items:
        print(f"  阻塞项 ({len(blocking_items)}):")
        for item in blocking_items:
            print(f"    {item}")
    print("=" * 60)

    if decision == "NO_GO":
        sys.exit(1)
    elif decision == "ERROR":
        sys.exit(2)

def _print_json(data: object) -> None:
    """安全打印 JSON，处理编码问题"""
    import json
    text = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8", errors="replace"))

def build_parser() -> argparse.ArgumentParser:
    """构建命令行解析器"""
    parser = argparse.ArgumentParser(
        prog="main.py",
        description="哨响AI — 足球预测智能引擎 统一入口",
    )
    subparsers = parser.add_subparsers(dest="command", help="可用子命令")

    # ── pipeline 子命令 ──
    p_pipeline = subparsers.add_parser("pipeline", help="运行自动预测+回测管道")
    p_pipeline.add_argument("--daemon", action="store_true", help="守护模式，定期自动运行")
    p_pipeline.add_argument("--interval", type=int, default=30, help="守护模式运行间隔（分钟）")
    p_pipeline.add_argument("--backtest", action="store_true", help="仅回测已结束的比赛")
    p_pipeline.add_argument("--report", action="store_true", help="生成准确率分析报告")
    p_pipeline.set_defaults(func=cmd_pipeline)

    # ── backend 子命令 ──
    p_backend = subparsers.add_parser("backend", help="启动 FastAPI 后端服务")
    p_backend.add_argument("--dev", action="store_true", help="开发模式（自动重载）")
    p_backend.add_argument("--port", type=int, default=None, help="监听端口")
    p_backend.set_defaults(func=cmd_backend)

    # ── predict 子命令 ──
    p_predict = subparsers.add_parser("predict", help="运行预测引擎")
    p_predict.add_argument("--model", type=str, default=None, help="模型文件路径")
    p_predict.set_defaults(func=cmd_predict)

    # ── agent 子命令 ──
    p_agent = subparsers.add_parser("agent", help="运行智能体对话")
    p_agent.add_argument("query", nargs="?", default=None, help="查询内容")
    p_agent.set_defaults(func=cmd_agent)

    # ── conversation 子命令 (6层AI对话) ──
    p_conv = subparsers.add_parser("conversation", aliases=["conv"],
                                   help="启动6层AI对话引擎 (意图识别→专家协同→操盘解读)")
    p_conv.add_argument("--query", "-q", type=str, default=None, help="单次查询 (非交互)")
    p_conv.add_argument("--home", type=str, default=None, help="主队名")
    p_conv.add_argument("--away", type=str, default=None, help="客队名")
    p_conv.add_argument("--league", "-l", type=str, default=None, help="联赛名")
    p_conv.add_argument("--odds-home", type=float, default=None, help="主胜赔率")
    p_conv.add_argument("--odds-draw", type=float, default=None, help="平局赔率")
    p_conv.add_argument("--odds-away", type=float, default=None, help="客胜赔率")
    p_conv.add_argument("--demo", action="store_true", help="运行演示")
    p_conv.add_argument("--no-l6", action="store_true", help="禁用L6自主优化")
    p_conv.set_defaults(func=cmd_conversation)

    # ── eval 子命令 ──
    p_eval = subparsers.add_parser("eval", help="运行模型上线评估流水线 (七维评估器 + 回测 + 审批表)")
    p_eval.add_argument("--quick", action="store_true", help="快速模式：仅运行 E1+E3+E6 核心评估器")
    p_eval.add_argument("--db", type=str, default=None, help="SQLite 数据库路径")
    p_eval.add_argument("--no-form", action="store_true", help="跳过审批表生成")
    p_eval.set_defaults(func=cmd_eval)

    return parser

def main() -> None:
    """主入口"""
    parser = build_parser()
    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    )

    # 执行子命令
    args.func(args)

if __name__ == "__main__":
    main()
