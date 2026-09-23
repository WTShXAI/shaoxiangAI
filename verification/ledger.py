"""验证账本 — append-only SQLite (T02).

职责:
  - 建立并维护 verification.db 的 verification_ledger 表 (仅追加)
  - 提供取数 (fetch_credible / count_credible) 与导出 (export_ledger)
  - ingest_new 委托 verification.ingest 完成 "取数→结算→去水→写账本"

绝不修改既有行: 所有写入走 INSERT; append 预检 (match_id, model_source) 去重。
结算 / 去水严格复用 SSoT (pipeline.settle / pipeline.odds_math), 不在此重写。
"""
from __future__ import annotations

import csv
import json
import sqlite3
from typing import Dict, List, Optional

from verification.schema import LEDGER_DDL, LEDGER_MIGRATIONS, VERIFICATION_DB


class VerificationLedger:
    """append-only 验证账本."""

    def __init__(self, db_path: str = VERIFICATION_DB) -> None:
        self.db_path = db_path

    # ── 连接 / schema ──────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        return con

    def ensure_schema(self) -> None:
        """幂等建表 + 索引 + 存量列迁移."""
        with self._connect() as con:
            con.executescript(LEDGER_DDL)
            for ddl in LEDGER_MIGRATIONS:
                try:
                    con.execute(ddl)
                except sqlite3.OperationalError:
                    pass  # 列已存在, 幂等跳过

    # ── 写入 (仅 INSERT) ──────────────────────────────────────────────────
    def append(self, rec: dict) -> int:
        """仅 INSERT 一条记录; 若 (match_id, model_source) 已存在则跳过并返回 0。
        返回新行 row_id (>0) 或 0 (去重命中).
        """
        with self._connect() as con:
            exists = con.execute(
                "SELECT 1 FROM verification_ledger WHERE match_id=? AND model_source=?",
                (rec["match_id"], rec["model_source"]),
            ).fetchone()
            if exists:
                return 0
            cur = con.execute(
                """
                INSERT INTO verification_ledger
                (run_id, match_id, model_source, kickoff_utc, match_date, chosen_outcome,
                 predicted_prob, p_home, p_draw, p_away, chosen_dec_odds,
                 devig_h, devig_d, devig_a, paper_stake, settled_home, settled_away,
                 settled_outcome, payoff, is_credible, created_at, devig_method)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    rec.get("run_id", ""),
                    rec["match_id"],
                    rec["model_source"],
                    rec.get("kickoff_utc", ""),
                    rec.get("match_date", ""),
                    rec["chosen_outcome"],
                    rec.get("predicted_prob"),
                    rec.get("p_home"),
                    rec.get("p_draw"),
                    rec.get("p_away"),
                    rec["chosen_dec_odds"],
                    rec.get("devig_h"),
                    rec.get("devig_d"),
                    rec.get("devig_a"),
                    rec.get("paper_stake", 1.0),
                    rec.get("settled_home"),
                    rec.get("settled_away"),
                    rec.get("settled_outcome"),
                    rec.get("payoff"),
                    rec.get("is_credible", 1),
                    rec.get("created_at", ""),
                    rec.get("devig_method"),
                ),
            )
            return int(cur.lastrowid)

    def ingest_new(self, con, run_id: str) -> int:
        """委托 ingest.ingest_all 完成本轮取数→结算→去水→写账本.
        con 为 events.db 只读连接 (由调用方打开).
        """
        from verification.ingest import ingest_all

        return ingest_all(con, self, run_id)

    # ── 取数 ──────────────────────────────────────────────────────────────
    def fetch_credible(self, model_source: str, latest_per_match: bool = True) -> List[dict]:
        """返回该 source 下 is_credible=1 的全部账本行 (dict 列表).
        latest_per_match=True 时按 (match_id) 取最大 row_id 一行, 防重.
        """
        with self._connect() as con:
            if latest_per_match:
                rows = con.execute(
                    """
                    SELECT vl.* FROM verification_ledger vl
                    JOIN (
                        SELECT match_id, model_source, MAX(row_id) AS mr
                        FROM verification_ledger
                        WHERE model_source=? AND is_credible=1
                        GROUP BY match_id
                    ) t ON t.mr = vl.row_id
                    ORDER BY vl.row_id
                    """,
                    (model_source,),
                ).fetchall()
            else:
                rows = con.execute(
                    """
                    SELECT * FROM verification_ledger
                    WHERE model_source=? AND is_credible=1
                    ORDER BY row_id
                    """,
                    (model_source,),
                ).fetchall()
            return [dict(r) for r in rows]

    def count_credible(self, model_source: str) -> int:
        """该 source 下 is_credible=1 的行数 (用于进度/验收线)."""
        with self._connect() as con:
            row = con.execute(
                "SELECT COUNT(*) FROM verification_ledger WHERE model_source=? AND is_credible=1",
                (model_source,),
            ).fetchone()
            return int(row[0])

    # ── 导出 (供二次分析) ──────────────────────────────────────────────────
    def export_ledger(self, fmt: str, path: str) -> str:
        """导出账本为 csv 或 json. 返回写出路径."""
        with self._connect() as con:
            cur = con.execute("SELECT * FROM verification_ledger ORDER BY row_id")
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        if fmt == "json":
            with open(path, "w", encoding="utf-8") as f:
                json.dump([dict(r) for r in rows], f, ensure_ascii=False, indent=2)
        elif fmt == "csv":
            with open(path, "w", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                w.writerow(cols)
                for r in rows:
                    w.writerow(list(r))
        else:
            raise ValueError(f"未知导出格式: {fmt}")
        return path


__all__ = ["VerificationLedger"]
