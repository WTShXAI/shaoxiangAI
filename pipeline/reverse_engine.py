"""博弈逆向引擎: 赔率解码 + 积分路径 + 赛会赛制 三层融合
============================================================
不在"预测比分",而在"还原动机":
  赔率异常 ∩ 积分动机 ∩ 赛制路径 → "球队在挑谁/在避谁"
============================================================
"""
import json, math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from pathlib import Path

ROOT = Path(__file__).parent.parent

# ═══════════════════════════════════════════════
# 一、赔率解码器 (Vig Strip + FairProb + Anomaly)
# ═══════════════════════════════════════════════

def strip_vig(odds: List[float]) -> List[float]:
    """剥离博彩公司抽水, 得到fair概率
    公式: implied = 1/odds, 归一化 sum(implied) -> fair_prob
    """
    implied = [1.0 / o for o in odds]
    total = sum(implied)
    fair = [p / total for p in implied]
    return [round(f, 4) for f in fair]

def detect_odds_anomaly(fair_probs: List[float], model_probs: List[float], 
                         threshold: float = 0.03) -> Dict:
    """检测赔率异常: 模型概率 vs 市场fair概率
    Returns: {direction: 'overvalued'/'undervalued'/'normal', gap: float, team_index: int}
    """
    gaps = [abs(f - m) for f, m in zip(fair_probs, model_probs)]
    max_gap = max(gaps)
    idx = gaps.index(max_gap)
    
    if max_gap < threshold:
        return {'status': 'normal', 'max_gap': round(max_gap, 4), 'team_index': idx}
    
    # 哪个方向被高估/低估
    if fair_probs[idx] > model_probs[idx]:
        direction = 'overvalued_by_market'
        note = f"市场高估方向{idx}(赔率偏热), 实际概率可能更低"
    else:
        direction = 'undervalued_by_market'
        note = f"市场低估方向{idx}(赔率偏冷), 可能存在价值"
    
    return {
        'status': direction,
        'max_gap': round(max_gap, 4),
        'team_index': idx,
        'fair_vs_model': {i: {'fair': round(f, 4), 'model': round(m, 4), 'gap': round(abs(f-m), 4)} 
                          for i, (f, m) in enumerate(zip(fair_probs, model_probs))},
        'note': note
    }

class OddsDecoder:
    """赔率解码师: vig剥离 + 偏差检测"""
    
    @staticmethod
    def analyze(home: str, away: str, oh: float, od: float, oa: float) -> Dict:
        fair = strip_vig([oh, od, oa])
        ti = 1/oh + 1/od + 1/oa
        raw_imp = [round((1/oh)/ti, 4), round((1/od)/ti, 4), round((1/oa)/ti, 4)]
        
        # 用简单模型概率: 赔率内含的返奖率偏差
        model_est = [
            max(0.25, min(0.70, raw_imp[0] * 0.92)),
            max(0.18, min(0.40, raw_imp[1] * 1.08)),
            max(0.15, min(0.55, raw_imp[2] * 0.92))
        ]
        model_est = [round(p / sum(model_est), 4) for p in model_est]
        
        anomaly = detect_odds_anomaly(fair, model_est)
        
        return {
            'odds_raw': [oh, od, oa],
            'implied': raw_imp,
            'fair_prob': fair,
            'model_est': model_est,
            'anomaly': anomaly,
            'vig': round(sum(1/o for o in [oh, od, oa]) - 1, 4)
        }


# ═══════════════════════════════════════════════
# 二、积分路径逆向师 (Monte Carlo + Path)
# ═══════════════════════════════════════════════

class PointsPathReconstructor:
    """积分路径逆向师: 小组出线蒙特卡洛 + 挑对手推演"""
    
    @staticmethod
    def group_standings_snapshot(group_id: str) -> Dict:
        """获取小组当前积分"""
        p = ROOT / 'data' / 'final_group_standings_v2.json'
        if p.exists():
            data = json.load(open(p, 'r', encoding='utf-8'))
            standings = data.get('standings', {})
            if group_id in standings:
                return {team: stats for team, pts, gd, gf, ga, pos in standings[group_id]}
        return {}

    @staticmethod
    def infer_motivation(team: str, group_id: str, position: int) -> Dict:
        """反推球队动机: 冲头名/保第二/苟第三"""
        # 2026淘汰赛对阵树: 
        # C1→下半区(碰F2→I1/J1枝), C2→上半区(碰D1→A1/B1枝)
        # D1→上半区(碰C2→E1/F1枝), D2→下半区
        # ...完整树见赛会赛制师模块
        
        if position == 1:
            return {
                'motivation': 'maintain',
                'target': '头名晋级',
                'risk': '撞下半区强队(I1/J1枝)',
                'note': '有微弱动机控分→掉到第二走软半区'
            }
        elif position == 2:
            return {
                'motivation': 'push',
                'target': '争第二或保最佳第三',
                'risk': '被第三反超',
                'note': '胜=保第二, 平=可能掉第三被挤'
            }
        elif position == 3:
            return {
                'motivation': 'survive',
                'target': '挤进TOP8第三名',
                'risk': '被挤出前8',
                'note': '必须赢+刷净胜球, 同时看其他组脸色'
            }
        return {'motivation': 'eliminated', 'target': '荣誉之战'}


