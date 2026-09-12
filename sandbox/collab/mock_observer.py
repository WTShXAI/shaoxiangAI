#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""模拟哨响分析 AI: 回放真实历史异常为观察条目 (非编造数据, 全部本会话实测案例)."""
import subprocess
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 真实历史异常 (2026-09-08~10 会话实测), (task_id, 优先级, 描述)
ANOMALIES = [
    ('ANOM_Tarragona_deadline6',  'P1', '塔拉戈纳 85\' 线6@74%大: 死线(12分钟无报价)参与判定, CS 卡被污染出 5-2/6-2/7-2 幽灵首选'),
    ('ANOM_Storm_HT_pollution',   'P1', '风暴vs守护者FC: 滚球中 API S1 字段携带当前比分冒充 HT, HT 被写 4-1(真实 1-1)'),
    ('ANOM_Postponed_ghost_0_0',  'P1', '延期/未开赛场被 3.5h 规则打成 finished 0-0, 让球+1 结算器把 0-0 当平局覆盖记假 win(82.5% 虚高)'),
    ('ANOM_Watch_neg1_45_bias',   'P1', '盯盘 11 场实证: CS top1 总球均值比实际低 1.45 球(8/11 负偏差), CS_TOP1 0/11'),
    ('ANOM_BetUnder_line_150',    'P2', 'beat_under 线1.5 亏损簇: 15 注 ROI -33.5%'),
    ('ANOM_BetUnder_line_175',    'P2', 'beat_under 线1.75 亏损簇: 32 注 ROI -32.0%'),
    ('ANOM_BetUnder_line_200',    'P2', 'beat_under 线2.0 亏损簇: 65 注 ROI -18.6%'),
    ('ANOM_BetUnder_line_275',    'P2', 'beat_under 线2.75 亏损簇: 43 注 ROI -13.9%'),
    ('ANOM_BetUnder_line_300',    'P2', 'beat_under 线3.0 亏损簇: 46 注 ROI -14.9%'),
    ('ANOM_BetUnder_odds_lt180',  'P2', 'beat_under 低赔档<1.80: 75 注 ROI -25.6%'),
    ('ANOM_BetUnder_midnight',    'P2', 'beat_under 午夜档(00-06时): 155 注 ROI -16.0%'),
    ('ANOM_Zombie_12h_live',      'P1', '塔拉戈纳场开赛 12 小时仍标 live, mlet 卡死 45:00 占位, 僵尸 OU 报价持续推流(大6@1.03)'),
    ('ANOM_MinuteAt_618_pollute', 'P1', '历史 minute_at 61.8% 污染(45/90 占位 + 垃圾 mmp 穿插), HT 窗口回测全失真'),
    ('ANOM_HT_gt_FT_792',         'P1', '792 场不可行半场(HT>FT, 含篮球 71-79 混入)'),
    ('ANOM_Stale_anchor_750_89',  'P1', '阿尔托赞比西 89\' OU 赛前线 7.5 当滚球锚, 降权真首选 2-2 并合成 4-2/5-2 幽灵比分'),
    ('ANOM_NO_EDGE_high_prob',    'P2', 'probe 输出 NO_EDGE 但 prob≥0.70(标注语义矛盾, 展示层弱优势)'),
    ('ANOM_1X2_freeze_45pct',     'P2', '中场冻结 1X2 方向首批 45.0%(49/60), 领先域先验 71.2% 未优先'),
    ('ANOM_X2_port_regression',   'P1', 'v2 移植实验: HT 回放 TOP1 46.7→17.1% — 已回滚, 需记录为被否决方案'),
    ('ANOM_Score_backtrack_15',   'P2', '近2天 15 场比分轨迹倒退(feed 误推后修正, 轴上幽灵比分)'),
    ('ANOM_Supply_cut_83',        'P2', '近2天 83 场完赛无比分帧(断供), 结算可信度双闸门持续拦截'),
    ('ANOM_Preverify_double',     'P3', '结构性候选: 赛前锚 verdict 与统一波胆方向双源并存, 一致率仅 25%(样本偏差待查)'),
    ('ANOM_OU_deadline_6',        'P1', 'OU_6.00 报价 1.03(市场认为总球≥7)而系统比分 3-2 — 比分滞后或市场僵尸流, 需交叉核实'),
]


def run(journal, limit=None):
    items = ANOMALIES if limit is None else ANOMALIES[:limit]
    for task, pri, text in items:
        subprocess.run([sys.executable, os.path.join(os.path.dirname(__file__), 'collab.py'),
                        '--journal', journal, 'obs', '--task', task,
                        '--pri', pri, '--text', text],
                       capture_output=True, text=True)
    print(f'[mock_observer] 回放 {len(items)} 条真实历史异常')


if __name__ == '__main__':
    journal = sys.argv[sys.argv.index('--journal') + 1] if '--journal' in sys.argv else 'journal.jsonl'
    run(journal)
