"""
哨响AI · 赔率结构特征库 (odds_feature_library)
=============================================
定位：把"采集到的赔率结构 + 正确选项标签"提炼成【模型可直接训练的特征矩阵】。
本库是哨响AI自己的训练资产 —— 只含特征向量 + 正确选项标签，绝不写入比分/赛果。

数据流：
  events.db(match_outcomes: 赔率+赛果同行)
    -> render_structure() 只看赔率，渲染结构
    -> compute_labels()   现场算正确选项标签（随后原始比分被丢弃）
    -> extract_features() 把结构压成定长数值特征向量
    -> 写入 shaoxiang_feature_library.db（own database）

训练消费：
  load_Xy(task) -> (X_train, y_train, X_test, y_test)   task in {1x2, ou, ah}
  切分：walkforward（按 kickoff 时间，早70%训/晚30%测，防未来泄露）

特征设计（全部由赔率推导，无赛果）：
  1X2 块: 去水概率 h/d/a, margin, fav概率, draw_gap, home_fav, h_minus_a
  OU  块: line, over概率, under概率, margin, has_ou(缺失填中性+flag)
  AH  块: line, home概率, has_ah  + 增强块(gap跨市场分歧/line_abs/line_bucket/fav_align/margin/home_str)
  CS  块: top1概率, 熵, 梯子项数, has_cs
  上下文: league频率, kickoff时段
"""
import sqlite3
import json
import math
import time
from pipeline.odds_structure_db import render_structure, compute_labels
from pipeline.opening_line import build_opening_lines  # SSoT: 真·初盘主线(铁律: OU/AH 禁直读 op_*_line)

# 特征名（定长，顺序即列序）
FEATURE_NAMES = [
    # 1X2 结构块
    "x1_h", "x1_d", "x1_a", "x1_margin", "x1_fav", "x1_drawgap", "x1_homefav", "x1_hminusa",
    # OU 结构块
    "xou_line", "xou_over", "xou_under", "xou_margin", "xou_has",
    # AH 结构块
    "xah_line", "xah_home", "xah_has",
    # CS 结构块
    "xcs_top1", "xcs_ent", "xcs_cnt", "xcs_has",
    # 上下文
    "x_league_freq", "x_kickoff_band",
    # AH 增强块 (edge 特征, 2026-08-08: AH 作为让球盘 edge, 补跨市场分歧/盘口层级/市场共识/抽水)
    # ── 注: tick 原始赔率特征(ftick_*) 已于 2026-08-22 移除 ──
    # 根因: 它们把去水概率反推回赔率尾数(%10)判"4=陷阱/1,2,9=强队", 属原始赔率值特征,
    # 直接违反抗诱导铁律"禁原始赔率值作特征/阈值"。模型在学庄家诱导噪声而非信号
    # (对应涛哥"模型学习力不够")。现只保留去水概率/漂移/跨市场残差三类不变量。
    #   xah_away      去水客队概率(补全双侧)
    #   xah_gap       AH主队概率 - 1X2主队概率 (跨市场软线价差 = 铁律真edge)
    #   xah_line_abs  |让球线| (spread 量级)
    #   xah_line_bucket 让球线层级(0/0.25/.../2 => 0..8, >2 => 9)
    #   xah_fav_align AH热门 == 1X2热门 ?
    #   xah_margin    AH 市场抽水(原始赔率)
    #   xah_home_str  sah_home - 0.5 (spread 市场主队强度, 带符号)
    "xah_away", "xah_gap", "xah_line_abs", "xah_line_bucket",
    "xah_fav_align", "xah_margin", "xah_home_str",
]
N_FEAT = len(FEATURE_NAMES)

_LABEL1X2 = {"H": 0, "D": 1, "A": 2}
_LABELSIDE = {"O": 0, "U": 1, "H": 0, "A": 1}  # OU/AH 统一: 0=首选(over/home), 1=次选(under/away)

# ---------------------------------------------------------------- 市场健康护栏
# 事故 (2026-08-05): 采集器 parse_ah_line('') 返回 0.0, 把「缺失让球线」伪造成
# 「平手盘」, 全库 op_ah_line 恒为 0 -> xah_line 零方差(死特征),
# label_ah 退化为「1X2 去掉平局」-> 伪造出「AH AUC 0.7015 三任务最强」的假结论。
# 消融实验: 去掉全部 xah_* 后 AUC 仅变 0.0003 (即零贡献)。
#
# 护栏: 建库前对每个盘口市场做健康检查, 不健康就整块剥离(特征+标签都不产出),
# 宁可少一个任务, 也不让死特征/伪标签进训练集。数据修好后自动恢复。
MIN_MARKET_SAMPLES = 200      # 有效样本下限
MIN_LINE_NUNIQUE = 2          # 盘口线唯一值下限 (零方差 = 死特征)


