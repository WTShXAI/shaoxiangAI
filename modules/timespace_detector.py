#!/usr/bin/env python3
"""
哨响AI - 时空断裂带检测模型
===========================
分析赛程密度、旅途疲劳、状态断层三大维度对球队表现的潜在影响。

检测维度:
  1. 赛程密度断裂带: 7天内比赛过密 → 体能透支风险
  2. 旅途疲劳断裂带: 客场飞行超过800km → 旅途疲劳影响
  3. 状态断层: 最近5场胜率较前5场下降30%+ → 状态滑坡信号
"""
import numpy as np
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass


@dataclass
class TimeSpaceFault:
    """时空断裂带信号"""
    fault_type: str       # schedule / travel / form
    severity: float       # 0~1 严重程度
    description: str      # 人类可读描述
    affected_team: str    # 受影响球队
    impact_direction: str # positive / negative
    confidence: float     # 置信度


class TimeSpaceFaultZoneDetector:
    """
    时空断裂带检测器

    用法:
        detector = TimeSpaceFaultZoneDetector()
        result = detector.analyze(
            home_team="曼联", away_team="利物浦",
            home_matches=[...], away_matches=[...],
            home_form="WWDLW", away_form="LWDWW",
            home_city="Manchester", away_city="Liverpool"
        )
    """

    # === 欧洲主要足球城市坐标 (lon, lat, altitude_m) ===
    CITY_COORDS = {
        "Manchester": (-2.24, 53.48, 40),
        "London": (-0.12, 51.51, 11),
        "Liverpool": (-2.99, 53.41, 20),
        "Munich": (11.58, 48.14, 519),
        "Berlin": (13.40, 52.52, 34),
        "Madrid": (-3.70, 40.42, 667),
        "Barcelona": (2.17, 41.39, 12),
        "Milan": (9.19, 45.46, 120),
        "Rome": (12.50, 41.90, 21),
        "Paris": (2.35, 48.86, 35),
        "Turin": (7.68, 45.07, 239),
        "Naples": (14.27, 40.85, 17),
        "Dortmund": (7.47, 51.51, 86),
        "Leipzig": (12.37, 51.34, 118),
        "Seville": (-5.98, 37.38, 7),
        "Valencia": (-0.38, 39.47, 15),
        "Lisbon": (-9.14, 38.72, 2),
        "Porto": (-8.61, 41.15, 104),
        "Amsterdam": (4.90, 52.37, 2),
        "Marseille": (5.37, 43.30, 7),
        "Glasgow": (-4.25, 55.86, 25),
        "Istanbul": (28.98, 41.01, 40),
    }

    # === 赛程密度检测 ===

    def detect_schedule_fault(self, team: str,
                               recent_matches: List[Dict]) -> Optional[TimeSpaceFault]:
        """
        检测7天内赛程密度是否过高

        Args:
            team: 球队名称
            recent_matches: 近期比赛列表，每项含 'date' 字段
        """
        now = datetime.now()
        recent_7 = [
            m for m in recent_matches
            if (now - self._parse_date(m.get("date", now.strftime("%Y-%m-%d")))).days <= 7
        ]
        density = len(recent_7)

        if density >= 3:
            severity = min(1.0, (density - 2) * 0.3)
            return TimeSpaceFault(
                fault_type="schedule",
                severity=severity,
                description=f"赛程过密: 7天{density}场比赛",
                affected_team=team,
                impact_direction="negative",
                confidence=0.75 + severity * 0.15,
            )
        return None

    # === 旅途疲劳检测 ===

    def detect_travel_fault(self, team: str, home_city: str,
                             away_city: str) -> Optional[TimeSpaceFault]:
        """
        检测客场长途旅行疲劳

        Args:
            team: 球队名称
            home_city: 主队城市
            away_city: 客队城市
        """
        h = self.CITY_COORDS.get(home_city, (0, 0, 0))
        a = self.CITY_COORDS.get(away_city, (0, 0, 0))

        # Haversine 距离公式
        R = 6371  # 地球半径(km)
        dlat = np.radians(a[1] - h[1])
        dlon = np.radians(a[0] - h[0])
        dist = 2 * R * np.arcsin(
            np.sqrt(
                np.sin(dlat / 2) ** 2 +
                np.cos(np.radians(h[1])) * np.cos(np.radians(a[1])) *
                np.sin(dlon / 2) ** 2
            )
        )

        if dist > 800:
            severity = min(1.0, (dist - 800) / 2000)
            return TimeSpaceFault(
                fault_type="travel",
                severity=severity,
                description=f"长途旅行疲劳: {dist:.0f}km",
                affected_team=team,
                impact_direction="negative",
                confidence=0.65 + severity * 0.2,
            )
        return None

    # === 状态断层检测 ===

    def detect_form_fault(self, team: str,
                           recent_form: List[str]) -> Optional[TimeSpaceFault]:
        """
        检测状态急剧下滑/上升

        Args:
            team: 球队名称
            recent_form: 最近比赛结果列表 ['W','D','L','W','L','W','D','...']
        """
        if len(recent_form) < 5:
            return None

        r5 = recent_form[-5:]   # 最近5场
        o5 = recent_form[-10:-5] if len(recent_form) >= 10 else recent_form[:-5]

        if not o5:
            return None

        r_win_rate = sum(1 for r in r5 if r == "W") / len(r5)
        o_win_rate = sum(1 for r in o5 if r == "W") / len(o5)
        drop = o_win_rate - r_win_rate

        if abs(drop) >= 0.3:
            direction = "negative" if drop > 0 else "positive"
            return TimeSpaceFault(
                fault_type="form",
                severity=min(1.0, abs(drop)),
                description=f"状态断层: {o_win_rate:.0%}→{r_win_rate:.0%}",
                affected_team=team,
                impact_direction=direction,
                confidence=0.70 + abs(drop) * 0.15,
            )
        return None

    # === 综合分析 ===

    def analyze(self, home_team: str, away_team: str,
                home_matches: List[Dict], away_matches: List[Dict],
                home_form: List[str], away_form: List[str],
                home_city: str = "", away_city: str = "") -> Dict:
        """
        综合分析两队时空断裂带

        Args:
            home_team: 主队名称
            away_team: 客队名称
            home_matches: 主队近期比赛（含date字段）
            away_matches: 客队近期比赛（含date字段）
            home_form: 主队近期战绩 ['W','D','L',...]
            away_form: 客队近期战绩 ['W','D','L',...]
            home_city: 主队所在城市
            away_city: 客队所在城市

        Returns:
            分析结果字典
        """
        home_faults, away_faults = [], []

        # 对主客队分别检测
        for team, matches, form, hc, ac in [
            (home_team, home_matches, home_form, home_city, away_city),
            (away_team, away_matches, away_form, away_city, home_city),
        ]:
            # 赛程密度检测
            fault = self.detect_schedule_fault(team, matches)
            if fault:
                (home_faults if team == home_team else away_faults).append(fault)

            # 旅途疲劳检测
            if hc and ac:
                fault = self.detect_travel_fault(team, hc, ac)
                if fault:
                    (home_faults if team == home_team else away_faults).append(fault)

            # 状态断层检测
            fault = self.detect_form_fault(team, form)
            if fault:
                (home_faults if team == home_team else away_faults).append(fault)

        # 计算影响指数
        home_impact = -sum(
            f.severity * f.confidence
            for f in home_faults
            if f.impact_direction == "negative"
        )
        away_impact = -sum(
            f.severity * f.confidence
            for f in away_faults
            if f.impact_direction == "negative"
        )

        net = "away" if home_impact > away_impact else (
            "home" if away_impact > home_impact else "neutral"
        )

        all_faults = home_faults + away_faults
        confidence = (
            np.mean([f.confidence for f in all_faults])
            if all_faults else 0.6
        )

        return {
            "home_faults": [
                {"type": f.fault_type, "severity": f.severity, "desc": f.description}
                for f in home_faults
            ],
            "away_faults": [
                {"type": f.fault_type, "severity": f.severity, "desc": f.description}
                for f in away_faults
            ],
            "home_impact": round(home_impact, 3),
            "away_impact": round(away_impact, 3),
            "net_advantage": net,
            "confidence": round(confidence, 3),
        }

    # === 工具方法 ===

    @staticmethod
    def _parse_date(date_str: str) -> datetime:
        """解析日期字符串"""
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y"):
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        return datetime.now()
