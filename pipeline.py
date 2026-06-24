"""
预测闭环流水线 v2.0 — 集成 PredictionGuard 守护
================================================
每日运行: 守护预测 → 入库验证 → 评估 → 报告

完整闭环 (带守护):
  0. 独立审计现有预测数据质量
  1. 🛡 带守护预测未来7天比赛 (PredictionGuard)
  2. 保存预测到数据库 (prediction_tracker)
  3. 评估已完成比赛 (回填比分)
  4. 生成复盘报告
  5. 检查10场挑战进度
  6. 自动调优门控参数 (如准确率不达预期)

使用:
  python pipeline.py --run                 # 执行完整流水线(带守护)
  python pipeline.py --predict-only        # 仅预测
  python pipeline.py --predict-only --guard # 带守护的预测
  python pipeline.py --eval-only           # 仅评估
  python pipeline.py --report-only         # 仅生成报告
  python pipeline.py --audit               # 独立审计
"""
import sys, os, logging, json
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger('Pipeline')

# 添加项目路径
sys.path.insert(0, os.path.dirname(__file__))

def run_pipeline(model_path: str = 'saved_models/football_ensemble_20260613_150555.joblib',
                 use_guard: bool = False):
    """
    执行完整预测闭环流水线 (可选PredictionGuard守护)
    """
    from selective_predictor import SelectivePredictor
    from prediction_tracker import PredictionTracker

    logger.info("="*80)
    logger.info("  预测闭环流水线 v2.0 启动")
    if use_guard:
        logger.info("  🛡 PredictionGuard 守护模式已启用")
    logger.info("  "+datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    logger.info("="*80)

    sp = SelectivePredictor(model_path=model_path)
    tracker = PredictionTracker()

    # ── Step 1: 预测未来比赛 ──
    logger.info("\n[Step 1/5] 预测未来7天比赛...")

    if use_guard:
        from prediction_guard import PredictionGuard
        guard = PredictionGuard()
        results, guard_report = guard.guarded_predict(sp, days_ahead=7)

        # 根据守护报告决策
        if guard_report.final_decision == "BLOCKED":
            logger.error("  ❌ PredictionGuard 阻断预测，流水线终止")
            logger.error(f"  原因: {guard_report.summary}")
            return {
                'status': 'BLOCKED',
                'guard_report': guard_report,
                'predictions_saved': 0,
            }
        elif guard_report.final_decision == "DEGRADED":
            logger.warning(f"  ⚠ 预测结果降级: {guard_report.summary}")
    else:
        results = sp.predict_upcoming_matches(days_ahead=7)
        guard_report = None

    if results:
        qualified = [r for r in results if r.tier in ['S', 'A', 'B']]
        logger.info(f"  命中 {len(results)} 场, 其中S/A/B级 {len(qualified)} 场")

        if qualified:
            # ── SAVE阶段: 入库前类型安全检查 ──
            for r in qualified:
                r.match_id = int(r.match_id) if not isinstance(r.match_id, int) else r.match_id
                r.predicted_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            saved = tracker.save_predictions(qualified)
            logger.info(f"  已保存 {saved} 条预测到数据库")
        else:
            saved = 0

        sp.print_report(qualified[:20], "未来7天预测")
    else:
        qualified = []
        saved = 0
        logger.info("  未来7天无可用比赛数据")

    # ── Step 2: 评估已完成的预测 ──
    logger.info("\n[Step 2/5] 评估已完成比赛...")
    eval_result = tracker.evaluate(days_back=7)

    if eval_result.get('evaluated', 0) > 0:
        logger.info(f"  评估了 {eval_result['evaluated']} 条预测")
        logger.info(f"  整体准确率: {eval_result['accuracy']}%")
        for tier, stats in eval_result.get('tier_stats', {}).items():
            logger.info(f"  [{tier}] {stats['count']}场, 准确率{stats['accuracy']}%")
    else:
        logger.info("  无需评估的预测")

    # ── Step 3: 生成复盘报告 ──
    logger.info("\n[Step 3/5] 生成复盘报告...")
    report = tracker.generate_report(days_back=30)

    report_path = f"output/pipeline_report_{datetime.now().strftime('%Y%m%d_%H%M')}.md"
    os.makedirs('output', exist_ok=True)
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)
    logger.info(f"  报告已保存: {report_path}")

    # ── Step 4: 检查挑战进度 ──
    logger.info("\n[Step 4/5] 检查10场挑战进度...")
    progress = tracker.get_challenge_progress()

    if progress.get('status') == 'no_active_challenge':
        logger.info("  当前无活跃挑战")
        logger.info("  提示: 运行 'python prediction_tracker.py start' 开始新挑战")
    else:
        c = progress['challenge']
        p = progress['progress']
        logger.info(f"  挑战: {c['name']}")
        logger.info(f"  进度: {p['correct']}/{p['total_evaluated']} = {p['accuracy']}%")

        if p['accuracy'] >= c['target']:
            logger.info(f"  ★★★ 目标达成! 准确率已达到{c['target']}%!")
        elif p['remaining_to_target'] > 0:
            logger.info(f"  还需评估 {p['remaining_to_target']} 场以达到10场")

        if progress.get('recent_results'):
            logger.info(f"  最近结果:")
            for r in progress['recent_results'][:5]:
                icon = '✓' if r['correct'] else '✗'
                logger.info(f"    {icon} {r['match_date']} {r['home_team'][:15]} vs {r['away_team'][:15]} "
                          f"预测{r['predicted']} 实际{r['actual']} [{r['tier']}]")

    # ── Step 5: 守护审计(如果启用) ──
    if use_guard and guard_report:
        logger.info("\n[Step 5/5] PredictionGuard 审计...")
        guard.print_audit()

    # ── 汇总 ──
    logger.info("\n" + "="*80)
    logger.info("  流水线执行完成!")
    logger.info("  " + datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    logger.info("="*80)

    return {
        'predictions_saved': saved,
        'evaluated': eval_result.get('evaluated', 0),
        'eval_accuracy': eval_result.get('accuracy', 0),
        'report_path': report_path,
        'challenge_progress': progress,
        'guard_report': guard_report,
    }


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='预测闭环流水线 v2.0')
    parser.add_argument('--model', default='saved_models/football_ensemble_20260613_150555.joblib',
                       help='模型路径')
    parser.add_argument('--run', action='store_true', help='执行完整流水线(带守护)')
    parser.add_argument('--predict-only', action='store_true', help='仅预测')
    parser.add_argument('--eval-only', action='store_true', help='仅评估')
    parser.add_argument('--report-only', action='store_true', help='仅生成报告')
    parser.add_argument('--audit', action='store_true', help='独立审计现有预测数据')
    parser.add_argument('--guard', action='store_true', help='启用PredictionGuard守护')
    parser.add_argument('--strict', action='store_true', help='守护严格模式(WARN也阻断)')

    args = parser.parse_args()

    if not any([args.run, args.predict_only, args.eval_only, args.report_only, args.audit]):
        parser.print_help()
        sys.exit(1)

    from selective_predictor import SelectivePredictor
    from prediction_tracker import PredictionTracker

    sp = SelectivePredictor(model_path=args.model)
    tracker = PredictionTracker()

    if args.audit:
        from prediction_guard import audit_predictions
        logger.info("🔍 启动独立审计模式...")
        report = audit_predictions()
        guard = __import__('prediction_guard', fromlist=['PredictionGuard']).PredictionGuard()
        guard.current_report = report
        guard.print_audit()

    elif args.predict_only:
        use_guard = args.guard
        if use_guard:
            from prediction_guard import PredictionGuard
            guard = PredictionGuard(strict_mode=args.strict)
            results, report = guard.guarded_predict(sp, days_ahead=7)
        else:
            results = sp.predict_upcoming_matches(days_ahead=7)

        if results:
            qualified = [r for r in results if r.tier in ['S', 'A', 'B']]
            # 类型安全检查
            for r in qualified:
                r.match_id = int(r.match_id) if not isinstance(r.match_id, int) else r.match_id
                r.predicted_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            tracker.save_predictions(qualified)
            sp.print_report(qualified[:20])
        else:
            logger.warning("无可用预测结果")

    elif args.eval_only:
        result = tracker.evaluate(days_back=7)
        print(f"评估: {result}")

    elif args.report_only:
        report = tracker.generate_report(days_back=30)
        print(report)

    elif args.run:
        use_guard = args.guard or True  # --run 默认启用守护
        run_pipeline(model_path=args.model, use_guard=use_guard)
