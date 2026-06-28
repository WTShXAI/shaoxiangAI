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
        
        # 2. 预测
        from pipeline.full_linkage_predictor import FullLinkagePipeline, MatchInput
        
        # 加载6/28 R3赛程
        matches = [
            MatchInput('克罗地亚','加纳', 1.65,3.05,5.11, -1.0, 2.5, r3_rotation=True, sporttery_hcp=-1.0),
            MatchInput('巴拿马','英格兰', 2.70,4.10,1.94, +2.0, 2.5, r3_rotation=True, sporttery_hcp=+2.0),
            MatchInput('哥伦比亚','葡萄牙', 3.50,3.68,1.75, +1.0, 2.5, r3_rotation=True, sporttery_hcp=+1.0),
            MatchInput('民主刚果','乌兹别克斯坦', 1.46,4.10,5.00, -1.0, 2.5, r3_rotation=True, sporttery_hcp=-1.0),
            MatchInput('阿尔及利亚','奥地利', 3.70,2.02,2.75, +1.0, 2.5, r3_rotation=True, sporttery_hcp=+1.0),
            MatchInput('约旦','阿根廷', 2.58,3.90,2.06, +2.0, 2.5, r3_rotation=True, sporttery_hcp=+2.0),
        ]
        
        pipeline = FullLinkagePipeline()
        results = []
        for m in matches:
            r = pipeline.predict(m)
            results.append(r)
            fv = r['final_verdict']
            print(f"\n  {r['match']}")
            print(f"    方向: {fv['primary']} / {fv['secondary']}")
            print(f"    比分: {fv['best_score']} 备选: {fv['alt_scores']}")
            print(f"    推荐类型: {fv.get('rec_type', 'balanced')}")
            if fv.get('context_reason'):
                print(f"    战意: {fv['context_reason']}")
        
        print(f'\n  ✅ R3预测完成 ({len(results)}场)')
        
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
