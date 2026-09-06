"""
build_independent_features.py — 构造"真独立"特征(彻底脱离赔率, 根除市场镜像)

根因修复(2026-07-25):
  旧 Chain3 的 81 特征 ~80% 由赔率衍生 -> 模型=市场镜像 -> edge≈0。
  且 unified_predictor 推理时把 55 个非赔率特征填训练中位数 -> 部署即退化成赔率平滑。
  本脚本从"比赛结果"本身算特征, 完全不碰赔率, 产出 indep_features 表:
    Elo(实力评级) / 主客形式 / 休息天数 / 交锋H2H / 联赛强度。

覆盖: matches(WH+IW) + fd_matches(13联赛, 此前训练完全未用, 白白丢 23854 样本)。
队名经 team_canonical.aliases_json 归一为 canonical key, 两源合并成统一时序流。

用法: .ocr_venv/Scripts/python.exe scripts/build_independent_features.py
"""
import sqlite3, os, json, ast
from collections import defaultdict, deque
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "football_data.db")


def build_alias_map(cur):
    """alias(小写) -> canonical。包含 canonical 自身。"""
    amap = {}
    for canon, aj in cur.execute("SELECT canonical, aliases_json FROM team_canonical"):
        amap[canon.strip().lower()] = canon
        if aj:
            try:
                lst = json.loads(aj) if aj.strip().startswith("[") else ast.literal_eval(aj)
                for a in lst:
                    amap[str(a).strip().lower()] = canon
            except Exception:
                pass
    return amap


def canon(team, amap):
    if not team:
        return None
    t = str(team).strip().lower()
    return amap.get(t, team.strip())  # 未知队名用原名(最佳努力)


def load_rows(cur, amap):
    rows = []
    # matches (WH+IW)
    for hn, an, md, hs, as_, lr, lg in cur.execute(
        "SELECT home_team_name, away_team_name, match_date, home_score, away_score, final_result, league_name "
        "FROM matches WHERE final_result IN ('H','D','A')"):
        rows.append((str(md)[:10], canon(hn, amap), canon(an, amap),
                     int(hs) if hs is not None else None, int(as_) if as_ is not None else None,
                     lr, lg, 'whiw'))
    # fd_matches
    for hn, an, ud, hs, as_, comp in cur.execute(
        "SELECT home_team, away_team, utc_date, home_score, away_score, competition_name "
        "FROM fd_matches WHERE home_score IS NOT NULL AND away_score IS NOT NULL AND status='FINISHED'"):
        try:
            hs_i, as_i = int(hs), int(as_)
        except Exception:
            continue
        res = 'H' if hs_i > as_i else ('D' if hs_i == as_i else 'A')
        rows.append((str(ud)[:10], canon(hn, amap), canon(an, amap), hs_i, as_i, res, comp, 'fd'))
    # 按日期排序(稳定)
    rows.sort(key=lambda r: r[0])
    return rows


