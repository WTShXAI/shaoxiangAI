import sys
import json
from pathlib import Path
from datetime import datetime, timezone
# 确保项目根目录在路径中
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from data_collector.sportsapi_wc2026 import fetch_results, fetch_matches
from pipeline.full_linkage_predictor import FullLinkagePipeline, MatchInput
def run_real_backtest():
    print("🚀 启动真实数据回测流程 (v2 - 完整参数)...")
    
    # 1. 获取真实赛果
    print("📥 正在从 SportsAPI 获取最新赛果...")
    try:
        results = fetch_results()
        print(f"✅ 获取到 {len(results)} 场已完成比赛")
    except Exception as e:
        print(f"❌ 获取赛果失败: {e}")
        return
    # 2. 执行回测
    pipeline = FullLinkagePipeline()
    backtest_results = []
    
    print("⚖️ 正在进行模型回测...")
    for game in results[:5]: 
        home = game.get('homeCompetitor', {}).get('name', 'Unknown')
        away = game.get('awayCompetitor', {}).get('name', 'Unknown')
        score_h = game.get('homeCompetitor', {}).get('score', 0)
        score_a = game.get('awayCompetitor', {}).get('score', 0)
        
        # 构造预测输入 (使用标准盘口参数)
        pred_input = MatchInput(home, away, 2.0, 3.2, 3.5, hcp=0.0, ou_line=2.5) 
        try:
            pred = pipeline.predict(pred_input)
            backtest_results.append({
                "match": f"{home} vs {away}",
                "actual_score": f"{score_h}-{score_a}",
                "prediction": pred['final_verdict']['best_score'],
                "direction": pred['final_verdict']['primary']
            })
        except Exception as e:
            print(f"⚠️ 单场预测失败: {e}")
    # 3. 保存报告
    report_path = PROJECT_ROOT / "reports" / f"real_backtest_{datetime.now().strftime('%Y%m%d_%H%M')}.json"
    report_path.parent.mkdir(exist_ok=True)
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(backtest_results, f, ensure_ascii=False, indent=2)
    
    print(f"📄 回测报告已生成: {report_path}")
    return backtest_results
if __name__ == '__main__':
    run_real_backtest()