# ═══════════════════════════════════════════════
# 三、赛会赛制师 (2026 Bracket Tree)
# ═══════════════════════════════════════════════

BRACKET_2026 = """
╔══════════════════════════════════════════════════════════════╗
║            2026美加墨世界杯 R32 淘汰赛对阵树                    ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  上半区 (Top Half)                                            ║
║  ┌──────────────────────────────────────────────────────┐    ║
║  │ R32: A2(南非) vs B2(加拿大) → 胜者v ?                │    ║
║  │ R32: C1(巴西) vs F2(日本)   → 胜者v ?                │    ║
║  │ R32: E1(德国) vs D3(巴拉圭) → 胜者v ?                │    ║
║  │ R32: F1(荷兰) vs C2(摩洛哥) → 胜者v ?                │    ║
║  │ R32: E2(科特迪瓦) vs I2(挪威)→ 胜者v ?               │    ║
║  │ R32: I1(法国) vs F3(瑞典)   → 胜者v ?                │    ║
║  │ R32: A1(墨西哥) vs E3(厄瓜多尔)→ 胜者v ?             │    ║
║  │ R32: L1(英格兰) vs K3(民主刚果)→ 胜者v ?             │    ║
║  └──────────────────────────────────────────────────────┘    ║
║                                                              ║
║  下半区 (Bottom Half)                                         ║
║  ┌──────────────────────────────────────────────────────┐    ║
║  │ R32: G1(比利时) vs I3(塞内加尔)→ 胜者v ?             │    ║
║  │ R32: D1(美国) vs B3(波黑)    → 胜者v ?               │    ║
║  │ R32: H1(西班牙) vs J2(奥地利) → 胜者v ?              │    ║
║  │ R32: K2(葡萄牙) vs L2(克罗地亚)→ 胜者v ?             │    ║
║  │ R32: B1(瑞士) vs J3(阿尔及利亚)→ 胜者v ?             │    ║
║  │ R32: D2(澳大利亚) vs G2(埃及) → 胜者v ?              │    ║
║  │ R32: J1(阿根廷) vs H2(佛得角) → 胜者v ?              │    ║
║  │ R32: K1(哥伦比亚) vs L3(加纳) → 胜者v ?              │    ║
║  └──────────────────────────────────────────────────────┘    ║
║                                                              ║
║  ⚠️ 关键: 上下半区交叉规则 (同一半区内走)                     ║
║  上半区强队: 巴西/法国/英格兰/德国/荷兰                       ║
║  下半区强队: 阿根廷/西班牙/葡萄牙/比利时/哥伦比亚             ║
║  死亡半区判定: 下半区(阿根廷vs西班牙在QF可能相遇)             ║
╚══════════════════════════════════════════════════════════════╝
"""

class TournamentArchitect:
    """赛会赛制师: 2026对阵树 + 半区分析"""
    
    BRACKET = BRACKET_2026
    
    @staticmethod
    def get_half(team: str) -> str:
        """返回球队在哪个半区"""
        top_half = {'巴西','德国','荷兰','法国','英格兰','墨西哥','科特迪瓦','挪威',
                     '日本','瑞典','巴拉圭','厄瓜多尔','加拿大','南非','摩洛哥','民主刚果'}
        return 'top' if team in top_half else 'bottom'
    
    @staticmethod
    def bracket_ascii() -> str:
        return BRACKET_2026


# ═══════════════════════════════════════════════
# 四、逆向推理中枢 (Fusion)
# ═══════════════════════════════════════════════

@dataclass
class MotivationReport:
    """动机还原报告"""
    match: str
    odds_anomaly: Dict
    points_motivation: Dict
    half_info: str
    conclusion: str = ""
    risk_flags: List[str] = field(default_factory=list)

def reverse_engineer_match(home: str, away: str, oh: float, od: float, oa: float,
                            group_id: str = '') -> MotivationReport:
    """三层融合逆向推理"""
    decoder = OddsDecoder.analyze(home, away, oh, od, oa)
    half = TournamentArchitect.get_half(home)
    
    # 简易动机推断
    if oh < 2.0:
        motivation = PointsPathReconstructor.infer_motivation(home, group_id, 1)
    elif oh < 4.0:
        motivation = PointsPathReconstructor.infer_motivation(home, group_id, 2)
    else:
        motivation = PointsPathReconstructor.infer_motivation(home, group_id, 3)
    
    # 融合结论
    flags = []
    if decoder['anomaly']['status'] != 'normal':
        flags.append(f"⚠️ 赔率异常: {decoder['anomaly']['note']}")
    
    vig = decoder['vig']
    if vig > 0.10:
        flags.append(f"💰 高抽水({vig:.1%}): 庄家对这场极度不确信")
    
    conclusion = f"{home} {'强' if oh < 2.5 else '中' if oh < 6 else '弱'}势 | "
    conclusion += f"半区={half} | 动机={motivation['motivation']}"
    
    return MotivationReport(
        match=f"{home} vs {away}",
        odds_anomaly=decoder,
        points_motivation=motivation,
        half_info=half,
        conclusion=conclusion,
        risk_flags=flags
    )
