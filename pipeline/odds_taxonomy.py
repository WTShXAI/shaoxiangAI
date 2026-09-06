"""
odds_taxonomy.py — 比赛赔率「理论分类」(医生必须先懂的教科书)

隐喻: 赔率=检查报告, 模型=医生。医生可以误诊(实操犯错), 但不能不会
(必须先把报告分对类, 才知道用哪套诊断方案)。本模块是"理论层":
  - 定义赔率报告的分类维度 + 每类的判定规则 (the theory)
  - classify() 把一份报告归入各维度类别

⚠️ 本分类是【草稿】, 口径按项目既有框架 + 本地知识库 + 网络盘口理论拟,
  待用户(涛哥)按"我说的"确认/修改。确认前模型不得据此实操
  (IR-21 建仓人工审批 + 本指令"100分之后再实操")。

分类维度 (v2, 8 维):
  D1 tier        可信度层: main(主流有DB深诊) / obscure(无DB结构读盘) / virtual(剔除 IR-30)
  D2 line_state  盘路层:   trap_candidate(顺人线陷阱候选) / low_water_smart(低水聪明边 IR-22-23)
                         / high_water_lure(高水诱导) / balanced
  D3 stage       阶段层:   opening(初盘立锚) / closing(临场平衡) / live(滚球动态)  (IR-26 三段)
  D4 consistency 一致性:   consistent(三市场自洽) / internal_divergence(1X2↔AH 半球线分歧>6pp)
  D5 draw_risk   平局层:   high(>=0.30) / low(<=0.20) / neutral  (平局提醒层 DRAW_ALERT=0.24)
  D6 water_level 水位层:   超低水(<0.75)/低水/中低水/中水/中高水/高水/超高水(>=1.05)/n/a(无AH)
                         (web 7级 + 项目 E4 water>=0.98 僵持标记)
  D7 eu_ah_fit   欧亚换算:  consistent(实际盘≈欧赔理论盘) / deeper(开大盘, 实际让得比该有的多)
                         / shallower(开小盘, 实际让得比该有的少) / unknown
                         (项目 bookmaker_sim/odds_handicap_converter.py 校准)
  D8 operation   操盘图案:  造冷造热/阻上诚实防 项目 E-枚举 + 漂移确认需求标记 (bookmaker_trap_detector.py E1-E12)

判定规则全部源自项目既有铁律(IR-22/23/26/30 + 平局提醒层 + 三段框架)与
本地知识库(bookmaker_trap_detector.py / odds_handicap_converter.py / 庄家定价白皮书)及
网络盘口理论, 非凭空发明。矛盾点已在 THEORY_NOTES 标注(低水双态/ DRAW_ALERT 数值/
AH-CS 信号有效性 / 铁律文件冲突)。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class OddsCase:
    """一份待分类的赔率报告 + 上下文。理论考卷用合成病例填充。"""
    match_key: str
    home: str
    away: str
    league: str
    home_odds: float
    draw_odds: float
    away_odds: float
    ah_line: Optional[float] = None
    ah_home_odds: Optional[float] = None
    ah_away_odds: Optional[float] = None
    ou_line: Optional[float] = None
    ou_over_odds: Optional[float] = None
    ou_under_odds: Optional[float] = None
    is_virtual: bool = False
    has_db_analysis: bool = False      # 主流联赛有 DB 可跑 analyze
    stage: str = "opening"             # opening / closing / live
    draw_alert: Optional[float] = None  # 市场去水 P(平), 来自平局提醒层
    # ── 漂移相关(造冷造热检测需 open→close, odds_changes tick 流; 单快照为 None) ──
    ah_line_open: Optional[float] = None     # 初盘 AH 让球
    ah_line_close: Optional[float] = None    # 即时 AH 让球
    fav_water_open: Optional[float] = None   # 初盘热门方 AH 水位
    fav_water_close: Optional[float] = None  # 即时热门方 AH 水位
    fav_odds_open: Optional[float] = None    # 初盘热门方 1X2 赔率
    fav_odds_close: Optional[float] = None   # 即时热门方 1X2 赔率
    extra: dict = field(default_factory=dict)


# ── 工具 ──────────────────────────────────────────────────────────────
def _devig_two(h: float, a: float) -> float:
    if not h or not a or h <= 0 or a <= 0:
        return 0.5
    ih, ia = 1.0 / h, 1.0 / a
    return ih / (ih + ia)


def _implied_1x2(c: OddsCase) -> Dict[str, float]:
    inv = {"home": 1 / c.home_odds, "draw": 1 / c.draw_odds, "away": 1 / c.away_odds}
    tot = sum(inv.values())
    return {k: v / tot for k, v in inv.items()}


def _favorite(c: OddsCase) -> str:
    imp = _implied_1x2(c)
    return max(imp, key=imp.get)


def _fav_odds(c: OddsCase) -> float:
    fav = _favorite(c)
    return {"home": c.home_odds, "draw": c.draw_odds, "away": c.away_odds}[fav]


def _fav_ah_water(c: OddsCase) -> Optional[float]:
    """热门方的 AH 水位(真实含义的水位, 0.75-2.10 区间); 无 AH 返回 None。"""
    if c.ah_home_odds is None or c.ah_away_odds is None:
        return None
    if c.ah_home_odds <= 0 or c.ah_away_odds <= 0:
        return None
    fav = _favorite(c)
    return c.ah_home_odds if fav == "home" else c.ah_away_odds


# ── 欧亚换算: 欧赔→理论 AH 让球幅度 (项目 odds_handicap_converter.py 校准) ──
def _theoretical_ah_mag(home: float, away: float) -> Optional[float]:
    """由 1X2 十进制赔率估理论 AH 让球幅度(绝对值, 0.25 档)。None=不可估。"""
    if not home or not away or home <= 0 or away <= 0:
        return None
    ratio = max(home, away) / min(home, away)
    # 项目 _estimate_fair_handicap() 比率→盘口带映射(5.4万场校准, scale=3.2)
    if ratio > 15:
        return 2.5
    if ratio > 10:
        return 2.25
    if ratio > 7:
        return 2.0
    if ratio > 5:
        return 1.75
    if ratio > 3.5:
        return 1.5
    if ratio > 2.5:
        return 1.25
    if ratio > 1.8:
        return 0.75
    if ratio > 1.4:
        return 0.5
    if ratio > 1.15:
        return 0.25
    return 0.0


# ── 各维度判定 (the theory) ──────────────────────────────────────────
def _tier(c: OddsCase) -> str:
    """可信度层 (IR-30 虚拟剔除 + 主流/obscure 分诊)。"""
    if c.is_virtual:
        return "virtual"
    return "main" if c.has_db_analysis else "obscure"


def _line_state(c: OddsCase) -> str:
    """盘路层 (IR-22/23: 低水=庄家护的聪明边; 顺人线=陷阱候选; 高水=诱导)。

    规则:
      - 顺人线陷阱候选: 强队深让(ah_line<=-1.5)且热门方低水(<1.85)
        → 大众顺人性押强队低水, 理论标 trap_candidate (需漂移证据才升级为陷阱)。
      - 低水聪明边 (IR-23): 最低水方=庄家护的聪明边, 须跟随。
        含"平局即最低水"情形(平局赔率<=热门且<1.85)——平局是庄家压低的保护方。
      - 高水诱导: 热门方高水(>=2.20) → 诱导大众追。
      - 其余: balanced。
    ⚠️ 低水双态(诚实边界): 低水=聪明边仅在【初盘】成立; 滚盘(live)低水=赔付压制(反向),
       见 ou_live_lowwater_rootcause.md (REQ-07)。live 阶段低水仍标 low_water_smart 但
       下游诊断须降级置信(不升级为硬信号)。
    """
    if c.is_virtual:
        return "balanced"  # 虚拟剔除, 盘路维度无意义
    fav = _favorite(c)
    fav_odds = {"home": c.home_odds, "draw": c.draw_odds, "away": c.away_odds}[fav]
    deep_handicap = (c.ah_line is not None) and (c.ah_line <= -1.5)
    if deep_handicap and fav_odds < 1.85:
        return "trap_candidate"
    if c.draw_odds <= fav_odds and c.draw_odds < 1.85:
        return "low_water_smart"
    if fav_odds >= 2.20:
        return "high_water_lure"
    return "balanced"


def _stage(c: OddsCase) -> str:
    """阶段层 (IR-26 三段框架: 初盘立锚 / 临场1h平衡 / 滚球动态调)。"""
    return c.stage if c.stage in ("opening", "closing", "live") else "opening"


def _consistency(c: OddsCase) -> str:
    """一致性层: 1X2 主胜视图 vs AH 主胜视图 是否分歧。

    仅在 AH 半球线(干净映射)时判断; 否则标 consistent(信息不足不妄断)。
    """
    if (c.ah_line is None or c.ah_home_odds is None or c.ah_away_odds is None
            or c.ah_home_odds <= 0 or c.ah_away_odds <= 0):
        return "consistent"
    if not (-1.0 < c.ah_line <= -0.5):  # 仅半球线可干净映射
        return "consistent"
    p_cover = _devig_two(c.ah_home_odds, c.ah_away_odds)  # P(主胜) via AH -0.5
    p_1x2_home = _implied_1x2(c)["home"]
    return "internal_divergence" if abs(p_cover - p_1x2_home) > 0.06 else "consistent"


def _draw_risk(c: OddsCase) -> str:
    """平局层 (平局提醒层口径: >=0.30 高 / <=0.20 低 / 中性)。

    ⚠️ DRAW_ALERT 真值锁定 0.24 (pipeline/draw_signal.py:30); bridge_service.py:1805=0.26、
       scripts/integrate_draw_signal.py:38=0.28 为副本漂移, 理论层以 0.24 为准。
    """
    if c.draw_alert is None:
        return "neutral"
    if c.draw_alert >= 0.30:
        return "high"
    if c.draw_alert <= 0.20:
        return "low"
    return "neutral"


def _water_level(c: OddsCase) -> str:
    """水位层 (web 7级 + 项目 E4 僵持标记)。

    取热门方 AH 水位(真实水位语义, 0.75-2.10); 无 AH 则 n/a(须欧亚转换代理)。
    分级(web 盘口理论):
      超低水 <0.75 / 低水 0.75-0.85 / 中低水 0.85-0.90 / 中水 0.90-0.95 /
      中高水 0.95-1.00 / 高水 1.00-1.05 / 超高水 >=1.05
    """
    w = _fav_ah_water(c)
    if w is None:
        return "n/a"
    if w < 0.75:
        return "超低水"
    if w < 0.85:
        return "低水"
    if w < 0.90:
        return "中低水"
    if w < 0.95:
        return "中水"
    if w < 1.00:
        return "中高水"
    if w < 1.05:
        return "高水"
    return "超高水"


def _eu_ah_fit(c: OddsCase) -> str:
    """欧亚换算一致性 (实际 AH 盘 vs 欧赔理论盘)。

    项目 odds_handicap_converter.py: handicap = round(scale*(p_home-p_away)*4)/4, scale=3.2,
    0.25 档。本函数用等价的赔率比→盘口带映射估理论幅度, 与实际 ah_line 比较:
      - 实际让球幅度 > 理论 +0.25 → deeper (开大盘, 实际让得比该有的多)
      - 实际让球幅度 < 理论 -0.25 → shallower (开小盘, 实际让得比该有的少)
      - 否则 consistent
    方向含义(诚实, 不作终判; 项目 E12 给方向线索):
      deeper=庄家把热门方让得更多(更难赢盘)→ 可能阻上 或 顺人线诱上(须 drift 确认);
      shallower=热门方让得更少(更易赢盘)→ 可能造热诱上。
    """
    if c.ah_line is None:
        return "unknown"
    theo = _theoretical_ah_mag(c.home_odds, c.away_odds)
    if theo is None:
        return "unknown"
    actual = abs(c.ah_line)
    if actual > theo + 0.25:
        return "deeper"
    if actual < theo - 0.25:
        return "shallower"
    return "consistent"


def _operation(c: OddsCase) -> str:
    """操盘图案 (项目 bookmaker_trap_detector.py E1-E12 命名体系)。

    漂移路径(有 open→close): 用 odds_changes tick 流判定造冷造热/阻上。
      - 升盘+降水(close 盘更深 & 水位降) → heat_up_line_drop_water (E3 升盘降水造热)
      - 降赔+升水(close 赔率降 & 水位升) → drop_odds_raise_water (E2 降赔升水诱)
      - 降盘(close 盘更浅)             → drop_line_cool (降盘阻上/造冷)
    单快照(无漂移): 用静态信号标候选, 真陷阱须漂移确认(诚实标注 needs_drift 思想)。
      - 深让+热门低水 → deep_handicap_trap_candidate (E6 深盘诱杀, 需漂移)
      - 热门高水     → shallow_line_hot (E1 浅盘大热)
      - 水位>=0.98 僵持 → deadlock_draw (E4 平半高水死扛→平局)
      - 其余         → balanced
    """
    if c.is_virtual:
        return "balanced"
    # 漂移路径 (ah_line 为负: 升盘=更深=更负; 降盘=更浅=更正)
    if (c.ah_line_open is not None and c.ah_line_close is not None
            and c.fav_water_open is not None and c.fav_water_close is not None):
        deeper = c.ah_line_close < c.ah_line_open - 0.25       # 升盘(让球加深)
        shallower_line = c.ah_line_close > c.ah_line_open + 0.25  # 降盘(让球变浅)
        if deeper and c.fav_water_close < c.fav_water_open - 0.05:
            return "heat_up_line_drop_water"                   # E3 升盘降水造热
        if (c.fav_odds_open is not None and c.fav_odds_close is not None
                and c.fav_odds_close < c.fav_odds_open - 0.10
                and c.fav_water_close > c.fav_water_open + 0.05):
            return "drop_odds_raise_water"                     # E2 降赔升水诱
        if shallower_line:
            return "drop_line_cool"                            # 降盘阻上/造冷
    # 单快照静态
    fav_odds = _fav_odds(c)
    deep_handicap = (c.ah_line is not None) and (c.ah_line <= -1.5)
    if deep_handicap and fav_odds < 1.85:
        return "deep_handicap_trap_candidate"
    if fav_odds >= 2.20:
        return "shallow_line_hot"
    w = _fav_ah_water(c)
    # E4 平半高水死扛→平局: 水位【僵持在高水带 0.98-1.10】(项目 water>=0.98),
    # 不含极端超高水(1.85/2.00 属大让球公平价, 非死扛僵持)。
    if w is not None and 0.98 <= w < 1.10:
        return "deadlock_draw"
    return "balanced"


DIMENSIONS = ("tier", "line_state", "stage", "consistency",
              "draw_risk", "water_level", "eu_ah_fit", "operation")


def classify(c: OddsCase) -> Dict[str, str]:
    """把一份报告归入各维度类别 (the theory in action)。"""
    return {
        "tier": _tier(c),
        "line_state": _line_state(c),
        "stage": _stage(c),
        "consistency": _consistency(c),
        "draw_risk": _draw_risk(c),
        "water_level": _water_level(c),
        "eu_ah_fit": _eu_ah_fit(c),
        "operation": _operation(c),
    }


# 人类可读说明 (教科书正文) — 整合 web + 本地知识, 标注矛盾点
THEORY_NOTES = {
    "tier": "可信度层决定'能不能深诊': virtual(IR-30 剔除)一律不碰; main(主流联赛有DB)可跑 analyze 出漂移/陷阱+注码建议; obscure(无DB)只能做结构性读盘, 置信度低, 不假装确定。",
    "line_state": "盘路层是陷阱识别核心(IR-22/23): 单张 live 无开盘价时, 低水线=庄家真实预期(聪明边)须跟随, 绝不可反向说成'陷阱诱多'; 真陷阱判定必须有开盘→收盘漂移证据。顺人线(强队深让低水)=大众顺人性押注=陷阱候选, 但须漂移确认才升级。⚠️低水双态: 低水=聪明边仅初盘成立; 滚盘低水=赔付压制(反向, ou_live_lowwater_rootcause.md REQ-07)。",
    "stage": "阶段层对应 IR-26 三段框架: 初盘立锚→临场1h平衡筹码→滚球动态调。不同阶段的盘口含义不同。⚠️真实病例库(match_outcomes)仅初盘→stage 维度退化; 临场/滚球分类须切 odds_changes/odds_snapshots tick 流(PREMATCH_DEVIATION.md H3: 无live时用 open→close 漂移代理, 变短favorite胜率0.684 vs 变长0.614, gap 7pp=edge窗口在开赛前)。",
    "consistency": "一致性层: 1X2 与 AH 对'主胜'的看法若分歧>6pp, 说明市场内部不一致(潜在陷阱/机会信号); 同源自洽则无此信号。项目分歧闸门: 共识热门概率压至0.41(OOS实测0.405)。",
    "draw_risk": "平局层沿用平局提醒层(DRAW_ALERT=0.24, draw_signal.py:30 为准): 市场去水 P(平)>=0.30 高平局警 / <=0.20 低 / 中性。⚠️DRAW_ALERT 副本漂移(bridge 0.26 / integrate 0.28), 理论层锁 0.24。平局阈值一律走 draw_signal.py, 市场即最好平局源, 勿另起炉灶。",
    "water_level": "水位层(web 7级): 超低水<0.75/低水0.75-0.85/中低水0.85-0.90/中水0.90-0.95/中高水0.95-1.00/高水1.00-1.05/超高水>=1.05。项目阈值: E4 water>=0.98 僵持=平局信号(deadlock_draw); OU 水位差Δ>=0.20 时低水方=庄家真实站队(live_goal_probe 半场OU_0.5低水over命中75-87%)。总水(抽水)基线 E7: <5%极度自信热门单边 / 5-7%正常偏低 / 7-10%正常 / >10%不确定需对冲; 联赛 overround_base≈0.055-0.068。",
    "eu_ah_fit": "欧亚换算层(项目 odds_handicap_converter.py 校准 scale=3.2): 用欧赔比→理论 AH 盘带, 与实际 ah_line 比。deeper=开大盘(实际让得比该有的多, 可能阻上或顺人线诱上); shallower=开小盘(实际让得少, 可能造热诱上); consistent=自洽。方向不作终判, 项目 E12 给线索(欧主胜P>0.55且亚盘<0.5→诱下; 欧平P>0.24且0.4<ah<0.6→诱平; 客胜P<0.15且ah>1.5→赢球输盘)。",
    "operation": "操盘图案层(项目 bookmaker_trap_detector.py E1-E12 命名, 非网络'降盘阻上/升水阻上'分类词): 造热诱盘类 E3升盘降水/E1浅盘大热/E6深盘诱杀/E2降赔升水/E11资金过热赔率不动→反向; 阻上诚实防类 E10深盘退热(gap>0.3+高水→真实保护)/E7抽水异常/E4平半高水死扛→平局。E5临场突变: 单家异动=反向思考, 多机构同步=真实信息顺势(最接近'临场'口诀; 网络口诀'临场升盘看支撑/临场降盘防冷门''买降不买升'本仓库未出现)。⚠️真陷阱须 open→close 漂移确认, 单快照仅标候选。",
}
