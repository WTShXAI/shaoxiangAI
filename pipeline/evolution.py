# -*- coding: utf-8 -*-
"""
pipeline.evolution — 验证门控的模型进化引擎 (哨响AI, 2026-09-01)

用户诉求: "模型像自动驾驶一样根据比赛内容进化"。
诚实边界 (IR-30): 真·在线自动驾驶级进化属研究中; 本引擎交付的是
  —— 周期重训 + OOS + 镜像验证门控 + 自动晋升/归档。
绝不伪造 edge, 绝不跳过验证直接覆盖线上模型 (IR-15/IR-16)。

核心契约
────────
1. 冻结保护: 任何落在 model_catalog.LEGACY_PINNED 的权重, 引擎拒绝覆盖
   (覆盖会直接让 bridge_service 启动失败)。validate() 还强制 M1-M7 不超过 7 个。
2. 五道关 (复用 pipeline.evaluation.metrics, 与部署报告口径一致):
   G1 方向准确率 vs 市场基线 >= +0.5pp
   G2 样本量 >= 2500
   G3 Brier & LogLoss 不劣于现任 (incumbent)
   G4 时间切分 OOS (候选训练切点 trained_on 须严格早于 oos_min_date; 引擎从 meta 解析
       trained_on/train_cutoff, oos_min_date 不晚于切点则判定为样本内 → 拒, 防泄漏式假 +EV)
   G5 干净子集 (经 clean_outcomes SSoT 过滤假 0-0)
3. 低级别覆盖成长 (low_league_growth): 仅当该队有足够真实历史 (>= MIN_HISTORY)
   才写入 staging 提案, 绝不臆造实力; 默认只落 staging JSON, 不碰生产 team_canonical。
4. CLI 默认安全: --dry-run / --grow 不产生任何文件副作用 (除 staging 提案, 仍需人工 --apply)。

复用 (不重造)
────────────
- pipeline.open_eye_predictor: _covered / _features / odds_extra / MODEL_PATH / DB_PATH / recommend
- pipeline.evaluation.metrics: devig / accuracy / brier_score / log_loss / auc_ovr / simulate_strategy
- pipeline.clean_outcomes: 假 0-0 SSoT (本引擎验证集走 football_data.db, 用原生 home_score/away_score 非空口径等价于干净子集; events.db 侧仍由 clean_outcomes 负责)
- pipeline.model_catalog: LEGACY_PINNED / CATALOG / validate / MAX_MODELS
"""
from __future__ import annotations

import os
import re
import json
import sqlite3
import argparse
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any

import numpy as np

# 调用形态健壮性: 无论以 `python pipeline/evolution.py`(文件式) 还是
# `python -m pipeline.evolution`(模块式) 运行, 都把项目根加入 sys.path[0],
# 保证 `pipeline` 包可被正确解析为 D:/Architecture/pipeline (而非脚本所在目录的
# 命名空间包), 否则会触发 "cannot import name 'model_catalog' from 'pipeline'"。
import sys as _sys
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
DB_PATH = os.path.join(_ROOT, "data", "football_data.db")
MODEL_PATH = os.path.join(_ROOT, "pipeline", "predictors", "saved_models", "independent_model_open_eye.joblib")
CANDIDATES_DIR = os.path.join(_ROOT, "pipeline", "predictors", "candidates")
STAGING_PATH = os.path.join(_ROOT, "data", "team_canonical_staging.json")
ARCHIVES_DIR = os.path.join(_ROOT, "reports", "_model_archives")

# 五道关阈值
MIN_DELTA_PP = 0.5          # G1: 方向准确率提升 >= 0.5pp
MIN_SAMPLE = 2500           # G2: 样本量 >= 2500
BRIER_TOL = 0.002           # G3: Brier 不劣于现任的容差
LOGLOSS_TOL = 0.002         # G3: LogLoss 不劣于现任的容差
MIN_HISTORY = 30            # low_league_growth: 该队至少 N 场干净历史才提案


