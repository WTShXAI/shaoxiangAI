"""
哨响AI · 赔率结构库 (odds_structure_db)
=====================================
设计目标（涛哥 2026-08-03 理论）：
  采集"赔率结构" + "该结构里正确的选项"，不记录比分/赛果本身。
  攒够量后做结构分类，未来用于识别某类结构的正确倾向 + 陷阱型检测。

本模块只做两件事：
  1. render_structure(row)  —— 把一行 GQ 赔率渲染成"结构特征向量"
  2. compute_labels(row)    —— 从赛果现场算出正确选项标签，然后【丢弃原始比分】

存储约定（严格遵循"只存结构+正确选项"）：
  - 结构特征：1X2去水概率 / OU线+去水概率 / AH线+去水概率(本数据集线恒为0) / CS梯子形状
  - 正确选项标签：label_1x2∈{H,D,A} / label_ou∈{O,U,P} / label_ah∈{H,A,P} / label_cs∈{比分串|MISS}
  - 数据为 null 的市场不强行填（保持稀疏真实）
  - 仅附加 source/league 两个极简上下文标签（结构含义随联赛/庄家而变，无此则分类无意义）
  - 绝不写入 score_home/score_away/result 任何字段
"""

import sqlite3
import json
import math
import time
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# 去水工具
# ---------------------------------------------------------------------------
def _devig2(h: float, a: float):
    """两变盘去水，返回 (p_h, p_a)；任一非法返回 None"""
    if not (h and a) or h < 1.01 or a < 1.01 or h > 50 or a > 50:
        return None
    inv = 1.0 / h + 1.0 / a
    if inv <= 0:
        return None
    return (1.0 / h / inv, 1.0 / a / inv)


def _devig3(h: float, d: float, a: float):
    """三变盘去水，返回 (p_h, p_d, p_a)"""
    if not (h and d and a):
        return None
    if min(h, d, a) < 1.01 or max(h, d, a) > 50:
        return None
    inv = 1.0 / h + 1.0 / d + 1.0 / a
    if inv <= 0:
        return None
    return (1.0 / h / inv, 1.0 / d / inv, 1.0 / a / inv)


def _cs_shape(cs_text: str):
    """解析波胆梯子 JSON，返回 (top1_prob, entropy, count) 或 None"""
    if not cs_text:
        return None
    try:
        ladder = json.loads(cs_text)
    except Exception:
        return None
    if not ladder:
        return None
    odds = []
    for item in ladder:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            try:
                odds.append(float(item[1]))
            except Exception:
                pass
    if len(odds) < 2:
        return None
    odds = [o for o in odds if o > 0]   # 过滤脏数据(赔率<=0)
    if len(odds) < 2:
        return None
    probs = [1.0 / o for o in odds]
    s = sum(probs)
    probs = [p / s for p in probs]
    top1 = probs[0]
    entropy = -sum(p * math.log(p) for p in probs if p > 0)
    return (top1, entropy, len(probs))


# ---------------------------------------------------------------------------
# 结构渲染 + 标签计算
# ---------------------------------------------------------------------------
@dataclass
class StructureRecord:
    source: str
    league: str
    # 结构特征（去水后的概率空间，跨场可比）
    s1x2_h: float = None
    s1x2_d: float = None
    s1x2_a: float = None
    sou_line: float = None
    sou_over: float = None
    sah_line: float = None
    sah_home: float = None
    sah_away: float = None          # 去水客队概率 (增强: 补全 AH 双侧)
    sah_home_odds: float = None     # 原始主队赔率 (增强: 算 AH 抽水)
    sah_away_odds: float = None     # 原始客队赔率 (增强: 算 AH 抽水)
    scs_top1: float = None
    scs_ent: float = None
    scs_cnt: int = None
    # 正确选项标签（由赛果现场算出，原始比分不入库）
    label_1x2: str = None   # H / D / A
    label_ou: str = None    # O / U / P(push)
    label_ah: str = None    # H / A / P(push)
    label_cs: str = None    # "2-1" / MISS


