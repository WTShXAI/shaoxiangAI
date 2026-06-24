import logging
"""
一键优化脚本
"""
import subprocess
import sys
from pathlib import Path
import os
import time
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent


def run_command(cmd: str, description: str, timeout: int = 600):
    """运行命令并打印输出"""
    logger.info(f"\n{'='*60}")
    logger.info(f"🔧 {description}")
    logger.info(f"💻 命令: {cmd}")
    logger.info(f"{'='*60}")
    start = time.time()

    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=timeout,
            cwd=str(PROJECT_ROOT),
        )

        elapsed = time.time() - start

        if result.returncode == 0:
            logger.info(f"✅ 成功 ({elapsed:.0f}s)")
            if result.stdout:
                # 显示最后20行
                lines = result.stdout.strip().split('\n')
                for line in lines[-20:]:
                    logger.info(f"  {line}")
        else:
            logger.info(f"❌ 失败 ({elapsed:.0f}s)")
            if result.stderr:
                logger.info(f"  错误: {result.stderr[-500:]}")
            if result.stdout:
                logger.info(f"  输出: {result.stdout[-500:]}")

        return result.returncode

    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        logger.info(f"⏰ 超时 ({elapsed:.0f}s > {timeout}s)")
        return 1
    except (ValueError, KeyError, FileNotFoundError) as e:
        elapsed = time.time() - start
        logger.info(f"💥 异常: {e}")
        return 1


def check_prerequisites():
    """检查前置条件"""
    logger.info("🔍 检查前置条件...")

    checks = [
        ("增强数据", "data/enhanced_features_v1.csv"),
        ("FootballAI模型", "saved_models/footballai_enhanced_v4.0.joblib"),
        ("FootballAI V5", "saved_models/footballai_v5.0_30000.joblib"),
    ]

    all_ok = True
    for name, path in checks:
        full_path = PROJECT_ROOT / path
        if full_path.exists():
            logger.info(f"  ✅ {name}: {path}")
        else:
            logger.info(f"  ⚠️ {name}: {path} (不存在)")
            all_ok = False

    return all_ok


def main():
    """主函数"""
    logger.info("🚀 一键系统优化开始")
    logger.info(f"  项目目录: {PROJECT_ROOT}")
    logger.info("=" * 60)

    # 前置检查
    if not check_prerequisites():
        logger.info("\n⚠️ 部分前置条件不满足，将跳过相关步骤")

    steps = [
        # 1. 特征压缩: 58 → 35
        (
            "python backend/features/smart_feature_compressor.py "
            "--input data/enhanced_features_v1.csv --target 35 "
            "--output data/features_compressed_v1.csv",
            "步骤 1/8: 智能特征压缩 (58→35)",
        ),

        # 2. 验证压缩特征
        (
            "python backend/features/verify_compressed_features.py "
            "--original 58 --compressed 35 "
            "--original-csv data/enhanced_features_v1.csv "
            "--compressed-csv data/features_compressed_v1.csv",
            "步骤 2/8: 验证压缩特征质量",
        ),

        # 3. 重新训练精简模型
        (
            "python backend/models/retrain_with_compressed_features.py "
            "--features data/features_compressed_v1.csv "
            "--output saved_models/footballai_compressed.joblib "
            "--version compressed_v1",
            "步骤 3/8: 重训练精简模型",
        ),

        # 4. 智能集成模型
        (
            "python backend/models/smart_integration.py "
            "--footballai saved_models/footballai_compressed.joblib "
            "--expert saved_models/experts/ "
            "--output saved_models/smart_integrated.joblib",
            "步骤 4/8: 智能模型集成",
        ),

        # 5. 验证集成性能
        (
            "python backend/models/test_smart_integration.py "
            "--model saved_models/smart_integrated.joblib "
            "--test data/features_compressed_v1.csv "
            "--baseline saved_models/footballai_v5.0_30000.joblib",
            "步骤 5/8: 验证集成模型性能",
        ),

        # 6. 部署优化系统
        (
            "python backend/scripts/deploy_optimized_system.py "
            "--compressed-model saved_models/footballai_compressed.joblib "
            "--integrated-model saved_models/smart_integrated.joblib "
            "--prod-dir saved_models/production",
            "步骤 6/8: 部署优化系统",
        ),

        # 7. 运行系统测试
        (
            "python scripts/run_optimized_system_tests.py --quick",
            "步骤 7/8: 系统测试验证",
        ),

        # 8. 性能基准测试
        (
            "python scripts/benchmark_optimized_system.py "
            "--model saved_models/smart_integrated.joblib "
            "--requests 1000 --concurrent 10 "
            "--output output/benchmark_report.json",
            "步骤 8/8: 性能基准测试",
        ),
    ]

    failed_steps = []
    total_start = time.time()

    for cmd, desc in steps:
        ret = run_command(cmd, desc, timeout=900)  # 15分钟超时
        if ret != 0:
            failed_steps.append(desc)
            logger.info(f"\n⚠️ 步骤失败，继续执行下一步...")
            # 不中断，继续执行后续步骤

    total_elapsed = time.time() - total_start

    # 最终报告
    logger.info("\n" + "=" * 60)
    logger.info(f"🎉 一键优化完成! (总耗时: {total_elapsed/60:.1f}分钟)")
    logger.info("=" * 60)

    if failed_steps:
        logger.info(f"\n⚠️ {len(failed_steps)}/{len(steps)} 步骤失败:")
        for step in failed_steps:
            logger.info(f"  ❌ {step}")
    else:
        logger.info("\n✅ 全部步骤成功!")
        logger.info("\n优化结果:")
        logger.info("  ✓ 特征压缩: 58 → 35 (缩减40%)")
        logger.info("  ✓ 模型集成: footballAI + 专家精华")
        logger.info("  ✓ 系统部署: 优化目录结构")
        logger.info("  ✓ 测试验证: 全部通过")
        logger.info("\n产物:")
        logger.info("  📁 data/features_compressed_v1.csv")
        logger.info("  📁 saved_models/footballai_compressed.joblib")
        logger.info("  📁 saved_models/smart_integrated.joblib")
        logger.info("  📁 saved_models/production/ (生产部署)")
        logger.info("  📁 output/benchmark_report.json")

    return 0 if not failed_steps else 1


if __name__ == "__main__":
    sys.exit(main())
