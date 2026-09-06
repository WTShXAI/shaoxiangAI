"""验证 残留①: _sweep_finished 经单写者(gq.db.conn) 且 live 翻页仍真实执行.

做法: mock gq.auto_collector.conn (=单写者上下文管理器) → 指向一个临时 matches 库的
真实连接; 同时 mock upsert_match / record_match_outcome 为记录调用; 构造未初始化的
GQCollector 实例并调用 _sweep_finished。断言:
  - 单写者 conn() 被真实进入 (entered>=1)
  - live 僵尸被纠正(upsert_match 真实调用)
  - 赛果归档(record_match_outcome 仅 sc>0 时) 真实调用
  - 全程不触碰真实 events.db
"""
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from unittest import mock

import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gq.auto_collector as ac  # noqa: E402


def _build_temp_db(path: str) -> None:
    c = sqlite3.connect(path)
    c.execute(
        """CREATE TABLE matches (
            match_key TEXT PRIMARY KEY, mid TEXT, home TEXT, away TEXT, league TEXT,
            kickoff TEXT, status TEXT, score_home INTEGER, score_away INTEGER,
            minute INTEGER, last_seen REAL)"""
    )
    # 已实质性结束却被标 live 的僵尸(开赛时间很早): sweep 应纠正为 finished 并归档
    c.execute(
        "INSERT INTO matches VALUES ('m1','m1','H','A','L','2020-01-01 00:00',"
        "'live',2,1,90,1.0)"
    )
    c.commit()
    c.close()


def main() -> None:
    tmp = tempfile.mkdtemp()
    db_path = os.path.join(tmp, "events.db")
    _build_temp_db(db_path)

    entered = []

    @contextmanager
    def _fake_conn(readonly: bool = False):  # 模拟单写者: 真实连临时库
        real = sqlite3.connect(db_path, timeout=30)
        real.row_factory = sqlite3.Row
        entered.append(1)
        try:
            yield real
        finally:
            real.close()

    upsert_calls = []
    record_calls = []

    with mock.patch.object(ac, "conn", _fake_conn), \
         mock.patch.object(ac, "upsert_match",
                           lambda *a, **k: upsert_calls.append(1) or None), \
         mock.patch.object(ac, "record_match_outcome",
                           lambda *a, **k: record_calls.append(1) or None):
        coll = ac.GQCollector.__new__(ac.GQCollector)
        coll.log = lambda m: None
        coll._sweep_scheduled = lambda: None
        coll._sweep_finished()

    assert len(entered) >= 1, "单写者 conn() 未被调用"
    assert len(upsert_calls) >= 1, "live 翻页写(upsert_match) 未真实执行"
    assert len(record_calls) >= 1, "赛果归档(record_match_outcome, sc>0) 未真实执行"

    print(f"PASS residual1: conn entered={len(entered)}, "
          f"upsert={len(upsert_calls)}, record={len(record_calls)}")
    print("RESIDUAL1: _sweep_finished USES SINGLE-WRITER + LIVE FLIP EXECUTED")


if __name__ == "__main__":
    main()