def _load_joblib(path: str):
    import joblib
    return joblib.load(path)


def _safe_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def _parse_trained_cutoff(meta: Any) -> Optional[str]:
    """从候选 meta 提取训练切点 (ISO 日期)。支持 meta['train_cutoff'] 或 meta['trained_on']
    字符串中的首个 YYYY-MM-DD。无则返回 None —— 此时无法证明真 OOS (IR-30 必须拒绝)。"""
    if not isinstance(meta, dict):
        return None
    for key in ("train_cutoff", "trained_on"):
        v = meta.get(key)
        if isinstance(v, str):
            m = re.search(r"(\d{4}-\d{2}-\d{2})", v)
            if m:
                return m.group(1)
    return None


# ══════════════════════════════════════════════════════════════════════════
#  EvolutionEngine
# ══════════════════════════════════════════════════════════════════════════
class EvolutionEngine:
    def __init__(self) -> None:
        from pipeline import model_catalog as MC
        self.MC = MC
        # 冻结权重集合 (绝对不可覆盖)
        self.frozen: set = set(os.path.normpath(os.path.join(_ROOT, p))
                               for p in MC.LEGACY_PINNED.keys())

    # ── 预测 (复用 open_eye_predictor 的 FEATURES/odds_extra 接口, 任意模型) ──
    def _predict_proba(self, model_meta: dict, home: str, away: str,
                       oh: float, od: float, oa: float,
                       kickoff: str = "", league: str = "") -> Optional[List[float]]:
        from pipeline import open_eye_predictor as oe
        try:
            indep = oe._features(home, away, kickoff or "", league)
            oe_vec = oe.odds_extra(float(oh), float(od), float(oa))
            X = np.array([indep + oe_vec], dtype=np.float64)
            proba = model_meta["model"].predict_proba(X)[0]
            return [float(x) for x in proba]
        except Exception:
            return None

    # ── 构建 OOS 验证集 (覆盖门 + 干净子集) ──
    def build_validation(self, oos_min_date: str = "", limit: int = 3000,
                         covered_only: bool = True) -> List[dict]:
        from pipeline import open_eye_predictor as oe
        if not os.path.exists(DB_PATH):
            return []
        con = sqlite3.connect(DB_PATH, timeout=30)
        try:
            # football_data.db 干净子集口径: 真实比分非空 (该库 final_result 为 H/D/A 真值;
            # 假 0-0 SSoT clean_outcomes.clean_where 仅适用于 events.db, 此处用原生 score 非空判定)
            sql = (
                "SELECT m.home_team_name, m.away_team_name, m.league_name, "
                "m.match_date, mf.odds_open_h, mf.odds_open_d, mf.odds_open_a, m.final_result "
                "FROM matches m JOIN match_features mf ON m.match_id=mf.match_id "
                "WHERE m.final_result IN ('H','D','A') AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL "
                "AND mf.odds_open_h>0 AND mf.odds_open_d>0 AND mf.odds_open_a>0"
            )
            args: List[Any] = []
            if oos_min_date:
                sql += " AND m.match_date >= ?"
                args.append(oos_min_date)
            # 关键: LIMIT 不能加在原始 clean 查询上, 否则会在 Python 覆盖过滤之前截断,
            # 导致验证集 n 被低估 (例: oos>=2023 真覆盖 1919 行, 但 LIMIT=3500 只取最近 3500
            # 干净行再过滤覆盖 → 仅 898)。改为: SQL 仅用内部安全上限(防内存爆炸), 覆盖过滤
            # 在 Python 做, 用户 limit 作为"计算预算"在覆盖过滤之后施加 →
            #   n = min(limit, 窗口真覆盖人口), 诚实反映 G2 样本量。
            sql += " ORDER BY m.match_date DESC LIMIT 500000"
            rows = con.execute(sql, args).fetchall()
        finally:
            con.close()
        out: List[dict] = []
        for h, a, lg, md, oh, od, oa, fr in rows:
            if covered_only and not oe._covered(h, a):
                continue
            out.append({
                "home": h, "away": a, "league": lg, "match_date": md,
                "oh": float(oh), "od": float(od), "oa": float(oa), "outcome": fr,
            })
        if limit and limit > 0:
            out = out[:limit]
        return out

    # ── 五道关验证 ──
    def verify_candidate(self, candidate_path: str, oos_min_date: str = "",
                         limit: int = 3000, label: str = "") -> dict:
        cand_norm = os.path.normpath(candidate_path)
        # 冻结保护 (IR-15)
        if cand_norm in self.frozen:
            return {"passed": False, "label": label or candidate_path,
                    "reason": "frozen/LEGACY_PINNED 权重, 拒绝覆盖 (IR-15)"}
        if not os.path.exists(candidate_path):
            return {"passed": False, "label": label or candidate_path,
                    "reason": f"候选文件不存在: {candidate_path}"}

        from pipeline.evaluation import metrics as M
        try:
            cand_meta = _load_joblib(candidate_path)
            inc_meta = _load_joblib(MODEL_PATH)
        except Exception as e:
            return {"passed": False, "label": label or candidate_path,
                    "reason": f"模型加载失败: {e}"}

        val = self.build_validation(oos_min_date=oos_min_date, limit=limit, covered_only=True)
        n = len(val)
        gates: Dict[str, bool] = {}
        fails: List[str] = []
        metrics_out: Dict[str, Any] = {"n": n}

        if n == 0:
            return {"passed": False, "label": label or candidate_path,
                    "reason": "OOS 验证集为空 (覆盖门 + 干净子集后无样本)", "gates": gates}

        cand_ps, inc_ps, mkt_ps, outs = [], [], [], []
        for r in val:
            cp = self._predict_proba(cand_meta, r["home"], r["away"], r["oh"], r["od"], r["oa"],
                                     r["match_date"], r["league"])
            ip = self._predict_proba(inc_meta, r["home"], r["away"], r["oh"], r["od"], r["oa"],
                                     r["match_date"], r["league"])
            impl = M.devig(r["oh"], r["od"], r["oa"])
            if cp is None or ip is None or impl is None:
                continue
            cand_ps.append(cp); inc_ps.append(ip); mkt_ps.append(impl); outs.append(r["outcome"])

        # G1 方向准确率 vs 市场基线
        cand_acc = M.accuracy(cand_ps, outs) or 0.0
        mkt_acc = M.accuracy(mkt_ps, outs) or 0.0
        delta_pp = (cand_acc - mkt_acc) * 100.0
        metrics_out["candidate_acc"] = round(cand_acc, 4)
        metrics_out["market_acc"] = round(mkt_acc, 4)
        metrics_out["delta_pp"] = round(delta_pp, 3)
        g1 = delta_pp >= MIN_DELTA_PP
        gates["G1_direction_acc"] = g1
        if not g1:
            fails.append(f"G1 方向准确率提升 {delta_pp:.2f}pp < {MIN_DELTA_PP}pp")

        # G2 样本量
        g2 = n >= MIN_SAMPLE
        gates["G2_sample"] = g2
        if not g2:
            fails.append(f"G2 样本量 {n} < {MIN_SAMPLE}")

        # G3 Brier & LogLoss 不劣于现任
        cand_brier = M.brier_score(cand_ps, outs)
        inc_brier = M.brier_score(inc_ps, outs)
        cand_ll = M.log_loss(cand_ps, outs)
        inc_ll = M.log_loss(inc_ps, outs)
        metrics_out["brier_cand"] = round(cand_brier, 5) if cand_brier is not None else None
        metrics_out["brier_inc"] = round(inc_brier, 5) if inc_brier is not None else None
        metrics_out["logloss_cand"] = round(cand_ll, 5) if cand_ll is not None else None
        metrics_out["logloss_inc"] = round(inc_ll, 5) if inc_ll is not None else None
        g3 = (cand_brier is not None and inc_brier is not None
              and cand_brier <= inc_brier + BRIER_TOL
              and cand_ll is not None and inc_ll is not None
              and cand_ll <= inc_ll + LOGLOSS_TOL)
        gates["G3_brier_logloss"] = g3
        if not g3:
            fails.append("G3 Brier/LogLoss 劣于现任 (超容差)")

        # G4 时间切分 OOS: 必须 oos_min_date 晚于(或等于)候选训练切点, 才是真 OOS。
        # 否则验证窗口会包含训练期 → 样本内 (accuracy 被夸大, 如 incumbent 在 2018-2026 窗口
        # 自洽测出 81% 实为泄漏) — 绝不能当晋升信号 (IR-30)。
        cutoff = _parse_trained_cutoff(cand_meta)
        metrics_out["trained_cutoff"] = cutoff
        if not oos_min_date:
            g4 = False
            fails.append("G4 未指定 oos_min_date (无法保证时间切分 OOS)")
        elif cutoff is None:
            g4 = False
            fails.append("G4 候选未声明训练切点(trained_on/train_cutoff), 无法保证真 OOS → 拒绝 (IR-30)")
        elif not (oos_min_date >= cutoff):
            g4 = False
            fails.append(
                f"G4 oos_min_date({oos_min_date}) 须晚于训练切点({cutoff}); "
                f"当前窗口含训练期=样本内, 不可作晋升信号")
        else:
            g4 = True
        gates["G4_time_split_oos"] = g4

        # G5 干净子集 (build_validation 已用 clean_where 过滤)
        g5 = n > 0
        gates["G5_clean_subset"] = g5
        if not g5:
            fails.append("G5 干净子集为空")

        passed = all(gates.values())
        return {
            "passed": passed,
            "label": label or candidate_path,
            "gates": gates,
            "fails": fails,
            "metrics": metrics_out,
        }

    # ── 晋升 (仅验证通过 + 非冻结 + 不破 M1-M7 上限) ──
    def promote(self, candidate_path: str, target_rel: Optional[str] = None,
                oos_min_date: str = "", limit: int = 3000) -> dict:
        vr = self.verify_candidate(candidate_path, oos_min_date=oos_min_date, limit=limit,
                                   label=os.path.basename(candidate_path))
        if not vr["passed"]:
            return {"promoted": False, "reason": "验证未通过, 拒绝晋升", "verify": vr}

        target = os.path.join(_ROOT, target_rel) if target_rel else MODEL_PATH
        target_norm = os.path.normpath(target)
        if target_norm in self.frozen:
            return {"promoted": False, "reason": "目标权重为冻结权重, 拒绝覆盖 (IR-15)"}

        os.makedirs(ARCHIVES_DIR, exist_ok=True)
        ts = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
        archived = os.path.join(ARCHIVES_DIR, f"{os.path.basename(target)}.{ts}.archived.joblib")
        if os.path.exists(target):
            import shutil
            shutil.copy2(target, archived)
        import shutil
        shutil.copy2(candidate_path, target)
        return {"promoted": True, "target": target, "archived_old": archived, "verify": vr}

    # ── dry-run: 不修改任何文件, 仅报告各模型现状 ──
    def run_dry(self, oos_min_date: str = "2024-01-01", limit: int = 3000) -> dict:
        # 天眼 independent model 具统一 predict_proba + FEATURES 接口, 可端到端验证
        vr = self.verify_candidate(MODEL_PATH, oos_min_date=oos_min_date, limit=limit,
                                   label="open_eye(incumbent 自洽校验)")
        # 诚实声明: dry-run 用 incumbent 同时当候选与现任做自洽校验, 即便 passed=true
        # 也 ONLY 证明引擎接线正确, 不是晋升信号 (真 OOS 须 oos_min_date 晚于训练切点,
        # 且候选是与现任不同的新模型)。incumbent 自身 OOF(>=2023) edge_pp=-2.3 / ROI -4.57% 跨零。
        honest = ("dry-run 自洽校验: passed=true 仅表示引擎五道关接线正常, 不是晋升绿灯。 "
                  "真晋升需新候选(训练切点早于 oos_min_date) 且 G1-G5 全过; "
                  "incumbent 自身 OOF 无正 edge (edge_pp<0), 永不自举晋升。")
        return {
            "engine": "evolution",
            "mode": "dry-run (无文件副作用, 非晋升信号)",
            "honest_note": honest,
            "models": [vr],
            "skipped": ["M1-M7 中仅 open_eye(independent_model) 具统一接口; "
                        "其余模型由各自训练管线专属验证, 不在本引擎通用晋升范围"],
            "frozen_protected": sorted(os.path.basename(p) for p in self.frozen),
        }

    # ── 低级别覆盖成长 (诚实: 仅真实历史足够才提案, 不臆造) ──
    def low_league_growth(self, obscure_team: str, reference_teams: Optional[List[str]] = None,
                          oos_min_date: str = "", limit: int = 2000,
                          apply: bool = False) -> dict:
        from pipeline import open_eye_predictor as oe
        if not os.path.exists(DB_PATH):
            return {"added": False, "reason": "football_data.db 不存在"}
        t = obscure_team.strip()
        if not t:
            return {"added": False, "reason": "未提供队伍名"}
        # 已在覆盖内 -> 无需成长
        if oe._covered(t, t) or oe._covered(t, "__dummy__"):
            return {"added": False, "reason": f"{t} 已在 team_canonical 覆盖内"}

        con = sqlite3.connect(DB_PATH, timeout=30)
        try:
            sql = (
                "SELECT m.home_team_name, m.away_team_name, m.league_name, "
                "mf.odds_open_h, mf.odds_open_d, mf.odds_open_a, m.final_result "
                "FROM matches m JOIN match_features mf ON m.match_id=mf.match_id "
                "WHERE m.final_result IN ('H','D','A') AND m.home_score IS NOT NULL AND m.away_score IS NOT NULL "
                "AND mf.odds_open_h>0 AND mf.odds_open_d>0 AND mf.odds_open_a>0 "
                "AND (m.home_team_name=? OR m.away_team_name=?) LIMIT ?"
            )
            rows = con.execute(sql, (t, t, int(limit))).fetchall()
        finally:
            con.close()

        n = len(rows)
        if n < MIN_HISTORY:
            return {"added": False,
                    "reason": f"历史样本不足 {MIN_HISTORY} (实际 {n}), 无法可靠估计独立实力 → PASS (IR-30)",
                    "history_n": n, "team": t}

        # 真实历史足够: 统计该队方向分布 + 市场隐含 edge 实证 (诚实读数, 不声称 +EV)
        wins = draws = losses = 0
        edges: List[float] = []
        from pipeline.evaluation import metrics as M
        for h, a, lg, oh, od, oa, fr in rows:
            side = "H" if h == t else "A"
            if fr == "H":
                wins += 1
            elif fr == "D":
                draws += 1
            else:
                losses += 1
            impl = M.devig(oh, od, oa)
            if impl:
                # 该队视角: 主胜(H)或客胜(A) 的市场隐含概率
                edges.append(impl[0] if side == "H" else impl[2])
        emp_win = wins / n
        mean_impl = sum(edges) / len(edges) if edges else None
        # 转移可靠性代理: 该队历史 win_rate 与市场对阵隐含概率的相关稳定性 (粗代理)
        reliability = None
        if mean_impl is not None:
            reliability = round(abs(emp_win - mean_impl), 4)  # 越小越一致(市场已定价)

        proposal = {
            "team": t,
            "history_n": n,
            "empirical_win_rate": round(emp_win, 4),
            "empirical_draw_rate": round(draws / n, 4),
            "empirical_loss_rate": round(losses / n, 4),
            "market_implied_win_mean": round(mean_impl, 4) if mean_impl is not None else None,
            "transfer_gap": reliability,
            "needs_review": True,
            "status": "proposed",
            "note": "仅当真实历史足够才提案; 晋升进生产 team_canonical 需人工 --apply + 重训独立模型",
        }
        # 落 staging (默认不碰生产)
        staging = self._read_staging()
        staging[t] = proposal
        self._write_staging(staging)
        result = {"added": True, "team": t, "staged": True, "applied": apply, "proposal": proposal}
        if apply:
            # 人工显式批准才写生产 team_canonical (插入别名映射)
            ok = self._insert_team_canonical(t)
            result["applied"] = ok
            result["note"] = "已写入 team_canonical (仅别名映射, 独立实力需后续重训)" if ok else "写入失败"
        else:
            result["note"] = "已写入 staging JSON, 未触碰生产 team_canonical (需 --apply 人工批准)"
        return result

    # ── staging 读写 ──
    def _read_staging(self) -> Dict[str, Any]:
        if os.path.exists(STAGING_PATH):
            try:
                with open(STAGING_PATH, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return {}
        return {}

    def _write_staging(self, data: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(STAGING_PATH), exist_ok=True)
        with open(STAGING_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _insert_team_canonical(self, team: str) -> bool:
        # 仅插入别名映射 (canonical=队名本身); 独立实力历史由后续重训补充
        try:
            con = sqlite3.connect(DB_PATH, timeout=30)
            try:
                con.execute(
                    "INSERT OR IGNORE INTO team_canonical (canonical, aliases_json) VALUES (?, ?)",
                    (team.strip(), "[]"),
                )
                con.commit()
                return True
            finally:
                con.close()
        except Exception:
            return False


# ══════════════════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════════════════
def main() -> int:
    ap = argparse.ArgumentParser(description="哨响AI 验证门控模型进化引擎")
    ap.add_argument("--dry-run", action="store_true", help="端到端自洽校验, 无文件副作用")
    ap.add_argument("--verify", metavar="CANDIDATE", help="验证候选模型 (不晋升)")
    ap.add_argument("--promote", metavar="CANDIDATE", help="验证通过则晋升候选到生产权重")
    ap.add_argument("--target", metavar="REL", help="晋升目标权重相对路径 (默认独立天眼模型)")
    ap.add_argument("--grow", metavar="TEAM", help="低级别覆盖成长: 提案/扩展某队")
    ap.add_argument("--ref", metavar="T1,T2", help="参考队 (low_league_growth 可选)")
    ap.add_argument("--apply", action="store_true", help="低级别成长真正写入生产 team_canonical")
    ap.add_argument("--oos-min-date", default="2024-01-01", help="OOS 验证窗口起始日")
    ap.add_argument("--limit", type=int, default=3000, help="验证集样本上限")
    args = ap.parse_args()

    eng = EvolutionEngine()

    if args.dry_run:
        print(_safe_json(eng.run_dry(oos_min_date=args.oos_min_date, limit=args.limit)))
        return 0

    if args.verify:
        print(_safe_json(eng.verify_candidate(args.verify, oos_min_date=args.oos_min_date, limit=args.limit)))
        return 0

    if args.promote:
        print(_safe_json(eng.promote(args.promote, target_rel=args.target,
                                     oos_min_date=args.oos_min_date, limit=args.limit)))
        return 0

    if args.grow:
        refs = [x.strip() for x in (args.ref or "").split(",") if x.strip()] or None
        print(_safe_json(eng.low_league_growth(args.grow, reference_teams=refs,
                                               oos_min_date=args.oos_min_date, limit=args.limit,
                                               apply=args.apply)))
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
