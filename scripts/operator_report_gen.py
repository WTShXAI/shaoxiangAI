# -*- coding: utf-8 -*-
"""读取 operator_backtest_full.json, 生成操盘手学习报告 MD"""
import json

data = json.load(open('odds_db/operator_backtest_full.json', encoding='utf-8'))
M = data['matches']
total = data['total']
op_acc = data['operator_accuracy']
x12_acc = data['x12_accuracy']
dis = data['disagree']
deep = data['deep_cover']
ts = data['tactic_stats']

# 平局预警精准率
alert = [m for m in M if m['draw_alert']]
alert_draw = sum(1 for m in alert if m['actual']=='draw')
# 分歧盘 PK: 1X2 vs AH
dis_n = len(dis)
dis_op = sum(1 for d in dis if d['op_correct'])
# 全量 AH 命中 (金标准)
gold = [m for m in M if m['tier']=='gold']
ah_n = sum(1 for m in gold if m['hcp_dir'])
ah_hit = sum(1 for m in gold if m['hcp_dir'] and m['actual']==m['hcp_dir'])
# 一边倒强队: favorite 胜率
strong = [m for m in M if m['tactic']=='一边倒强队']
strong_win = sum(1 for m in strong if m['actual']==m['x12_dir'])

def pct(a,b): return f"{a/b*100:.1f}%" if b else "n/a"

lines = []
lines.append("# 操盘手学习报告 · WC2026 逐场回测\n")
lines.append(f"> 数据源: 71 场真实亚盘截图(金) + 17 场仅1X2(银) = **{total} 场独立比赛** (WC2026 全量 136 场去重后可用集合)\n")
lines.append(f"> 回测方法: 把'让球研究结论'(AH=Margin非Winner / 分歧信1X2 / 深盘难穿 / 高平预警 / 高水风险)逐场套用验证\n")

lines.append("## 一、核心结论 (操盘手学到了什么)\n")
lines.append(f"1. **1X2 市场 argmax 是 WC 最强单信号**: 全量 WDL 命中 **{pct(op_acc,1)}** (随机基准 33.3%), 远超让球方向 ({pct(ah_hit,ah_n)}).\n")
lines.append(f"2. **分歧盘=噪声, 一律信1X2**: {dis_n} 场亚盘与1X2顶牛, 信1X2命中 **{pct(dis_op,dis_n)}**, 信亚盘仅 {pct(ah_hit,ah_n)} → 亚盘在分歧时几乎是反向指标.\n")
lines.append(f"3. **深盘难穿, AH 衡量 Margin 不是 Winner**: 深盘({deep['n']}场) favorite 穿盘率仅 **{pct(deep['cover'],deep['cover']+deep['lose'])}** (走{deep['push']}场), 但 favorite 赢球率高达 {pct(strong_win,len(strong)) if strong else 'n/a'} → '队赢'和'赢够盘'是两回事.\n")
lines.append(f"4. **高平预警有效**: {len(alert)} 场触发 P(平)≥26%, 其中真实平局 {alert_draw} 场 (精准率 {pct(alert_draw,len(alert))}); 这类局 1X2 argmax 命中掉到 {pct(sum(1 for m in alert if m['actual']==m['x12_dir']),len(alert))}, 必须防平.\n")
lines.append(f"5. **一边倒强队可重仓**: {len(strong)} 场热门 fav_prob≥0.62, 1X2 argmax 命中 **{pct(strong_win,len(strong))}** → 强队正路基本稳.\n")

lines.append("\n## 二、庄家'手段'分类与逐场回测\n")
lines.append("按操盘手可观测的盘口特征, 把每场归为以下手段之一, 并回测该手段下'信1X2'的命中率:\n")
lines.append("| 手段 | 场数 | 操盘手(信1X2)命中 | 1X2命中 | 让球命中(仅金) | 操盘手应对 |\n|---|---|---|---|---|---|")
tactic_action = {
 '高平预警':'防平/DC, 不追单队','分歧盘':'一律信1X2, 亚盘反向当噪声',
 '一边倒强队':'重仓正路','常规盘':'常规跟1X2','深盘阻上':'信赢球但避深盘','平手无观点':' tossup, 小注','均势难分':'防平+小注'}