def market_health_check(rows: list, col_names: list, market: str) -> dict:
    """检查某盘口市场的数据是否可用于建模。

    Returns: {"ok": bool, "reason": str, "n": int, "nunique": int}
    """
    line_col = {"OU": "op_ou_line", "AH": "op_ah_line"}[market]
    side_col = {"OU": "op_ou_over", "AH": "op_ah_home"}[market]
    li, si = col_names.index(line_col), col_names.index(side_col)

    lines = [r[li] for r in rows if r[li] is not None and r[si] is not None]
    n, nuniq = len(lines), len(set(lines))

    if n < MIN_MARKET_SAMPLES:
        return {"ok": False, "n": n, "nunique": nuniq,
                "reason": f"有效样本 {n} < {MIN_MARKET_SAMPLES}"}
    if nuniq < MIN_LINE_NUNIQUE:
        return {"ok": False, "n": n, "nunique": nuniq,
                "reason": f"盘口线零方差 (唯一值={nuniq}, 恒={lines[0] if lines else '?'}) — 疑似采集伪造"}
    return {"ok": True, "n": n, "nunique": nuniq, "reason": "healthy"}


def _parse_kickoff_band(kickoff):
    """字符串 kickoff -> 时段 0..3 (0-6,6-12,12-18,18-24)，解析失败返回 0"""
    if not kickoff:
        return 0.0
    try:
        s = str(kickoff).replace("T", " ").strip()
        hh = int(s[11:13])
        return float(min(3, hh // 6))
    except Exception:
        return 0.0


def extract_features(struct, league_freq: float, kickoff) -> list:
    """把 StructureRecord 压成定长特征向量。缺失市场填中性值 + has_flag=0。"""
    f = [0.0] * N_FEAT

    # ---- 1X2 块 ----
    h, d, a = struct.s1x2_h, struct.s1x2_d, struct.s1x2_a
    if h is not None and d is not None and a is not None:
        f[0], f[1], f[2] = h, d, a
        inv = 1.0 / h + 1.0 / d + 1.0 / a
        f[3] = inv - 1.0                                   # margin
        f[4] = max(h, d, a)                                # fav 概率(去水)
        f[5] = d - (h + a) / 2.0                            # draw_gap
        f[6] = 1.0 if h == max(h, d, a) else 0.0           # home 是否 fav
        f[7] = h - a                                        # h - a

    # ---- OU 块 ----
    if struct.sou_line is not None and struct.sou_over is not None:
        ov = struct.sou_over
        f[8] = struct.sou_line
        f[9] = ov
        f[10] = 1.0 - ov
        inv = 1.0 / ov + 1.0 / (1.0 - ov)
        f[11] = inv - 1.0
        f[12] = 1.0

    # ---- AH 块 ----
    if struct.sah_home is not None and struct.sah_line is not None:
        f[13] = struct.sah_line
        f[14] = struct.sah_home
        f[15] = 1.0
        # ---- AH 增强块 (edge 特征, 2026-08-08) ----
        # 用 FEATURE_NAMES.index 取位置, 避免硬编码索引在增删特征时越界 (2026-08-22 修复)
        _ia = FEATURE_NAMES.index("xah_away")
        _ig = FEATURE_NAMES.index("xah_gap")
        _il = FEATURE_NAMES.index("xah_line_abs")
        _ib = FEATURE_NAMES.index("xah_line_bucket")
        _if = FEATURE_NAMES.index("xah_fav_align")
        _im = FEATURE_NAMES.index("xah_margin")
        _is = FEATURE_NAMES.index("xah_home_str")
        f[_ia] = struct.sah_away if struct.sah_away is not None else (1.0 - struct.sah_home)
        f[_il] = abs(struct.sah_line)                       # spread 量级
        f[_ib] = min(9.0, float(round(abs(struct.sah_line) / 0.25)))  # 盘口层级 0..9
        if h is not None and d is not None and a is not None:
            f[_ig] = struct.sah_home - h                    # 跨市场分歧 (AH主队概率 - 1X2主队概率)
            f[_if] = 1.0 if (struct.sah_home > 0.5) == (h == max(h, d, a)) else 0.0  # 市场共识
            f[_is] = struct.sah_home - 0.5                  # spread 主队强度(带符号)
        if struct.sah_home_odds and struct.sah_away_odds:
            f[_im] = 1.0 / struct.sah_home_odds + 1.0 / struct.sah_away_odds - 1.0  # AH 抽水

    # ---- CS 块 ----
    if struct.scs_top1 is not None:
        f[16] = struct.scs_top1
        f[17] = struct.scs_ent if struct.scs_ent is not None else 0.0
        f[18] = min(1.0, (struct.scs_cnt or 0) / 20.0)     # 归一化项数
        f[19] = 1.0

    # ---- 上下文 ----
    f[20] = league_freq
    f[21] = _parse_kickoff_band(kickoff)

    # ── tick 原始赔率特征已移除 (2026-08-22, 抗诱导铁律) ──
    # 原逻辑把去水概率反推回赔率尾数(%10)判"4=陷阱/1,2,9=强队", 属原始赔率值特征,
    # 违反"禁原始赔率值作特征/阈值"。现所有 tick 列恒为中性 0.0,
    # 仅保留去水概率 / 开盘→临场漂移 / 跨市场残差 三类不变量。
    return f


class FeatureLibrary:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_schema()

    def _init_schema(self):
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cols = ", ".join(f"{name} REAL" for name in FEATURE_NAMES)
        cur.execute(f"""
        CREATE TABLE IF NOT EXISTS features (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT, league TEXT, kickoff TEXT,
            {cols},
            label_1x2 INTEGER, label_ou INTEGER, label_ah INTEGER
        )""")
        # 列补齐：旧表可能缺 tick 列
        existing = {r[1] for r in cur.execute("PRAGMA table_info(features)")}
        for name in FEATURE_NAMES:
            if name not in existing:
                cur.execute(f"ALTER TABLE features ADD COLUMN {name} REAL DEFAULT 0")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_kick ON features(kickoff)")
        con.commit()
        con.close()

    def build_from_gq(self, gq_path: str) -> dict:
        """从 events.db 采集->提特征->写特征库（不写任何赛果）。返回统计。"""
        src = sqlite3.connect(gq_path)
        scur = src.cursor()
        scur.execute("""
            SELECT source, league, kickoff, home, away,
                   op_1x2_h, op_1x2_d, op_1x2_a,
                   op_ou_line, op_ou_over, op_ou_under,
                   op_ah_line, op_ah_home, op_ah_away,
                   op_cs, score_home, score_away, result
            FROM match_outcomes
            WHERE is_valid = 1
              AND COALESCE(is_virtual, 0) = 0   -- 根拦截: 虚拟/电子盘不进训练
              -- 2026-08-19: 移除 "gq 且无半场比分" 截断代理。该代理已失效:
              -- ht_score clobber 修复+回填后, 无HT gq 场终场分布健康(avg 2.94 球),
              -- 且 match_outcomes 终场与 live matches 100% 一致(无冻结)。
              -- 保留 is_valid + 虚拟盘拦截已足够; 与 clean_outcomes.drop_truncated 对齐。
        """)
        rows = scur.fetchall()
        src.close()

        # ---- SSoT 覆盖: 用 opening_line 真·主盘线覆盖 match_outcomes 残留可能偏差的 op_ou_*/op_ah_* ----
        # 铁律(2026-08-05): OU/AH 训练禁止直读 match_outcomes 盘口列。op_ou_line 虽已回填(95%对齐),
        # 仍有 ~3.8% 差>0.5 球的残留偏差会错标 OU 标签。这里以 SSoT 为唯一权威覆盖。
        _ou_map, _ah_map = {}, {}
        try:
            for _, s in build_opening_lines(market="OU").iterrows():
                _ou_map[s["match_key"]] = (float(s["line"]), float(s["over"]), float(s["under"]))
        except Exception as e:
            print(f"  [SSoT] OU 主盘线重建失败, 回退 op_ou_line: {e}")
        try:
            for _, s in build_opening_lines(market="AH").iterrows():
                _ah_map[s["match_key"]] = (float(s["line"]), float(s["home"]), float(s["away"]))
        except Exception as e:
            print(f"  [SSoT] AH 主盘线重建失败, 回退 op_ah_line: {e}")
        print(f"  [SSoT] OU 主盘线 {len(_ou_map)} 场 / AH 主盘线 {len(_ah_map)} 场 已载入覆盖表")

        # 联赛频率（上下文特征）
        league_cnt = {}
        for r in rows:
            lg = r[1]
            league_cnt[lg] = league_cnt.get(lg, 0) + 1
        total = max(1, len(rows))
        league_freq = {lg: c / total for lg, c in league_cnt.items()}

        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("DELETE FROM features")  # 重建（可重跑累积）
        stats = {"rows": 0, "labeled_1x2": 0, "labeled_ou": 0, "labeled_ah": 0}
        col_names = ["source", "league", "kickoff", "home", "away", "op_1x2_h", "op_1x2_d", "op_1x2_a",
                     "op_ou_line", "op_ou_over", "op_ou_under",
                     "op_ah_line", "op_ah_home", "op_ah_away",
                     "op_cs", "score_home", "score_away", "result"]

        # ---- 市场健康护栏: 不健康的盘口整块剥离, 绝不产出死特征/伪标签 ----
        blocked = set()
        for mkt in ("OU", "AH"):
            h = market_health_check(rows, col_names, mkt)
            stats[f"health_{mkt.lower()}"] = h
            if not h["ok"]:
                blocked.add(mkt)
                print(f"  [护栏] {mkt} 市场不可用 -> 剥离特征与标签。原因: {h['reason']}")
            else:
                print(f"  [护栏] {mkt} 市场健康 (n={h['n']}, 线唯一值={h['nunique']})")
        stats["blocked_markets"] = sorted(blocked)

        for row in rows:
            r = dict(zip(col_names, row))
            # 被护栏拦下的市场: 抹掉盘口线 -> render_structure / compute_labels
            # 都会因 line is None 自动跳过, 特征填中性值 + has_flag=0, 标签为 None
            if "OU" in blocked:
                r["op_ou_line"] = None
            if "AH" in blocked:
                r["op_ah_line"] = None
            # SSoT 覆盖: 用真·主盘线替换残留偏差的 op_ou_*/op_ah_* (护栏已拦的市场不动)
            _mk = f'{r.get("home")} vs {r.get("away")}'
            if _mk in _ou_map and "OU" not in blocked:
                ln, ov, un = _ou_map[_mk]
                r["op_ou_line"], r["op_ou_over"], r["op_ou_under"] = ln, ov, un
            if _mk in _ah_map and "AH" not in blocked:
                ln, hm, aw = _ah_map[_mk]
                r["op_ah_line"], r["op_ah_home"], r["op_ah_away"] = ln, hm, aw
            struct = render_structure(r)           # 只看赔率
            labels = compute_labels(r)             # 现场算标签，原始比分不入库
            if struct.s1x2_h is None and struct.sou_line is None \
               and struct.sah_home is None and struct.scs_top1 is None:
                continue
            feat = extract_features(struct, league_freq.get(r.get("league"), 0.0), r.get("kickoff"))

            l1 = _LABEL1X2.get(labels["label_1x2"]) if labels["label_1x2"] in ("H", "D", "A") else None
            lou = _LABELSIDE.get(labels["label_ou"]) if labels["label_ou"] in ("O", "U") else None
            lah = _LABELSIDE.get(labels["label_ah"]) if labels["label_ah"] in ("H", "A") else None

            placeholders = ",".join(["?"] * (3 + N_FEAT + 3))
            cur.execute(f"""
                INSERT INTO features
                (source, league, kickoff, {", ".join(FEATURE_NAMES)},
                 label_1x2, label_ou, label_ah)
                VALUES ({placeholders})
            """, (r.get("source"), r.get("league"), str(r.get("kickoff")),
                  *feat, l1, lou, lah))
            stats["rows"] += 1
            if l1 is not None:
                stats["labeled_1x2"] += 1
            if lou is not None:
                stats["labeled_ou"] += 1
            if lah is not None:
                stats["labeled_ah"] += 1
        con.commit()
        con.close()
        return stats

    def load_Xy(self, task: str, split: str = "walkforward"):
        """返回 (X_train, y_train, X_test, y_test)。task in {1x2, ou, ah}。"""
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        col_list = ", ".join(FEATURE_NAMES)
        cur.execute(f"SELECT {col_list}, label_1x2, label_ou, label_ah, kickoff FROM features")
        all_rows = cur.fetchall()
        con.close()

        lab_idx = {"1x2": N_FEAT, "ou": N_FEAT + 1, "ah": N_FEAT + 2}[task]
        samples = []
        for row in all_rows:
            y = row[lab_idx]
            if y is None:
                continue
            x = [float(v) for v in row[:N_FEAT]]
            kick = row[N_FEAT + 3] or ""
            samples.append((kick, x, int(y)))

        # walkforward：按 kickoff 字符串排序（ISO 格式可字典序比较），早70%训/晚30%测
        samples.sort(key=lambda s: s[0])
        n = len(samples)
        cut = int(n * 0.7)
        train = samples[:cut]
        test = samples[cut:]

        def unpack(part):
            X = [s[1] for s in part]
            y = [s[2] for s in part]
            return X, y

        Xtr, ytr = unpack(train)
        Xte, yte = unpack(test)
        return Xtr, ytr, Xte, yte


if __name__ == "__main__":
    import sys
    gq = r"D:\Architecture\data\events.db"
    out = r"D:\Architecture\data\shaoxiang_feature_library.db"
    s = FeatureLibrary(out).build_from_gq(gq)
    print("feature library build:", s)