def render_structure(row: dict) -> StructureRecord:
    """把 GQ 一行渲染成结构特征（只看赔率，不看赛果）"""
    r = StructureRecord(source=row.get("source"), league=row.get("league"))

    d3 = _devig3(row.get("op_1x2_h"), row.get("op_1x2_d"), row.get("op_1x2_a"))
    if d3:
        r.s1x2_h, r.s1x2_d, r.s1x2_a = d3

    line = row.get("op_ou_line")
    d2 = _devig2(row.get("op_ou_over"), row.get("op_ou_under"))
    if d2 and line is not None and line > 0:   # 0.0 视为未解析，丢弃
        r.sou_line = line
        r.sou_over = d2[0]

    ah_line = row.get("op_ah_line")
    ah_h = row.get("op_ah_home")
    ah_a = row.get("op_ah_away")
    d2a = _devig2(ah_h, ah_a)
    if d2a and ah_line is not None:
        r.sah_line = ah_line
        r.sah_home = d2a[0]
        r.sah_away = d2a[1]
        if ah_h and ah_a and ah_h > 1.01 and ah_a > 1.01:
            r.sah_home_odds = float(ah_h)
            r.sah_away_odds = float(ah_a)

    cs = _cs_shape(row.get("op_cs"))
    if cs:
        r.scs_top1, r.scs_ent, r.scs_cnt = cs

    return r


def compute_labels(row: dict) -> dict:
    """从赛果算出正确选项标签；随后调用方只保留标签、丢弃 row 的赛果字段"""
    sh, sa = row.get("score_home"), row.get("score_away")
    result = row.get("result")
    labels = {"label_1x2": None, "label_ou": None, "label_ah": None, "label_cs": None}

    # 1X2：优先用 result 字段
    if result in ("home", "draw", "away"):
        labels["label_1x2"] = {"home": "H", "draw": "D", "away": "A"}[result]
    elif sh is not None and sa is not None:
        labels["label_1x2"] = "H" if sh > sa else ("A" if sa > sh else "D")

    # OU
    line = row.get("op_ou_line")
    if sh is not None and sa is not None and line is not None and line > 0:
        total = sh + sa
        labels["label_ou"] = "O" if total > line else ("U" if total < line else "P")

    # AH: 让球盘标签 = 净胜球 diff 与让球线 ah_line 比较
    #
    # ⚠ 事故记录 (2026-08-05): 本函数曾在 ah_line 恒为 0 的污染数据上运行 1219 场。
    #   采集器 parse_ah_line('') 返回 0.0 把「缺失让球线」伪造成「平手盘」,
    #   而让球线 = 0 时 "打赢让球盘" ≡ "赢球", label_ah 完全退化为
    #   「1X2 去掉平局」的二分类 —— 与 label_1x2 完美对角。
    #   消融实验证实: 去掉全部 xah_* 特征后 AUC 只变 0.0003, 即 AH 模型
    #   零信息量, 所谓「AH AUC 0.7015 三任务最强」纯属任务难度下降的假象。
    #
    #   现在 ah_line is None 的场次会被正确跳过 (标签为 None 而非伪标签)。
    #   ah_line == 0 仅在【真实平手盘】时才允许出现 —— 采集器已改为
    #   挖不到让球线时落 AH_UNK + line=None, 绝不再填 0 (铁律1)。
    ah_line = row.get("op_ah_line")
    if sh is not None and sa is not None and ah_line is not None:
        diff = sh - sa
        labels["label_ah"] = "H" if diff > ah_line else ("A" if diff < ah_line else "P")

    # CS
    if sh is not None and sa is not None:
        actual = f"{sh}-{sa}"
        try:
            ladder = json.loads(row["op_cs"]) if row.get("op_cs") else []
        except Exception:
            ladder = []
        scores = {str(it[0]) for it in ladder if isinstance(it, (list, tuple)) and len(it) >= 1}
        labels["label_cs"] = actual if actual in scores else "MISS"

    return labels


