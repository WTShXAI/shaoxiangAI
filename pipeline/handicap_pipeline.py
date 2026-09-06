"""
让球覆盖预测管线 (v3.0 — 欧盘版)
================================================================
端到端管线: 从欧赔推导让球线 → 构建覆盖率剖面 → 注入特征 → 验证

4 步流程:
  python handicap_pipeline.py --full      # 完整管线
  python handicap_pipeline.py --labels    # 仅 Step 1
  python handicap_pipeline.py --profiles  # 仅 Step 2
  python handicap_pipeline.py --inject    # 仅 Step 3
  python handicap_pipeline.py --verify    # 仅 Step 4
"""

import sqlite3
import logging
import sys
import time

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

class HandicapPipeline:
    def __init__(self, db_path: str = "data/football_data.db"):
        self.db_path = db_path

    def step1_labels(self) -> dict:
        """Step 1: 计算让球结果标签 (比分 + 欧赔 → 让球线 → 赢盘方向)"""
        logger.info("=" * 60)
        logger.info("Step 1/4: 计算让球结果标签")
        logger.info("=" * 60)

        from data_collector.handicap_labeler import HandicapLabeler

        labeler = HandicapLabeler(db_path=self.db_path)
        labeler.initialize()
        labels = labeler.compute_labels()
        stats = labeler.get_stats()
        labeler.close()

        logger.info(f"标签完成: 总计 {stats['total_labels']} 条, "
                     f"分布 {stats['distribution']}")
        return stats

    def step2_profiles(self) -> dict:
        """Step 2: 构建让球深度×OTSM 覆盖率剖面"""
        logger.info("=" * 60)
        logger.info("Step 2/4: 构建让球深度×OTSM 覆盖率剖面")
        logger.info("=" * 60)

        from predictors.handicap_depth_mapper import HandicapDepthMapper

        mapper = HandicapDepthMapper(db_path=self.db_path)
        mapper.initialize()
        profiles = mapper.build_profiles()
        summary = mapper.get_summary()
        mapper.close()

        logger.info(f"剖面完成: {len(profiles)} 个组合")

        # 打印关键统计
        if not summary.empty:
            high_n = summary[summary["n"] >= 30]
            logger.info(f"样本量≥30的剖面: {len(high_n)} 个")
            for _, r in summary.iterrows():
                if r["n"] >= 50:
                    logger.info(
                        f"  [{r['handicap_bin']:>6} × {r['otsm_state']:>8}] "
                        f"n={r['n']:>5} home_cover={r['home_cover%']:.1f}% "
                        f"away_cover={r['away_cover%']:.1f}%"
                    )

        return {"profiles": len(profiles), "summary": summary}

    def step3_inject(self) -> dict:
        """Step 3: 批量注入让球覆盖特征到 match_features 表"""
        logger.info("=" * 60)
        logger.info("Step 3/4: 注入让球覆盖特征到 match_features")
        logger.info("=" * 60)

        from predictors.handicap_cover_predictor import HandicapCoverPredictor

        predictor = HandicapCoverPredictor(db_path=self.db_path)
        predictor.initialize()
        predictor.load_profiles()

        conn = predictor.conn
        cur = conn.cursor()

        # 确保 match_features 有对应列
        for col in ["handicap_cover_prob", "handicap_cover_confidence",
                     "handicap_value_signal", "handicap_value_exists"]:
            try:
                cur.execute(f"ALTER TABLE match_features ADD COLUMN {col} REAL DEFAULT 0.0")
            except sqlite3.OperationalError:
                pass
        conn.commit()

        # 查询所有有 odds 的比赛
        cur.execute("""
            SELECT DISTINCT mf.match_id, o.home_odds, o.draw_odds, o.away_odds
            FROM match_features mf
            JOIN odds o ON mf.match_id = o.match_id
            WHERE o.home_odds IS NOT NULL
              AND o.draw_odds IS NOT NULL
              AND o.away_odds IS NOT NULL
        """)
        rows = cur.fetchall()

        logger.info(f"待注入特征: {len(rows)} 场比赛")

        injected = 0
        skipped = 0
        for r in rows:
            mid = r["match_id"]
            result = predictor.predict(mid, r["home_odds"], r["draw_odds"], r["away_odds"])
            if result is None:
                skipped += 1
                continue

            cur.execute(
                """UPDATE match_features SET
                    handicap_cover_prob = ?,
                    handicap_cover_confidence = ?,
                    handicap_value_signal = ?,
                    handicap_value_exists = ?
                WHERE match_id = ?""",
                (
                    result.cover_probability,
                    result.cover_confidence,
                    1.0 if result.value_exists and result.signal_strength == "strong" else
                    0.5 if result.value_exists else 0.0,
                    1 if result.value_exists else 0,
                    mid,
                ),
            )
            injected += 1

        conn.commit()
        predictor.close()

        logger.info(f"注入完成: {injected} 场, 跳过 {skipped} 场")
        return {"injected": injected, "skipped": skipped}

    def step4_verify(self) -> dict:
        """Step 4: 验证管线各步骤数据完整性"""
        logger.info("=" * 60)
        logger.info("Step 4/4: 验证数据完整性")
        logger.info("=" * 60)

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        checks = {}

        # 1. handicap_labels 表
        cur.execute("SELECT COUNT(*) FROM handicap_labels")
        checks["labels"] = cur.fetchone()[0]
        logger.info(f"  handicap_labels: {checks['labels']} 条")

        # 2. handicap_depth_profile 表
        cur.execute("SELECT COUNT(*) FROM handicap_depth_profile")
        checks["profiles"] = cur.fetchone()[0]
        logger.info(f"  handicap_depth_profile: {checks['profiles']} 个组合")

        # 3. match_features 中的让球特征
        cur.execute("""
            SELECT COUNT(*) FROM match_features
            WHERE handicap_cover_prob IS NOT NULL AND handicap_cover_prob > 0
        """)
        checks["features"] = cur.fetchone()[0]
        logger.info(f"  match_features 含让球特征: {checks['features']} 场")

        # 4. 覆盖分布检查
        cur.execute("""
            SELECT cover_result, COUNT(*) as cnt
            FROM handicap_labels GROUP BY cover_result
        """)
        dist = {r[0]: r[1] for r in cur.fetchall()}
        checks["distribution"] = dist
        total = sum(dist.values()) if dist else 0
        if total > 0:
            for k, v in dist.items():
                logger.info(f"    {k}: {v} ({v/total*100:.1f}%)")

        # 5. 让球分桶覆盖
        cur.execute("""
            SELECT handicap_bin, COUNT(*) as cnt
            FROM handicap_labels GROUP BY handicap_bin
            ORDER BY handicap_bin LIMIT 15
        """)
        bins = cur.fetchall()
        logger.info(f"  让球分桶分布 (前15):")
        for r in bins:
            logger.info(f"    {r[0]:>7}: {r[1]:>6}")

        conn.close()

        all_ok = bool(checks["labels"] and checks["profiles"] and checks["features"])
        logger.info(f"\n验证结果: {'✓ 全部通过' if all_ok else '✗ 有缺失'}")
        return checks

    def run_full(self):
        """运行完整管线"""
        start = time.time()
        logger.info("=" * 60)
        logger.info("让球覆盖预测管线 — 全量运行")
        logger.info(f"启动时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info("=" * 60)
        logger.info("数据来源: 1X2 欧赔 → 理论让球线 (odds_handicap_converter)")

        results = {}

        try:
            results["labels"] = self.step1_labels()
        except (Exception, KeyError, IndexError) as e:
            logger.error(f"Step 1 失败: {e}")
            return results

        try:
            results["profiles"] = self.step2_profiles()
        except (Exception, KeyError, IndexError) as e:
            logger.error(f"Step 2 失败: {e}")
            return results

        try:
            results["inject"] = self.step3_inject()
        except (Exception, KeyError, IndexError) as e:
            logger.error(f"Step 3 失败: {e}")
            return results

        try:
            results["verify"] = self.step4_verify()
        except (Exception, KeyError, IndexError) as e:
            logger.error(f"Step 4 失败: {e}")

        elapsed = time.time() - start
        logger.info(f"\n管线完成! 耗时 {elapsed:.1f}s")
        return results

def main():
    pipeline = HandicapPipeline()

    if "--full" in sys.argv:
        pipeline.run_full()
    elif "--labels" in sys.argv:
        pipeline.step1_labels()
    elif "--profiles" in sys.argv:
        pipeline.step2_profiles()
    elif "--inject" in sys.argv:
        pipeline.step3_inject()
    elif "--verify" in sys.argv:
        pipeline.step4_verify()
    else:
        print("用法:")
        print("  python handicap_pipeline.py --full      完整管线")
        print("  python handicap_pipeline.py --labels    仅 Step 1: 计算标签")
        print("  python handicap_pipeline.py --profiles  仅 Step 2: 构建剖面")
        print("  python handicap_pipeline.py --inject    仅 Step 3: 注入特征")
        print("  python handicap_pipeline.py --verify    仅 Step 4: 验证")

if __name__ == "__main__":
    main()
