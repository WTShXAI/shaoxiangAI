# -*- coding: utf-8 -*-
"""
v4.2 风控联动验证 — 苏格兰比赛 bug 复现
测试目标: 确认 risk_tag 联动比分惩罚已生效
"""
import sys
import json
import urllib.request

sys.stdout.reconfigure(encoding='utf-8')

# 构造苏格兰相关测试场景
# 场景1: 海地 vs 苏格兰 (真实回测赔率, 平赔4.5对应隐含20.5%, 不触发ignore_draw)
# 场景2: 构造一个会触发"诱平陷阱"的赔率 (欧平>24% + 亚半球)
TEST_CASES = [
    {
        "name": "海地 vs 苏格兰 (真实赔率, D-Gate模式A)",
        "message": "海地 vs 苏格兰 6.90 4.50 1.40 让1.5 OU2.5",
    },
    {
        "name": "苏格兰 vs 安道尔 (构造诱平陷阱, 验证ignore_draw压倒D-Gate)",
        "message": "苏格兰 vs 安道尔 2.30 3.60 3.10 让0.5 OU2.5",
    },
    {
        "name": "巴西 vs 阿根廷 (强强对话, 验证neutral路径不误伤)",
        "message": "巴西 vs 阿根廷 2.10 3.30 3.60 让0.25 OU2.5",
    },
    {
        "name": "法国 vs 某弱队 (强弱悬殊, 验证weak_draw路径)",
        "message": "法国 vs 安道尔 1.20 6.50 12.0 让2.5 OU3.5",
    },
]

URL = "http://localhost:8000/api/v1/chat"

for tc in TEST_CASES:
    print(f"\n{'='*70}")
    print(f"测试场景: {tc['name']}")
    print(f"输入: {tc['message']}")
    print(f"{'='*70}")

    payload = json.dumps({"message": tc['message']}).encode('utf-8')
    req = urllib.request.Request(
        URL, data=payload, method='POST',
        headers={'Content-Type': 'application/json; charset=utf-8',
                 'Accept': 'text/event-stream'}
    )

    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            for raw in resp:
                line = raw.decode('utf-8', errors='replace').strip()
                if not line.startswith('data:'):
                    continue
                try:
                    evt = json.loads(line[5:].strip())
                except json.JSONDecodeError:
                    continue

                if evt.get('type') == 'predict_card':
                    card = evt.get('data', {})
                    print(f"\n--- 预测卡片 ---")
                    print(f"主/平/客概率: {card.get('h_prob')}/{card.get('d_prob')}/{card.get('a_prob')}")
                    print(f"D-Gate: active={card.get('d_gate_active')}, mode={card.get('d_gate_mode')}")
                    print(f"风险标签 risk_tag: {card.get('risk_tag')}")
                    print(f"平局惩罚率 draw_punish_rate: {card.get('draw_punish_rate')}")
                    print(f"风控理由: {card.get('risk_tag_reason')}")
                    print(f"陷阱分数: {card.get('trap_score')}, 推荐: {card.get('trap_recommendation')}")

                    traps = card.get('trap_warnings', [])
                    print(f"\n陷阱信号 ({len(traps)}个):")
                    for t in traps:
                        print(f"  [{t.get('type')}] dir={t.get('direction')} conf={t.get('confidence')}")
                        print(f"    {t.get('description')}")

                    scores = card.get('scores', [])
                    print(f"\n比分预测 ({len(scores)}个, 风控联动后):")
                    for s in scores:
                        marker = '🚫平局' if s.get('is_draw') else '✅分胜负'
                        tag = f" [{s.get('tag')}]" if s.get('tag') else ''
                        star = '⭐'*s.get('star', 0)
                        print(f"  {marker} {s.get('score')} {s.get('prob')} {star}{tag}")
                    break

                elif evt.get('type') == 'error':
                    print(f"❌ 错误: {evt.get('content')}")
                    break
    except Exception as e:
        print(f"❌ 请求失败: {e}")

print(f"\n{'='*70}")
print("验证完成")
