"""
哨响AI - AORE Pipeline 集成 v1.0
=================================
将角色互换逆向推演集成到预测流水线。

流程:
  1. 收集多玩法赔率 (sync_multi_market_odds.py)
  2. 对每场比赛执行 AORE 验证
  3. 将推演结果写入 cross_market_consistency 表
  4. 产出: 逆向推演比分 + 置信度

用法:
  python aore_pipeline.py                     # 对所有 upcoming 比赛
  python aore_pipeline.py --match-id=123      # 单场比赛
  python aore_pipeline.py --league=世界杯      # 指定联赛
"""
import sys, os, argparse, logging, json
from pathlib import Path
from datetime import datetime
from typing import List, Dict

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger('aore_pipeline')


def run_aore_pipeline(match_ids: List[int] = None, league_name: str = None,
                      max_matches: int = 50) -> Dict:
    """
    运行 AORE 逆向推演管线
    
    Args:
        match_ids: 指定比赛ID列表
        league_name: 指定联赛
        max_matches: 最大处理数量
    
    Returns:
        统计摘要
    """
    from bookmaker_sim import AdversarialOddsVerifier
    
    # 如果未指定 match_ids, 从数据库获取 upcoming 比赛
    if not match_ids:
        match_ids = _get_upcoming_matches(league_name, max_matches)
    
    if not match_ids:
        logger.warning("没有找到可推演的比赛")
        return {"error": "No matches found"}
    
    logger.info(f"开始 AORE 逆向推演: {len(match_ids)} 场比赛")
    
    verifier = AdversarialOddsVerifier()
    results = verifier.batch_verify(match_ids, verbose=True)
    
    # 统计信号
    summary = verifier.signal_summary(results)
    
    # 输出高信号比赛
    high_signal = [r for r in results if not r.error and r.signal_strength > 0.5]
    if high_signal:
        logger.info(f"\n=== 高信号比赛 ({len(high_signal)} 场) ===")
        for r in sorted(high_signal, key=lambda x: -x.signal_strength)[:10]:
            logger.info(
                f"  [{r.match_id}] {r.home_team} vs {r.away_team}: "
                f"推演 {r.best_score_h}-{r.best_score_a} "
                f"(信号={r.signal_strength:.2f}, 置信度={r.confidence:.2%})"
            )
    
    # 保存结果到 output/
    output_dir = Path("output/aore")
    output_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_file = output_dir / f"aore_results_{timestamp}.json"
    
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump({
            "summary": summary,
            "high_signal": [{
                "match_id": r.match_id,
                "home_team": r.home_team,
                "away_team": r.away_team,
                "best_score_h": r.best_score_h,
                "best_score_a": r.best_score_a,
                "signal_strength": r.signal_strength,
                "confidence": r.confidence,
            } for r in high_signal],
            "all_results": [{
                "match_id": r.match_id,
                "best_score": f"{r.best_score_h}-{r.best_score_a}",
                "signal_strength": r.signal_strength,
                "confidence": r.confidence,
                "error": r.error,
            } for r in results],
        }, f, indent=2, ensure_ascii=False, default=str)
    
    logger.info(f"结果已保存: {output_file}")
    
    return summary


def _get_upcoming_matches(league_name: str = None, limit: int = 50) -> List[int]:
    """从数据库获取即将开始的比赛ID"""
    import sqlite3
    db_path = os.path.join(os.path.dirname(__file__), 'data', 'football_data.db')
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    
    query = """
        SELECT match_id FROM matches 
        WHERE status = 'scheduled' AND match_date >= date('now')
    """
    params = []
    if league_name:
        query += " AND league_name = ?"
        params.append(league_name)
    
    query += " ORDER BY match_date ASC, match_time ASC LIMIT ?"
    params.append(limit)
    
    cur.execute(query, params)
    match_ids = [row[0] for row in cur.fetchall()]
    conn.close()
    
    return match_ids


def main():
    parser = argparse.ArgumentParser(description="哨响AI AORE 逆向推演管线")
    parser.add_argument("--match-id", type=int, help="指定比赛ID")
    parser.add_argument("--league", type=str, help="指定联赛名")
    parser.add_argument("--max", type=int, default=50, help="最大处理比赛数")
    parser.add_argument("--sigma", type=float, default=0.35, 
                       help="隐藏力度参数 (0.2=强信号, 0.5=弱信号)")
    
    args = parser.parse_args()
    
    match_ids = [args.match_id] if args.match_id else None
    
    result = run_aore_pipeline(
        match_ids=match_ids,
        league_name=args.league,
        max_matches=args.max,
    )
    
    print(f"\n=== 推演完成 ===")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
