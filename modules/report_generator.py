"""
哨响AI - HTML 评估报告生成器 v3.0
=================================
功能:
- 特征重要性可视化 (Bar Chart)
- 核心性能指标 (AUC, Accuracy, Brier Score, Log Loss, MCC)
- 混淆矩阵热力图
- 各类别精确率/召回率/F1详细表
- 预测分布 vs 实际分布对比
- 按联赛准确率排行
- 自包含 HTML (内嵌 CSS/JS, 无需外部依赖)
"""
import os, json, yaml
from datetime import datetime, timezone
from typing import Dict, Any
import numpy as np
import pandas as pd

class ReportGenerator:
    """
    HTML 评估报告生成器
    """

    def __init__(self, config_path: str = None):
        if config_path is None:
            config_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), 'config.yaml'
            )
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = yaml.safe_load(f)

        root = self.config['paths']['project_root']
        if not os.path.isabs(root):
            root = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
        self._report_dir = os.path.join(root, self.config['paths']['report_dir'])
        os.makedirs(self._report_dir, exist_ok=True)

    def generate(
        self,
        train_result: Dict[str, Any],
        feature_importance: Dict[str, float] = None,
        output_path: str = None,
    ) -> str:
        """
        生成完整 HTML 评估报告

        Args:
            train_result: 训练结果字典 (来自 EnsembleTrainer.train())
            feature_importance: 特征重要性 {feature_name: importance_pct}
            output_path: 自定义输出路径

        Returns:
            HTML 文件路径
        """
        eval_data = train_result.get('evaluation', {})
        feature_names = train_result.get('feature_names', [])

        if not output_path:
            timestamp = datetime.now(timezone.utc).strftime(
                self.config['output']['timestamp_format']
            )
            prefix = self.config['output']['report_prefix']
            output_path = os.path.join(
                self._report_dir, f"{prefix}_{timestamp}.html"
            )

        html = self._build_html(eval_data, feature_names, feature_importance,
                                train_result)

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html)

        return output_path

    def _build_html(self, eval_data: Dict, feature_names: list,
                    feature_importance: Dict = None,
                    train_result: Dict = None) -> str:
        """构建完整 HTML"""

        # 特征重要性排序
        if feature_importance is None:
            feature_importance = {}

        # 计算 AUC bar 颜色
        auc_val = eval_data.get('auc_macro', 0)
        acc_val = eval_data.get('accuracy', 0)
        brier_val = eval_data.get('brier_score', 0)
        ll_val = eval_data.get('log_loss', 0)
        mcc_val = eval_data.get('mcc', 0)

        per_class = eval_data.get('per_class', {})
        cm = eval_data.get('confusion_matrix', [[0,0,0],[0,0,0],[0,0,0]])
        league_metrics = eval_data.get('by_league', {})

        # 构建特征重要性 HTML
        feat_imp_html = self._build_feature_importance_html(feature_importance)

        # 构建混淆矩阵 HTML
        cm_html = self._build_confusion_matrix_html(cm)

        # 构建各类别指标表
        per_class_html = self._build_per_class_html(per_class)

        # 构建联赛排行
        league_html = self._build_league_html(league_metrics)

        # 分布对比
        pred_dist = eval_data.get('pred_distribution', {})
        actual_dist = eval_data.get('actual_distribution', {})
        distribution_html = self._build_distribution_html(pred_dist, actual_dist)

        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

        html = f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>哨响AI - 足球预测模型评估报告</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Microsoft YaHei', sans-serif
    background: linear-gradient(135deg, #0f0c29, #302b63, #24243e)
    color: #e0e0e0
    min-height: 100vh
    padding: 0
  }}
  .container {{ max-width: 1200px; margin: 0 auto; padding: 30px 20px; }}
  
  /* Header */
  .header {{
    text-align: center
    padding: 40px 20px
    margin-bottom: 30px
    background: rgba(255,255,255,0.03)
    border-radius: 16px
    border: 1px solid rgba(255,255,255,0.06)
  }}
  .header h1 {{
    font-size: 2.2em
    background: linear-gradient(135deg, #667eea, #764ba2)
    -webkit-background-clip: text
    -webkit-text-fill-color: transparent
    margin-bottom: 10px
  }}
  .header .subtitle {{ color: #888; font-size: 0.95em; }}

  /* Key Metrics Grid */
  .metrics-grid {{
    display: grid
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr))
    gap: 16px
    margin-bottom: 30px
  }}
  .metric-card {{
    background: rgba(255,255,255,0.04)
    border: 1px solid rgba(255,255,255,0.08)
    border-radius: 12px
    padding: 24px 20px
    text-align: center
    transition: transform 0.2s, box-shadow 0.2s
  }}
  .metric-card:hover {{
    transform: translateY(-2px)
    box-shadow: 0 8px 32px rgba(102, 126, 234, 0.15)
  }}
  .metric-card .label {{
    font-size: 0.82em
    color: #888
    margin-bottom: 8px
    text-transform: uppercase
    letter-spacing: 1px
  }}
  .metric-card .value {{
    font-size: 2em
    font-weight: 700
  }}
  .metric-card .sub {{
    font-size: 0.78em
    color: #666
    margin-top: 4px
  }}
  .good {{ color: #4ade80; }}
  .warn {{ color: #fbbf24; }}
  .bad {{ color: #f87171; }}
  .info {{ color: #60a5fa; }}

  /* Section */
  .section {{
    background: rgba(255,255,255,0.03)
    border: 1px solid rgba(255,255,255,0.06)
    border-radius: 12px
    padding: 28px
    margin-bottom: 24px
  }}
  .section h2 {{
    font-size: 1.2em
    color: #667eea
    margin-bottom: 20px
    padding-bottom: 12px
    border-bottom: 1px solid rgba(255,255,255,0.08)
  }}

  /* Feature Importance */
  .feat-bar-container {{ margin-bottom: 8px; }}
  .feat-bar-label {{
    display: flex
    justify-content: space-between
    margin-bottom: 3px
    font-size: 0.88em
  }}
  .feat-bar-track {{
    height: 22px
    background: rgba(255,255,255,0.05)
    border-radius: 4px
    overflow: hidden
    position: relative
  }}
  .feat-bar-fill {{
    height: 100%
    border-radius: 4px
    transition: width 0.6s ease
    background: linear-gradient(90deg, #667eea, #764ba2)
    display: flex
    align-items: center
    padding-left: 8px
    font-size: 0.75em
    color: white
    min-width: 40px
  }}
  .feat-bar-fill.high {{ background: linear-gradient(90deg, #4ade80, #22d3ee); }}
  .feat-bar-fill.mid {{ background: linear-gradient(90deg, #667eea, #764ba2); }}
  .feat-bar-fill.low {{ background: linear-gradient(90deg, #f87171, #fbbf24); }}

  /* Tables */
  table {{
    width: 100%
    border-collapse: collapse
    font-size: 0.9em
  }}
  th, td {{
    padding: 10px 14px
    text-align: left
    border-bottom: 1px solid rgba(255,255,255,0.06)
  }}
  th {{
    color: #667eea
    font-weight: 600
    font-size: 0.82em
    letter-spacing: 0.5px
  }}
  tr:hover {{ background: rgba(255,255,255,0.02); }}
  .text-right {{ text-align: right; }}

  /* Confusion Matrix */
  .cm-grid {{
    display: inline-grid
    grid-template-columns: repeat(4, 1fr)
    gap: 3px
  }}
  .cm-cell {{
    width: 80px
    height: 60px
    display: flex
    align-items: center
    justify-content: center
    font-size: 1.4em
    font-weight: 700
    border-radius: 6px
  }}
  .cm-header {{ background: rgba(102,126,234,0.15); color: #667eea; font-size: 0.85em; font-weight: 600; }}
  .cm-label {{ background: rgba(102,126,234,0.08); color: #667eea; font-size: 0.85em; font-weight: 600; }}
  .cm-diag {{ background: rgba(74,222,128,0.2); color: #4ade80; }}
  .cm-off {{ background: rgba(248,113,113,0.08); color: #f87171; }}

  /* Distribution Bars */
  .dist-bar-container {{
    display: flex
    gap: 30px
    flex-wrap: wrap
  }}
  .dist-bar-group {{ flex: 1; min-width: 280px; }}
  .dist-row {{
    display: flex
    align-items: center
    margin-bottom: 10px
  }}
  .dist-label {{ width: 48px; font-size: 0.85em; }}
  .dist-track {{
    flex: 1
    height: 24px
    background: rgba(255,255,255,0.05)
    border-radius: 4px
    overflow: hidden
  }}
  .dist-fill {{
    height: 100%
    border-radius: 4px
    display: flex
    align-items: center
    padding-left: 8px
    font-size: 0.75em
    color: white
    transition: width 0.6s ease
  }}
  .dist-fill.home {{ background: linear-gradient(90deg, #4ade80, #22c55e); }}
  .dist-fill.draw {{ background: linear-gradient(90deg, #fbbf24, #f59e0b); }}
  .dist-fill.away {{ background: linear-gradient(90deg, #60a5fa, #3b82f6); }}

  /* Badge */
  .badge {{
    display: inline-block
    padding: 2px 8px
    border-radius: 10px
    font-size: 0.75em
    font-weight: 600
  }}
  .badge-green {{ background: rgba(74,222,128,0.15); color: #4ade80; }}
  .badge-yellow {{ background: rgba(251,191,36,0.15); color: #fbbf24; }}
  .badge-red {{ background: rgba(248,113,113,0.15); color: #f87171; }}

  /* Footer */
  .footer {{
    text-align: center
    padding: 30px
    color: #555
    font-size: 0.82em
  }}

  /* Responsive */
  @media (max-width: 768px) {{
    .metrics-grid {{ grid-template-columns: repeat(2, 1fr); }}
    .dist-bar-container {{ flex-direction: column; }}
  }}
</style>
</head>
<body>
<div class="container">

<!-- Header -->
<div class="header">
  <h1>⚽ 哨响AI 模型评估报告</h1>
  <div class="subtitle">
    足球预测集成模型 | 训练时间: {train_result.get('pipeline_path', 'N/A')}<br>
    报告生成: {timestamp} | 测试样本: {eval_data.get('test_samples', 0)}
  </div>
</div>

<!-- Key Metrics -->
<div class="metrics-grid">
  <div class="metric-card">
    <div class="label">准确率 (Accuracy)</div>
    <div class="value {'good' if acc_val >= 50 else ('warn' if acc_val >= 42 else 'bad')}">{acc_val:.1f}%</div>
    <div class="sub">整体分类正确率</div>
  </div>
  <div class="metric-card">
    <div class="label">AUC (Macro)</div>
    <div class="value info">{auc_val:.4f}</div>
    <div class="sub">OvR 宏观平均</div>
  </div>
  <div class="metric-card">
    <div class="label">Brier Score</div>
    <div class="value {'good' if brier_val < 0.18 else ('warn' if brier_val < 0.22 else 'bad')}">{brier_val:.4f}</div>
    <div class="sub">概率校准质量 (越低越好)</div>
  </div>
  <div class="metric-card">
    <div class="label">Log Loss</div>
    <div class="value {'good' if ll_val < 0.95 else ('warn' if ll_val < 1.1 else 'bad')}">{ll_val:.4f}</div>
    <div class="sub">对数损失</div>
  </div>
  <div class="metric-card">
    <div class="label">MCC</div>
    <div class="value {'good' if mcc_val > 0.3 else ('warn' if mcc_val > 0.15 else 'bad')}">{mcc_val:.4f}</div>
    <div class="sub">马修斯相关系数</div>
  </div>
  <div class="metric-card">
    <div class="label">平局召回率</div>
    <div class="value {'good' if per_class.get('draw',{}).get('recall',0)*100 >= 35 else ('warn' if per_class.get('draw',{}).get('recall',0)*100 >= 25 else 'bad')}">{per_class.get('draw',{}).get('recall',0)*100:.1f}%</div>
    <div class="sub">平局正确预测比例</div>
  </div>
</div>

<!-- Feature Importance -->
<div class="section">
  <h2>📊 特征重要性</h2>
  {feat_imp_html if feat_imp_html else '<p style="color:#888;">暂无特征重要性数据（需 XGBoost 模型提供）</p>'}
</div>

<!-- Class Metrics -->
<div class="section">
  <h2>📈 各类别详细指标</h2>
  {per_class_html}
</div>

<!-- Confusion Matrix -->
<div class="section">
  <h2>🎯 混淆矩阵</h2>
  {cm_html}
</div>

<!-- Distribution Comparison -->
<div class="section">
  <h2>📉 预测分布 vs 实际分布</h2>
  {distribution_html}
</div>

<!-- League Performance -->
<div class="section">
  <h2>🏆 联赛性能排行</h2>
  {league_html if league_html else '<p style="color:#888;">暂无联赛细分评估数据</p>'}
</div>

<!-- Model Info -->
<div class="section">
  <h2>ℹ️ 模型信息</h2>
  <table>
    <tr><td style="width:160px;color:#888;">特征数量</td><td>{train_result.get('n_features', 'N/A')}</td></tr>
    <tr><td style="color:#888;">训练样本</td><td>{train_result.get('n_samples', 'N/A')}</td></tr>
    <tr><td style="color:#888;">特征列表</td><td style="font-size:0.82em;color:#aaa;">{', '.join(feature_names) if feature_names else 'N/A'}</td></tr>
    <tr><td style="color:#888;">移除特征</td><td style="font-size:0.82em;color:#aaa;">{', '.join(train_result.get('removed_features', [])) or '(无)'}</td></tr>
    <tr><td style="color:#888;">模型权重</td><td>XGBoost {self.config['models']['ensemble']['xgboost_weight']:.0%} | Ridge {self.config['models']['ensemble']['ridge_weight']:.0%} | 启发式 {self.config['models']['ensemble']['heuristic_weight']:.0%}</td></tr>
    <tr><td style="color:#888;">平局阈值</td><td>{eval_data.get('draw_threshold', 'N/A')}</td></tr>
    <tr><td style="color:#888;">阈值调整数</td><td>{eval_data.get('n_adjusted', 'N/A')}</td></tr>
  </table>
</div>

<div class="footer">
  哨响AI Football Prediction Ensemble Model v3.0 | Generated {timestamp}
</div>

</div>
</body>
</html>'''
        return html

    def _build_feature_importance_html(self, feat_imp: Dict) -> str:
        if not feat_imp:
            return ''

        sorted_imp = dict(sorted(feat_imp.items(), key=lambda x: -x[1]))
        max_imp = max(sorted_imp.values()) if sorted_imp else 1

        rows = []
        for name, imp in sorted_imp.items():
            pct = (imp / max_imp) * 100
            css_class = 'high' if imp >= 10 else ('mid' if imp >= 3 else 'low')
            bar_html = (
                f'<div class="feat-bar-container">'
                f'<div class="feat-bar-label">'
                f'<span>{name}</span>'
                f'<span style="color:#aaa;">{imp:.1f}%</span>'
                f'</div>'
                f'<div class="feat-bar-track">'
                f'<div class="feat-bar-fill {css_class}" style="width:{pct}%">{imp:.1f}%</div>'
                f'</div>'
                f'</div>'
            )
            rows.append(bar_html)

        return '\n'.join(rows)

    def _build_confusion_matrix_html(self, cm: list) -> str:
        labels = ['主胜', '平局', '客胜']
        total = sum(sum(row) for row in cm) if cm else 1

        cells = []
        # Header row
        cells.append('<div class="cm-cell cm-header"></div>')
        for lbl in labels:
            cells.append(f'<div class="cm-cell cm-header">预测<br>{lbl}</div>')

        for i, lbl in enumerate(labels):
            cells.append(f'<div class="cm-cell cm-label">实际<br>{lbl}</div>')
            for j in range(3):
                val = cm[i][j] if i < len(cm) and j < len(cm[i]) else 0
                pct = val / total * 100
                cls = 'cm-diag' if i == j else 'cm-off'
                cells.append(
                    f'<div class="cm-cell {cls}">'
                    f'<span>{val}<br><small>{pct:.1f}%</small></span>'
                    f'</div>'
                )

        return f'<div class="cm-grid">{"".join(cells)}</div>'

    def _build_per_class_html(self, per_class: Dict) -> str:
        if not per_class:
            return '<p style="color:#888;">无数据</p>'

        rows = []
        for cls_name in ['home', 'draw', 'away']:
            if cls_name not in per_class:
                continue
            m = per_class[cls_name]
            recall = m['recall'] * 100
            precision = m['precision'] * 100
            f1 = m['f1'] * 100

            recall_cls = 'badge-green' if recall >= 55 else ('badge-yellow' if recall >= 35 else 'badge-red')
            precision_cls = 'badge-green' if precision >= 55 else ('badge-yellow' if precision >= 35 else 'badge-red')
            f1_cls = 'badge-green' if f1 >= 50 else ('badge-yellow' if f1 >= 30 else 'badge-red')

            rows.append(f'''
            <tr>
              <td><strong>{cls_name}</strong></td>
              <td>{m['support']}</td>
              <td><span class="badge {recall_cls}">{recall:.1f}%</span></td>
              <td><span class="badge {precision_cls}">{precision:.1f}%</span></td>
              <td><span class="badge {f1_cls}">{f1:.1f}%</span></td>
            </tr>''')

        return f'''<table>
          <tr><th>类别</th><th>样本数</th><th>召回率</th><th>精确率</th><th>F1-Score</th></tr>
          {"".join(rows)}
        </table>'''

    def _build_league_html(self, league_metrics: Dict) -> str:
        if not league_metrics:
            return ''

        sorted_leagues = sorted(
            league_metrics.items(), key=lambda x: -x[1]['accuracy']
        )

        rows = []
        for league, metrics in sorted_leagues[:20]:
            acc = metrics['accuracy']
            badge_cls = 'badge-green' if acc >= 50 else ('badge-yellow' if acc >= 42 else 'badge-red')
            rows.append(f'''
            <tr>
              <td>{league}</td>
              <td class="text-right">{metrics['count']}</td>
              <td class="text-right"><span class="badge {badge_cls}">{acc:.1f}%</span></td>
            </tr>''')

        return f'''<table>
          <tr><th>联赛</th><th class="text-right">测试样本</th><th class="text-right">准确率</th></tr>
          {"".join(rows)}
        </table>'''

    def _build_distribution_html(self, pred_dist: Dict, actual_dist: Dict) -> str:
        max_total = max(
            max(pred_dist.values()) if pred_dist else 0,
            max(actual_dist.values()) if actual_dist else 0,
            1
        )

        def _build_group(title, dist):
            rows_html = []
            for key, label, css in [('home', '主胜', 'home'), ('draw', '平局', 'draw'), ('away', '客胜', 'away')]:
                val = dist.get(key, 0)
                pct = (val / max_total * 100) if max_total > 0 else 0
                rows_html.append(f'''
                <div class="dist-row">
                  <div class="dist-label">{label}</div>
                  <div class="dist-track">
                    <div class="dist-fill {css}" style="width:{pct}%">{val}%</div>
                  </div>
                </div>''')
            return f'''
            <div class="dist-bar-group">
              <h4 style="color:#888;margin-bottom:12px;font-size:0.9em;">{title}</h4>
              {"".join(rows_html)}
            </div>'''

        return f'''<div class="dist-bar-container">
          {_build_group('模型预测分布', pred_dist)}
          {_build_group('真实分布', actual_dist)}
        </div>'''

# ══════════════════════════════════════════════════
# 便捷函数
# ══════════════════════════════════════════════════

def generate_report(train_result: Dict[str, Any],
                    feature_importance: Dict[str, float] = None,
                    config_path: str = None) -> str:
    """生成评估报告的一站式方法"""
    gen = ReportGenerator(config_path)
    return gen.generate(train_result, feature_importance)
