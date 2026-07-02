"""
v5.24 兼容层 — DrawExpert 模块桥接

DrawExpert 原始定义位于 predictors/components/draw_expert.py。
旧版模型文件 (draw_expert_v1.joblib) 在 pickle 序列化时引用了
draw_expert.DrawExpert 路径。joblib.load() 反序列化需要此模块可导入。

此文件提供向后兼容的顶层 re-export。
"""
from predictors.components.draw_expert import DrawExpert  # noqa: F401
