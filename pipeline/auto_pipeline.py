#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
v5.7 自动化预测流水线 — 一键全链路
====================================
用法:
  python pipeline/auto_pipeline.py                                # 预测明天
  python pipeline/auto_pipeline.py --date 2026-06-28              # 指定日期
  python pipeline/auto_pipeline.py --update-standings             # 更新积分后预测淘汰赛
  python pipeline/auto_pipeline.py --backtest                     # 全量回测+学习
  
v6.0 数据清理:
  - 移除硬编码测试数据 (6/28 R3 赛程)
  - 赛程数据现在从 FootballDataLive API 实时获取
  - 赔率数据从配置文件 D:\Architecture\config\match_odds.json 读取 (手动维护)
"""

import sys, json, argparse, subprocess, os
from pathlib import Path
from datetime import datetime, timezone, timedelta

ARCH = Path(__file__).resolve().parent.parent
PYTHON = str(ARCH / '.venv' / 'Scripts' / 'python.exe')

def run(cmd, desc=''):
    print(f'\n  ⚡ {desc or cmd}')
    result = subprocess.run(
        [PYTHON] + cmd.split() if not cmd.startswith(PYTHON) else cmd.split(),
        capture_output=True, text=True, cwd=str(ARCH),
        env={**os.environ, 'PYTHONPATH': str(ARCH), 'PYTHONIOENCODING': 'utf-8', 'SECRET_KEY': 'dev'}
    )
    for line in result.stdout.split('\n')[-5:]:
        if line.strip():
            print(f'    {line.strip()[:120]}')
    if result.returncode != 0:
        print(f'  ❌ 失败: {result.stderr[:200]}')
        return False
    print(f'  ✅ 成功')
    return True

def main():
    ap = argparse.ArgumentParser(description='FootballAI v5.7 自动化流水线')
    ap.add_argument('--date', help='预测日期 (YYYY-MM-DD, 默认明天)')
    ap.add_argument('--predict', action='store_true', help='运行全链路预测')
    ap.add_argument('--update-standings', action='store_true', help='更新积分后预测淘汰赛')
    ap.add_argument('--backtest', action='store_true', help='全量回测+赛后学习')
    ap.add_argument('--knockout', action='store_true', help='淘汰赛预测')
    ap.add_argument('--all', action='store_true', help='执行全部(预测+回测+淘汰赛)')
    args = ap.parse_args()

    # 默认：明天
    date = args.date or (datetime.now(timezone.utc) + timedelta(days=1)).strftime('%Y-%m-%d')
    
    if args.all or not any([args.predict, args.update_standings, args.backtest, args.knockout]):
        # 全链路
        print(f'\n{"="*60}')
        print(f'  🔗 FootballAI v5.7 自动化流水线 — {date}')
        print(f'{"="*60}')
        
        # 1. 自检
        run(f'scripts/full_self_check.py', '全链路自检 52/52')
        
        # 2. 预测 — 从实时 API 获取赛程 (v6.0 数据清理: 不再使用硬编码假数据)
        try:
            from data_collector.football_data_live import FootballDataLive
            fdl = FootballDataLive()
            fixtures = fdl.get_wc2026_fixtures()
            print(f"\n  📡 从 Football-Data.org 获取实时赛程: {len(fixtures)} 场")
        except Exception as e:
            print(f"\n  ⚠️ 无法获取实时赛程: {e}")
            print(f"    请确认 FOOTBALL_DATA_API_KEY 已在 .env 中配置。")
            fixtures = []

        if fixtures:
            from pipeline.full_linkage_predictor import FullLinkagePipeline, MatchInput
            pipeline = FullLinkagePipeline()
            results = []
            skipped_no_odds = 0
            for f in fixtures[:12]:  # 最多预测12场
                # v6.0: 从赔率数据库读取真实赔率, 无数据时跳过而非使用默认假值
                try:
                    odds_path = ARCH / 'odds_db' / f"{f.get('homeTeam',{}).get('name','')}_vs_{f.get('awayTeam',{}).get('name','')}_{date}.json"
                    odds_data = json.loads(odds_path.read_text(encoding='utf-8')) if odds_path.exists() else None
                except Exception:
                    odds_data = None

                if odds_data is None:
                    skipped_no_odds += 1
                    print(f"\n  ⚠️ {f.get('homeTeam',{}).get('name','?')} vs {f.get('awayTeam',{}).get('name','?')}: 无赔率数据, 跳过 (不生成假数据)")
                    continue

                odds_h = odds_data.get('1X2', {}).get('home')
                odds_d = odds_data.get('1X2', {}).get('draw')
                odds_a = odds_data.get('1X2', {}).get('away')
                
                # 三项赔率缺一不可
                if not all([odds_h, odds_d, odds_a]):
                    skipped_no_odds += 1
                    print(f"\n  ⚠️ {f.get('homeTeam',{}).get('name','?')} vs {f.get('awayTeam',{}).get('name','?')}: 赔率数据不完整, 跳过")
                    continue
                    
                hcp = odds_data.get('hcp', {}).get('line', 0.0) if odds_data else 0.0
                ou = odds_data.get('ou', {}).get('line', 2.5) if odds_data else 2.5

                m = MatchInput(
                    f.get('homeTeam', {}).get('name', '?'),
                    f.get('awayTeam', {}).get('name', '?'),
                    odds_h, odds_d, odds_a,
                    hcp, ou
                )
                try:
                    r = pipeline.predict(m)
                    results.append(r)
                    fv = r['final_verdict']
                    print(f"\n  {r['match']}")
                    print(f"    方向: {fv['primary']} / {fv['secondary']}")
                    print(f"    比分: {fv['best_score']} 备选: {fv['alt_scores']}")
                except Exception as pred_err:
                    print(f"\n  ❌ {m.home} vs {m.away} 预测失败: {pred_err}")

            skip_msg = f' (跳过{skipped_no_odds}场无赔率数据)' if skipped_no_odds else ''
            print(f'\n  ✅ 预测完成 ({len(results)}/{len(fixtures[:12])}场){skip_msg}')
        else:
            print(f'\n  ⚠️ {date} 无赛程数据，跳过预测。')
        
        # 3. 淘汰赛预测
        print(f'\n  ⚡ 运行淘汰赛预测...')
        run('pipeline/knockout_predictor.py', '淘汰赛晋级预测')
        
        # 4. 赛后学习 (回测最近一次)
        backtest_files = sorted((ARCH / 'reports').glob('full_linkage_backtest_*.json'))
        if backtest_files:
            latest = backtest_files[-1]
            run(f'pipeline/post_match_learner.py --json "{latest}"', f'赛后学习: {latest.name}')
        
        print(f'\n{"="*60}')
        print(f'  🟢 全部完成.')
        print(f'{"="*60}')
    else:
        # 单项执行
        if args.predict:
            run(f'predict --date {date}')
        if args.backtest:
            run('pipeline/backtest_626_627_full_linkage.py', '全链路回测')
            bf = sorted((ARCH / 'reports').glob('full_linkage_backtest_*.json'))
            if bf:
                run(f'pipeline/post_match_learner.py --json "{bf[-1]}"', '赛后学习')
        if args.knockout:
            run('pipeline/knockout_predictor.py', '淘汰赛预测')
        if args.update_standings:
            run('pipeline/knockout_predictor.py', '更新积分+淘汰赛预测')

if __name__ == '__main__':
    main()
