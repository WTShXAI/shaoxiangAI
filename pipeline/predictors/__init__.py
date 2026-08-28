"""Full Linkage Predictor 拆分包

P0-Ω0 (2026-08-11): 移除所有 import * 惰性加载.
8 条级联 import * 在 bridge 取 MatchInput 时加载 10 个子模块(含 ou_linkage),
实测对线上预测零贡献(全仓活跃代码均使用全路径导入).
现仅保留 docstring, 所有子模块按需由消费者显式导入.
"""
