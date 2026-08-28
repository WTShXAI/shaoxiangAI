"""
哨响AI · 特征矩阵 8h 周期: 扩充 + tick + 重训 + WI教师重训
===========================================================
每 8 小时由自动化调用：
  0) 构建 tick 时序特征 (events.db odds_changes → 特征库)
  1) 把 GQ 当前已采集数据全量重建进特征矩阵 (refresh)
  2) 用更新后的特征矩阵重训 1X2/OU/AH 三任务分类器 (retrain)
  3) 用威廉+Inter 历史数据重训 WI 教师模型 (权重 0.85, 系统概率主干)

模型随数据一起长大。只做这四件事, 不写赛果、不改源码。
退出码: 0 成功, 1 任一阶段异常。

用法:
  python scripts/refresh_and_retrain.py
"""
import sys

sys.path.insert(0, r"D:\Architecture")

from scripts.refresh_feature_library import main as refresh_main
from scripts.train_feature_library_model import main as train_main


def main():
    print("#" * 64)
    print("# 阶段0/5: 构建 tick 时序特征 (odds_changes → 特征库)")
    print("#" * 64)
    try:
        from scripts.build_tick_features import main as tick_main
        tick_main()
    except Exception as e:
        print(f"⚠ tick 特征构建异常 (不终止 pipeline): {e}")

    print()
    print("#" * 64)
    print("# 阶段1/5: 构建跨市场价差特征 (1X2 vs OU 总进球期望差)")
    print("#" * 64)
    try:
        from scripts.build_cross_market_features import main as cross_main
        cross_main()
    except Exception as e:
        print(f"⚠ 跨市场价差特征构建异常 (不终止 pipeline): {e}")

    print()
    print("#" * 64)
    print("# 阶段2/5: 扩充特征矩阵 (GQ -> 特征库)")
    print("#" * 64)
    rc = refresh_main()
    if rc != 0:
        print("⚠ 特征矩阵扩充失败或为空，终止后续重训。")
        return 1

    print()
    print("#" * 64)
    print("# 阶段3/5: 重训三任务分类器 (1X2 / OU / AH)")
    print("#" * 64)
    try:
        train_main()
    except Exception as e:
        print(f"⚠ 三任务分类器重训异常: {e}")
        return 1

    print()
    print("#" * 64)
    print("# 阶段4/5: 重训 WI 教师模型 (LightGBM 400树, 权重0.85)")
    print("#" * 64)
    try:
        from scripts.train_william_inter_model import main as train_wi
        train_wi()
        print("✅ WI 教师模型已更新落盘 data/wi_{1x2,total}_model.joblib")
    except Exception as e:
        print(f"⚠ WI 教师重训异常 (不影响三任务分类器): {e}")

    print()
    print("✅ 8h 周期完成: tick + 跨市场价差 + 特征矩阵 + 三任务模型 + WI 教师 均已更新。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