# ---------------------------------------------------------------------------
# 结构库读写
# ---------------------------------------------------------------------------
class StructureDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_schema()

    def _init_schema(self):
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS odds_structures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT,
            league TEXT,
            s1x2_h REAL, s1x2_d REAL, s1x2_a REAL,
            sou_line REAL, sou_over REAL,
            sah_line REAL, sah_home REAL,
            scs_top1 REAL, scs_ent REAL, scs_cnt INTEGER,
            label_1x2 TEXT, label_ou TEXT, label_ah TEXT, label_cs TEXT,
            ingested_at REAL
        )""")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_src ON odds_structures(source)")
        con.commit()
        con.close()

    def ingest_from_gq(self, gq_path: str) -> dict:
        """从 events.db 抽取结构+标签入库（不写任何赛果字段）。返回统计。"""
        src = sqlite3.connect(gq_path)
        scur = src.cursor()
        scur.execute("""
            SELECT source, league, op_1x2_h, op_1x2_d, op_1x2_a,
                   op_ou_line, op_ou_over, op_ou_under,
                   op_ah_line, op_ah_home, op_ah_away,
                   op_cs, score_home, score_away, result
            FROM match_outcomes
            WHERE is_valid = 1
        """)
        rows = scur.fetchall()
        src.close()

        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        stats = {"rows": 0, "with_1x2": 0, "with_ou": 0, "with_ah": 0, "with_cs": 0}
        for row in rows:
            keys = ["source", "league", "op_1x2_h", "op_1x2_d", "op_1x2_a",
                    "op_ou_line", "op_ou_over", "op_ou_under",
                    "op_ah_line", "op_ah_home", "op_ah_away",
                    "op_cs", "score_home", "score_away", "result"]
            r = dict(zip(keys, row))

            struct = render_structure(r)          # 只看赔率
            labels = compute_labels(r)            # 现场算标签，随后 row 被丢弃（不入结构库）

            # 至少要有一种结构特征 + 一个标签才算有效样本
            has_struct = any(v is not None for v in
                             [struct.s1x2_h, struct.sou_line, struct.sah_home, struct.scs_top1])
            has_label = any(v is not None for v in labels.values())
            if not (has_struct and has_label):
                continue

            cur.execute("""
                INSERT INTO odds_structures
                (source, league, s1x2_h, s1x2_d, s1x2_a, sou_line, sou_over,
                 sah_line, sah_home, scs_top1, scs_ent, scs_cnt,
                 label_1x2, label_ou, label_ah, label_cs, ingested_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                struct.source, struct.league,
                struct.s1x2_h, struct.s1x2_d, struct.s1x2_a,
                struct.sou_line, struct.sou_over,
                struct.sah_line, struct.sah_home,
                struct.scs_top1, struct.scs_ent, struct.scs_cnt,
                labels["label_1x2"], labels["label_ou"], labels["label_ah"], labels["label_cs"],
                time.time(),
            ))
            stats["rows"] += 1
            if struct.s1x2_h is not None:
                stats["with_1x2"] += 1
            if struct.sou_line is not None:
                stats["with_ou"] += 1
            if struct.sah_home is not None:
                stats["with_ah"] += 1
            if struct.scs_top1 is not None:
                stats["with_cs"] += 1

        con.commit()
        con.close()
        return stats

    def fetch_all(self):
        con = sqlite3.connect(self.db_path)
        cur = con.cursor()
        cur.execute("SELECT * FROM odds_structures")
        cols = [d[0] for d in cur.description]
        out = [dict(zip(cols, r)) for r in cur.fetchall()]
        con.close()
        return out


if __name__ == "__main__":
    import sys
    gq = r"D:\Architecture\data\events.db"
    db = r"D:\Architecture\data\shaoxiang_odds_structure.db"
    s = StructureDB(db).ingest_from_gq(gq)
    print("ingest stats:", s)