def compute(rows):
    """返回每条比赛的特征 dict 列表(按输入顺序, 已填好赛前特征)。"""
    elo = defaultdict(lambda: 1500.0)
    HOME_ADV = 60.0  # Elo 主场优势
    K = 30.0
    home_form = defaultdict(lambda: deque(maxlen=5))
    away_form = defaultdict(lambda: deque(maxlen=5))
    last_date = {}            # team -> 上次比赛日期
    h2h = defaultdict(lambda: deque(maxlen=10))  # frozenset(team) -> 主队视角结果
    league_elo = defaultdict(list)  # league -> 已赛队伍 elo 快照(用赛前值近似)

    out = []
    for date_s, h, a, hs, as_, res, lg, src in rows:
        # 赛前特征
        elo_h, elo_a = elo[h], elo[a]
        elo_diff = elo_h + HOME_ADV - elo_a
        fh = np.mean(home_form[h]) if home_form[h] else 0.5
        fa = np.mean(away_form[a]) if away_form[a] else 0.5
        rh = (pd_date(date_s) - last_date[h]).days if h in last_date else 7
        ra = (pd_date(date_s) - last_date[a]).days if a in last_date else 7
        key = frozenset((h, a))
        hh = list(h2h[key])
        if hh:
            hw = sum(1 for x in hh if x == 1) / len(hh)
            hd = sum(1 for x in hh if x == 0.5) / len(hh)
            ha = sum(1 for x in hh if x == 0) / len(hh)
        else:
            hw = hd = ha = 1 / 3
        # 联赛强度: 该联赛已出现队伍的平均 elo(赛前近似)
        ls = np.mean(league_elo[lg]) if league_elo[lg] else 1500.0
        out.append({
            "match_date": date_s, "home": h, "away": a, "final_result": res,
            "league": lg, "src": src,
            "elo_home": round(elo_h, 1), "elo_away": round(elo_a, 1), "elo_diff": round(elo_diff, 1),
            "form_home": round(fh, 3), "form_away": round(fa, 3),
            "form_diff": round(fh - fa, 3),
            "rest_home": rh, "rest_away": ra, "rest_diff": rh - ra,
            "h2h_home_win": round(hw, 3), "h2h_draw": round(hd, 3), "h2h_away_win": round(ha, 3),
            "league_strength": round(ls, 1),
        })
        # 更新 Elo
        eh, ea = elo[h], elo[a]
        exp_h = 1.0 / (1.0 + 10 ** (-(eh + HOME_ADV - ea) / 400.0))
        exp_a = 1.0 - exp_h
        act_h = 1.0 if res == 'H' else (0.5 if res == 'D' else 0.0)
        act_a = 1.0 - act_h
        elo[h] = eh + K * (act_h - exp_h)
        elo[a] = ea + K * (act_a - exp_a)
        # 更新形式
        r_h = act_h  # 主队视角
        home_form[h].append(r_h)
        away_form[a].append(1.0 - act_a)  # 客队视角(客胜=1)
        # 更新休息
        last_date[h] = pd_date(date_s)
        last_date[a] = pd_date(date_s)
        # 更新 H2H
        h2h[key].append(r_h)
        # 更新联赛强度
        league_elo[lg].append(eh)
        league_elo[lg].append(ea)
    return out


def pd_date(s):
    from datetime import datetime
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except Exception:
        return datetime(2000, 1, 1)


def main():
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    print("=== 构造独立特征 ===")
    amap = build_alias_map(cur)
    print(f"  alias 映射: {len(amap)} 条")
    rows = load_rows(cur, amap)
    print(f"  合并比赛(matches+fd_matches): {len(rows)} 场")
    feats = compute(rows)
    # 写库
    cur.execute("DROP TABLE IF EXISTS indep_features")
    cur.execute("""
        CREATE TABLE indep_features (
            id INTEGER PRIMARY KEY,
            match_date TEXT, home TEXT, away TEXT, final_result TEXT, league TEXT, src TEXT,
            elo_home REAL, elo_away REAL, elo_diff REAL,
            form_home REAL, form_away REAL, form_diff REAL,
            rest_home INTEGER, rest_away INTEGER, rest_diff INTEGER,
            h2h_home_win REAL, h2h_draw REAL, h2h_away_win REAL,
            league_strength REAL
        )
    """)
    cols = ["match_date","home","away","final_result","league","src","elo_home","elo_away","elo_diff",
            "form_home","form_away","form_diff","rest_home","rest_away","rest_diff",
            "h2h_home_win","h2h_draw","h2h_away_win","league_strength"]
    cur.executemany(
        f"INSERT INTO indep_features ({','.join(cols)}) VALUES ({','.join(['?']*len(cols))})",
        [[f[c] for c in cols] for f in feats])
    conn.commit()
    # 统计
    print(f"  写入 indep_features: {len(feats)} 行")
    dist = cur.execute("SELECT final_result, COUNT(*) FROM indep_features GROUP BY final_result").fetchall()
    print("  结果分布:", dist)
    date_range = cur.execute("SELECT MIN(match_date), MAX(match_date) FROM indep_features").fetchone()
    print("  日期范围:", date_range)
    conn.close()
    print("✅ 独立特征构建完成")


if __name__ == "__main__":
    main()