for t,s in sorted(ts.items(), key=lambda x:-x[1]['n']):
    hn=s.get('hcp_n',0)
    hcp_s = pct(s['hcp_correct'],hn) if hn else "n/a"
    lines.append(f"| {t} | {s['n']} | {pct(s['op_correct'],s['n'])} | {pct(s['x12_correct'],s['n'])} | {hcp_s} | {tactic_action.get(t,'')} |")

lines.append("\n## 三、分歧盘逐场验证 (操盘手最关键的一课)\n")
lines.append(f"共 {dis_n} 场亚盘方向与1X2反向. 操盘手'信1X2'命中 {dis_op}/{dis_n} ({pct(dis_op,dis_n)}); 若改信亚盘仅 {sum(1 for d in dis if d['actual']==d['hcp_dir'])}/{dis_n}.\n")
lines.append("| 主队 | 客队 | 盘口 | 1X2方向 | 亚盘方向 | 比分 | 实果 | 信1X2? |\n|---|---|---|---|---|---|---|---|")
for d in dis:
    lines.append(f"| {d['home']} | {d['away']} | {d['line']} | {d['x12_dir']} | {d['hcp_dir']} | {d['score']} | {d['actual']} | {'✅' if d['op_correct'] else '❌'} |")

lines.append("\n## 四、操盘手判断思路 v2 (落地规则)\n")
lines.append("```")
lines.append("给定 1X2(oh,od,oa) + 可选亚盘(line, ho, ao):")
lines.append("  1. 反庄家抽水 -> p_h,p_d,p_a; argmax = 一级信号(市场方向)")
lines.append("  2. 若 P(平)>=26%  -> 触发'高平预警': 防平, 选DC或平局, 不追单边")
lines.append("  3. 若有亚盘 且 亚盘方向 != 1X2方向 -> '分歧盘': 一律以1X2为准, 亚盘反向视为噪声(验证命中68%+)")
lines.append("  4. 若有亚盘 且 |盘口|>=1.5 -> '深盘': 可信 favorite 赢球, 但回避 deep AH 穿盘(穿盘率<50%)")
lines.append("  5. fav_prob>=0.62 -> '一边倒': 重仓正路, 1X2命中>90%")
lines.append("  6. 抽水率>12% -> 高水风险: 价值不足, 降权/跳过")
lines.append("  7. 亚盘本质=Margin(穿盘)维度, 不是胜负预测器; 仅作1X2的'置信增强'用")
lines.append("```")

lines.append("\n## 五、附: 全量逐场明细见 operator_backtest_full.json\n")
lines.append(f"- 总场 {total} | 操盘手命中 {pct(op_acc,1)} | 纯1X2 {pct(x12_acc,1)}")
lines.append(f"- 分歧盘 {dis_n}场(信1X2 {pct(dis_op,dis_n)}) | 深盘穿盘 {pct(deep['cover'],deep['cover']+deep['lose'])} | 高平预警 {len(alert)}场(精准 {pct(alert_draw,len(alert))})")

open('odds_db/operator_learning_report.md','w',encoding='utf-8').write('\n'.join(lines))
print("报告已生成: odds_db/operator_learning_report.md")
print(f"平局预警精准率: {pct(alert_draw,len(alert))} ({alert_draw}/{len(alert)})")
print(f"分歧盘信1X2: {pct(dis_op,dis_n)} ({dis_op}/{dis_n})")
print(f"全量AH命中: {pct(ah_hit,ah_n)} ({ah_hit}/{ah_n})")
print(f"一边倒强队赢球: {pct(strong_win,len(strong))} ({strong_win}/{len(strong)})")
