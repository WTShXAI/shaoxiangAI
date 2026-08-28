"""
哨响AI · 特征矩阵定时刷新 (refresh_feature_library)
====================================================
每 8 小时由自动化调用：把 GQ 当前已采集的数据全量重建成特征矩阵。

为什么是全量重建而非增量：
  FeatureLibrary.build_from_gq 内部先 DELETE 再 INSERT，events.db 的 match_outcomes
  是持续累积的（采集器不断追加新比赛），全量重建即可让特征矩阵包含"截至此刻
  采集到的所有数据"，天然幂等、无重复行、无未来泄露（kickoff 排序切分）。

只做一件事：扩充特征矩阵（不重训、不写赛果）。
用法:
  python scripts/refresh_feature_library.py
"""
import sqlite3
import sys

sys.path.insert(0, r"D:\Architecture")

from pipeline.odds_feature_library import FeatureLibrary

GQ_DB = r"D:\Architecture\data\events.db"
FL_DB = r"D:\Architecture\data\shaoxiang_feature_library.db"


def _count(db_path: str) -> int:
    try:
        con = sqlite3.connect(db_path)
        n = con.execute("SELECT COUNT(*) FROM features").fetchone()[0]
        con.close()
        return n
    except Exception:
        return 0


def main():
    before = _count(FL_DB)
    stats = FeatureLibrary(FL_DB).build_from_gq(GQ_DB)
    after = _count(FL_DB)
    delta = after - before
    print("=" * 60)
    print("哨响AI 特征矩阵刷新 (GQ -> 特征库)")
    print("=" * 60)
    print(f"刷新前样本数 : {before}")
    print(f"刷新后样本数 : {after}")
    print(f"本次新增样本 : {delta if delta >= 0 else '全量重建(净变化 %d)' % delta}")
    print(f"结构统计     : {stats}")
    print("=" * 60)
    if after == 0:
        print("⚠ 特征库为空，请确认 events.db 是否在采集。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
