#!/usr/bin/env python
"""测试修复效果: 推荐系统双市场 + 输出格式主客分明"""
import sys
from pathlib import Path

from pipeline.full_linkage_predictor import MatchInput, FullLinkagePipeline

# 创建管道实例
pipeline = FullLinkagePipeline()

# 测试场景1: 深让盘 → 应该双推(让球+1X2)
print("=" * 60)
print("测试场景1: 深让盘 (主队让1球)")
print("=" * 60)
m1 = MatchInput('法国', '挪威', 1.25, 5.50, 12.00, -1.0, 2.25, r3_rotation=False)
result1 = pipeline.predict(m1)
v1 = result1['final_verdict']
print(f"推荐类型: {v1['rec_type']}")
print(f"推荐市场: {v1.get('rec_markets', 'ERROR: 缺失')}")
print(f"首选: {v1['primary']}")
print(f"次选: {v1['secondary']}")
print()

# 测试场景2: 大比分 → 应该双推(让球+1X2)
print("=" * 60)
print("测试场景2: 大比分预测 (强队大胜)")
print("=" * 60)
m2 = MatchInput('阿根廷', '约旦', 1.30, 5.00, 10.00, -2.0, 3.0, r3_rotation=False)
result2 = pipeline.predict(m2)
v2 = result2['final_verdict']
print(f"推荐类型: {v2['rec_type']}")
print(f"推荐市场: {v2.get('rec_markets', 'ERROR: 缺失')}")
print(f"首选: {v2['primary']}")
print(f"次选: {v2['secondary']}")
print()

# 测试场景3: 浅让盘+小比分 → 应该1X2优先
print("=" * 60)
print("测试场景3: 浅让盘+小比分")
print("=" * 60)
m3 = MatchInput('日本', '克罗地亚', 2.10, 3.20, 3.50, -0.25, 2.25, r3_rotation=False)
result3 = pipeline.predict(m3)
v3 = result3['final_verdict']
print(f"推荐类型: {v3['rec_type']}")
print(f"推荐市场: {v3.get('rec_markets', 'ERROR: 缺失')}")
print(f"首选: {v3['primary']}")
print(f"次选: {v3['secondary']}")
print()

# 验证标签格式
print("=" * 60)
print("验证标签格式 (应该都是明确的'主胜'/'客胜'/'平局'或'让胜'/'让平'/'让负')")
print("=" * 60)
for name, v in [('场景1', v1), ('场景2', v2), ('场景3', v3)]:
    pri = v['primary']
    sec = v['secondary']
    print(f"{name}: primary='{pri}', secondary='{sec}'")
    # 检查是否有模糊标签
    if pri in ['胜', '负', '平']:
        print(f"  ❌ 错误: 存在模糊标签 '{pri}'")
    else:
        print(f"  ✅ 标签清晰")

print()
print("=" * 60)
print("修复验证完成")
print("=" * 60)
