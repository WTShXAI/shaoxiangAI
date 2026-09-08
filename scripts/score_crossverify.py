# -*- coding: utf-8 -*-
"""比分源交叉核验 · 探查结论存档 (2026-09-09)

结论: 本脚本探查的 football_data.db::william_ht 是 2012-2018 年 William Hill
历史数据集(458,027 行, match_date 2012-08-25 ~ 2018-01-17) — 与哨响当前赛事
(2026 年, 乐鱼源) 零时间交集, 不能用作当前比赛的赛果核验第二源。

可行路径 (待用户批准, 属网络采集工程):
  1. aiqiuke 报告验证过的外部源: scoutingstats.ai / 24score.com / besoccer / 赛酷
     (俄系青训/丹麦联赛赛果稳定可查, 含 HT; "中文音译队名 WebSearch 命中率≥2源")
  2. 需先建 中文队名→拉丁转写 映射(teamNames.ts 前端已有部分)
  3. 接入后: score_crossverify 匹配逻辑可复用本脚本的 (home,away,date±1) 框架

在第二源接入前, 比分可信度依赖 beat_under_track 的双闸门 (轨迹末值一致+帧覆盖≥80')。
83% 断供场的赛果保持未知 — 诚实不结算优于污染标签。
"""
print(__doc__)
print('状态: 探查完成, 无可用第二源 (william_ht 时间窗 2012-2018)。')
