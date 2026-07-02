#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""v4.9 P0+P1 线上验证: 模式C(超热门翻车) + 模式A(2信号协同)"""
import urllib.request, json, sys

URL = "http://localhost:9000/api/v1/chat"

TEST_CASES = [
    {
        "name": "葡萄牙 vs 民主刚果 (模式C: 超热门翻车 od=5.9<6.0)",
        "message": "葡萄牙 vs 民主刚果 1.22 5.90 10.00 让1.75 OU3.0",
        "expect": "模式C触发, 预测平局",
    },
    {
        "name": "瑞士 vs 波黑 (模式A改善: 原误触发, 现需2信号)",
        "message": "瑞士 vs 波黑 1.61 3.75 5.00 让0.5 OU2.5",
        "expect": "模式A需2信号协同, 可能不再误触发",
    },
    {
        "name": "巴西 vs 摩洛哥 (60-70%区间真实平局)",
        "message": "巴西 vs 摩洛哥 1.39 4.50 7.50 让1.5 OU2.5",
        "expect": "信号数不足可能不触发, 走模型概率",
    },
]

def send_chat(message):
    data = json.dumps({"message": message}).encode('utf-8')
    req = urllib.request.Request(URL, data=data, method='POST',
                                  headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read().decode('utf-8')
    except Exception as e:
        return f"ERROR: {e}"

for tc in TEST_CASES:
    print(f"\n{'='*70}")
    print(f"测试: {tc['name']}")
    print(f"期望: {tc['expect']}")
    print(f"{'='*70}")

    raw = send_chat(tc['message'])
    # 解析SSE流, 提取predict_card
    for line in raw.split('\n'):
        if line.startswith('data: ') and 'predict_card' in line:
            try:
                payload = json.loads(line[6:])
                if payload.get('type') == 'predict_card':
                    card = payload.get('data', {})
                    pred = card.get('prediction', '?')
                    d_gate_active = card.get('d_gate_active', False)
                    d_gate_mode = card.get('d_gate_mode', '')
                    hp = card.get('h_prob', 0)
                    dp = card.get('d_prob', 0)
                    ap = card.get('a_prob', 0)
                    risk_tag = card.get('risk_tag', 'N/A')
                    print(f"  预测: {pred}")
                    print(f"  D-Gate: active={d_gate_active}, mode={d_gate_mode or '无'}")
                    print(f"  概率: H={hp:.1%} D={dp:.1%} A={ap:.1%}")
                    print(f"  risk_tag: {risk_tag}")
                    if d_gate_active:
                        print(f"  ✅ D-Gate v4.9 模式{d_gate_mode} 触发")
                    else:
                        print(f"  → D-Gate未触发, 走模型概率判定")
            except json.JSONDecodeError:
                pass
    print()

print("✅ 线上验证完成")
