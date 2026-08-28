"""
sharp_provider.py — M6 价值信号模型的「真·尖庄共识」数据源 (哨响AI)

目标: 给 pipeline/leyu_value_signal.py 的占位钩子 SHARP_CONSENSUS_PROVIDER
      接上真实多庄赔率, 让 M6 能产出真正的 +EV 信号, 而不是永远 PASS。

数据链路:
    GQ(乐鱼)中文队名
        → team_canonical 别名表解析出中英文候选形态
        → live_odds_raw 快照表 (由 pipeline/collectors/sp_odds_api.py 采集, 含逐庄 h/d/a)
        → Pinnacle 优先去水 / 无 Pinnacle 则多庄去水后平均
        → (ph, pd, pa) 独立概率源

铁律 (不可协商):
    1. 绝不伪造概率。拿不到真实多庄数据 → 返回 None, 由 evaluate() 如实回落 PASS。
    2. 队名来自前端/GQ 用户输入 → SQL 全参数化, 绝不字符串拼接。
    3. 不打印、不记录任何 API Key (SPOddsAPI 自行读 config.ini/.env)。
    4. 主客orientation必须正确。命中反向(swap)记录时, 概率必须跟着对调,
       否则会输出完全相反的下注方向 —— 这是本模块最危险的坑, 已显式处理。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple

# 项目根: 本文件正式落位为 pipeline/sharp_provider.py → parent.parent == 项目根
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ── 联赛 → The Odds API sport_key 映射 (仅用于 live_refresh 尽力而为刷新) ──
# 覆盖 leyu_value_signal.LEAGUE_WHITELIST 里的五大联赛标识 + 中文名
LEAGUE_SPORT_KEY: Dict[str, str] = {
    # leyu 白名单英文标识
    "premier_league": "soccer_epl",
    "bundesliga": "soccer_germany_bundesliga",
    "la_liga": "soccer_spain_la_liga",
    "serie_a": "soccer_italy_serie_a",
    "ligue_1": "soccer_france_ligue_one",
    # 国家名兜底 (leyu 白名单里也收了这些写法)
    "england": "soccer_epl",
    "germany": "soccer_germany_bundesliga",
    "spain": "soccer_spain_la_liga",
    "italy": "soccer_italy_serie_a",
    "france": "soccer_france_ligue_one",
    # 中文联赛名
    "英超": "soccer_epl",
    "德甲": "soccer_germany_bundesliga",
    "西甲": "soccer_spain_la_liga",
    "意甲": "soccer_italy_serie_a",
    "法甲": "soccer_france_ligue_one",
}

# 单庄赔率合法性校验: Σ(1/赔率) 落在此区间才认为是正经 1X2 盘 (防让球/变盘线混入)
_MIN_INV_SUM = 1.0
_MAX_INV_SUM = 1.35

# 单边队名候选形态上限 (防某队别名过多把 SQL IN 列表撑爆)
_MAX_FORMS_PER_SIDE = 12


def _devig(h: float, d: float, a: float) -> Optional[Tuple[float, float, float]]:
    """单庄去水归一化: inv=(1/h,1/d,1/a); s=Σinv; 返回 (inv/s)。

    非法赔率(<=1.0 / 非数字 / Σinv 越界) → 返回 None, 由调用方跳过该庄。
    """
    try:
        h, d, a = float(h), float(d), float(a)
    except (TypeError, ValueError):
        return None
    if h <= 1.0 or d <= 1.0 or a <= 1.0:
        return None
    inv = (1.0 / h, 1.0 / d, 1.0 / a)
    s = sum(inv)
    # Σinv<=1 意味着无抽水甚至负抽水(数据错误); 过大意味着不是标准1X2盘
    if not (_MIN_INV_SUM < s < _MAX_INV_SUM):
        return None
    return (inv[0] / s, inv[1] / s, inv[2] / s)


class LiveOddsSharpProvider:
    """从本地 live_odds_raw 快照产出尖庄共识概率的 provider。

    用法:
        prov = build_sharp_provider()
        probs = prov("富勒姆", "切尔西", "premier_league")   # → (ph, pd, pa) 或 None
        print(prov.last_consensus_method, prov.last_book_count)
    """

    def __init__(
        self,
        db_path: str = "data/football_data.db",
        max_age_hours: float = 720.0,
        preferred: str = "pinnacle",
        live_refresh: bool = True,
        allow_swap: bool = True,
        cache_ttl_sec: float = 300.0,
    ):
        """
        参数:
            db_path        统一库路径。相对路径会依次尝试 cwd 与 PROJECT_ROOT。
            max_age_hours  快照最大可用时龄(小时)。超龄视为未命中。
            preferred      首选尖庄 key (The Odds API 的 bookmaker key)。
            live_refresh   本地未命中/超龄时, 是否调 The Odds API 现拉一次刷新。
            allow_swap     是否允许命中主客反向的快照(命中后概率会自动对调)。
            cache_ttl_sec  快照查询结果的进程内缓存时长。0=关闭。
                           注意: 缓存的是「数据库行」, 新鲜度仍每次用 captured_at
                           重算, 因此不会因缓存而放行超龄数据。
        """
        self.db_path = self._resolve_db(db_path)
        self.max_age_hours = float(max_age_hours)
        self.preferred = (preferred or "pinnacle").lower().strip()
        self.live_refresh = bool(live_refresh)
        self.allow_swap = bool(allow_swap)

        # 查询缓存: live_odds_raw 已 3.5w 行且 WHERE 用了 LOWER()/TRIM() 无法走索引,
        # 单次查询约 60ms。批量扫盘(几百场)时不缓存会拖垮 FastAPI 事件循环。
        self.cache_ttl_sec = float(cache_ttl_sec)
        self._cache_lock = threading.Lock()
        self._cache: Dict[Tuple[str, str], Tuple[float, Optional[dict]]] = {}

        # 别名表缓存 (进程内只读一次; 610 行量级, 内存可忽略)
        self._alias_lock = threading.Lock()
        self._alias_index: Optional[Dict[str, Set[str]]] = None

        # 每次调用的透明度元数据。用 thread-local 存, 避免多线程互踩;
        # 同时镜像到实例属性, 兼容单线程直读 prov.last_consensus_method 的写法。
        self._tls = threading.local()
        self.last_consensus_method: Optional[str] = None
        self.last_book_count: int = 0
        self.last_orientation: Optional[str] = None
        self.last_age_hours: Optional[float] = None
        self.last_captured_at: Optional[str] = None

    # ────────────────────────── 初始化辅助 ──────────────────────────
    @staticmethod
    def _resolve_db(db_path: str) -> str:
        """把相对 db 路径解析成实际存在的绝对路径 (支持 SHAOXIANG_DB 环境变量覆盖)。"""
        env_db = os.getenv("SHAOXIANG_DB")
        if env_db and Path(env_db).exists():
            return str(Path(env_db).resolve())
        p = Path(db_path)
        if p.is_absolute():
            return str(p)
        for base in (Path.cwd(), PROJECT_ROOT):
            cand = base / p
            if cand.exists():
                return str(cand.resolve())
        # 都不存在: 原样返回, 由 build_sharp_provider 统一抛错
        return str(p)

    # ────────────────────────── 队名解析 ──────────────────────────
    def _load_alias_index(self) -> Dict[str, Set[str]]:
        """读 team_canonical, 建 {别名小写: {该队所有形态小写}} 索引。

        复用 daily_collector._cn_candidates 的同款数据源(canonical + aliases_json),
        但这里做成双向索引并进程内缓存, 避免每次调用全表扫描 610 行。
        """
        if self._alias_index is not None:
            return self._alias_index
        with self._alias_lock:
            if self._alias_index is not None:  # 双检锁
                return self._alias_index
            index: Dict[str, Set[str]] = {}
            try:
                conn = sqlite3.connect(self.db_path)
                cur = conn.cursor()
                cur.execute("SELECT canonical, aliases_json FROM team_canonical")
                rows = cur.fetchall()
                conn.close()
            except Exception:
                rows = []
            for canon, aj in rows:
                forms: Set[str] = set()
                if canon:
                    forms.add(str(canon).lower().strip())
                try:
                    aliases = json.loads(aj) if aj else []
                except (json.JSONDecodeError, TypeError):
                    aliases = []
                for al in aliases:
                    s = str(al).lower().strip()
                    if s:
                        forms.add(s)
                forms.discard("")
                for f in forms:
                    # 一个别名可能被多队共用(极少), 用并集保守处理
                    index.setdefault(f, set()).update(forms)
            self._alias_index = index
            return index

    def _name_forms(self, name: str) -> List[str]:
        """把单个队名展开成所有已知形态(中文名/英文名/缩写), 全部小写去重。

        查不到别名 → 原样返回 [name.lower()], 满足「查不到就原样」的要求。
        """
        base = (name or "").lower().strip()
        if not base:
            return []
        index = self._load_alias_index()
        forms = set(index.get(base, set()))
        forms.add(base)
        # 稳定排序 + 截断, 保证 SQL 参数量可控且结果可复现
        return sorted(forms)[:_MAX_FORMS_PER_SIDE]

    # ────────────────────────── 本地快照查询 ──────────────────────────
    def _cache_get(self, key):
        """读缓存; 未命中或过期返回哨兵 False (None 是合法的「查无此场」结果)。"""
        if self.cache_ttl_sec <= 0:
            return False
        import time as _t
        with self._cache_lock:
            hit = self._cache.get(key)
            if hit and (_t.time() - hit[0]) < self.cache_ttl_sec:
                return hit[1]
        return False

    def _cache_put(self, key, value):
        if self.cache_ttl_sec <= 0:
            return
        import time as _t
        with self._cache_lock:
            if len(self._cache) > 2000:      # 简单容量上限, 防长跑进程无限增长
                self._cache.clear()
            self._cache[key] = (_t.time(), value)

    def invalidate_cache(self):
        """清空查询缓存 (live_refresh 落库后必须调, 否则会读到刷新前的旧行)。"""
        with self._cache_lock:
            self._cache.clear()

    def _query_snapshot(self, home_forms: Sequence[str], away_forms: Sequence[str]) -> Optional[dict]:
        """按候选形态查最新一条快照。

        SQL 安全: f-string 里只拼入由 len() 生成的 '?' 占位符, 队名一律走参数绑定。
        """
        if not home_forms or not away_forms:
            return None

        ck = ("|".join(home_forms), "|".join(away_forms))
        cached = self._cache_get(ck)
        if cached is not False:
            return cached
        ph_h = ",".join(["?"] * len(home_forms))
        ph_a = ",".join(["?"] * len(away_forms))

        # 正向: DB 的 home 命中我们的 home;  反向(swap): DB 的 home 命中我们的 away
        forward = (f"((LOWER(TRIM(home_team)) IN ({ph_h}) OR LOWER(TRIM(home_team_en)) IN ({ph_h}))"
                   f" AND (LOWER(TRIM(away_team)) IN ({ph_a}) OR LOWER(TRIM(away_team_en)) IN ({ph_a})))")
        backward = (f"((LOWER(TRIM(home_team)) IN ({ph_a}) OR LOWER(TRIM(home_team_en)) IN ({ph_a}))"
                    f" AND (LOWER(TRIM(away_team)) IN ({ph_h}) OR LOWER(TRIM(away_team_en)) IN ({ph_h})))")

        hf, af = list(home_forms), list(away_forms)
        if self.allow_swap:
            where = f"({forward} OR {backward})"
            params = hf + hf + af + af + af + af + hf + hf
        else:
            where = forward
            params = hf + hf + af + af

        sql = (
            "SELECT home_team, away_team, home_team_en, away_team_en, "
            "       bookmakers_detail, captured_at, sport_key "
            "FROM live_odds_raw "
            "WHERE bookmakers_detail IS NOT NULL "
            "  AND TRIM(bookmakers_detail) NOT IN ('', 'null', '[]') "
            f"  AND {where} "
            "ORDER BY captured_at DESC LIMIT 1"
        )
        try:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute(sql, params)
            row = cur.fetchone()
            conn.close()
        except Exception:
            return None   # 查询异常不写缓存, 下次重试

        snap = None if not row else {
            "home_team": row[0], "away_team": row[1],
            "home_team_en": row[2], "away_team_en": row[3],
            "bookmakers_detail": row[4], "captured_at": row[5],
            "sport_key": row[6],
        }
        self._cache_put(ck, snap)
        return snap

    @staticmethod
    def _age_hours(captured_at: str) -> Optional[float]:
        """计算快照时龄(小时)。captured_at 为带时区的 ISO 字符串。"""
        if not captured_at:
            return None
        try:
            dt = datetime.fromisoformat(str(captured_at).strip())
        except ValueError:
            return None
        if dt.tzinfo is None:  # 无时区信息 → 按东八区解释
            dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
        now = datetime.now(dt.tzinfo)
        return (now - dt).total_seconds() / 3600.0

    def _orientation(self, snap: dict, home_forms: Sequence[str]) -> str:
        """判断快照的主客方向是否与请求一致。

        返回 'forward' (一致) 或 'swapped' (DB 主队其实是我们的客队)。
        """
        hset = set(home_forms)
        db_home = {str(snap.get("home_team") or "").lower().strip(),
                   str(snap.get("home_team_en") or "").lower().strip()}
        return "forward" if (db_home & hset) else "swapped"

    # ────────────────────────── 共识计算 ──────────────────────────
    def _consensus(self, detail_json: str) -> Optional[Tuple[Tuple[float, float, float], str, int]]:
        """从 bookmakers_detail JSON 算尖庄共识。

        返回 ((ph,pd,pa), consensus_method, book_count); 无可用庄 → None。
          - preferred(pinnacle) 在场 → 只用它去水, method='pinnacle'
          - 否则所有合法庄各自去水后按概率平均, method='multibook_consensus'
        """
        try:
            books = json.loads(detail_json) if detail_json else []
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(books, list) or not books:
            return None

        valid: List[Tuple[str, Tuple[float, float, float]]] = []
        for b in books:
            if not isinstance(b, dict):
                continue
            p = _devig(b.get("h"), b.get("d"), b.get("a"))
            if p is None:
                continue
            valid.append((str(b.get("name") or "").lower().strip(), p))
        if not valid:
            return None

        # 首选尖庄单独成共识 (Pinnacle 定价最锐, 优于把软庄平均进来稀释)
        for name, p in valid:
            if name == self.preferred:
                return p, self.preferred, len(valid)

        # 无首选庄 → 多庄去水概率算术平均, 再归一化消除浮点漂移
        n = len(valid)
        ph = sum(p[0] for _, p in valid) / n
        pd = sum(p[1] for _, p in valid) / n
        pa = sum(p[2] for _, p in valid) / n
        s = ph + pd + pa
        return (ph / s, pd / s, pa / s), "multibook_consensus", n

    # ────────────────────────── live 刷新 ──────────────────────────
    @staticmethod
    def _sport_key_for(league: str) -> Optional[str]:
        """联赛标识 → The Odds API sport_key (白名单校验, 映射不到返回 None)。

        安全: 严禁对任意 soccer_* 字符串放行, 否则未认证请求可借 live_refresh
        烧光 API 月配额(同域 SSRF 变种亦一并消除)。仅当 league 命中已知白名单
        (LEAGUE_SPORT_KEY 键/值 或 SPORT_KEY_MAP 的 key/中文名) 才返回。
        """
        lg = (league or "").lower().strip()
        if not lg:
            return None
        if lg in LEAGUE_SPORT_KEY:
            return LEAGUE_SPORT_KEY[lg]
        # 合法 sport_key / 中文名 白名单 (延迟导入, 失败则仅走 LEAGUE_SPORT_KEY)
        try:
            from pipeline.collectors.sp_odds_api import SPORT_KEY_MAP
        except Exception:
            return None
        if lg in SPORT_KEY_MAP.values():   # 直接给了合法中文 sport_key 名
            return lg
        for k, zh in SPORT_KEY_MAP.items():
            if (league or "").strip() == zh or lg == k.lower():
                return k
        return None

    def _try_live_refresh(self, league: str) -> bool:
        """按联赛拉一次实时赔率并落库。成功返回 True。

        SPOddsAPI 自带 api_budget 预算护栏 + 缓存, 此处不额外限速。
        任何异常一律吞掉 (返回 False), 绝不让刷新失败冒泡到打分链路。
        """
        sport_key = self._sport_key_for(league)
        if not sport_key:
            return False
        try:
            from pipeline.collectors.sp_odds_api import SPOddsAPI
            api = SPOddsAPI()                 # 内部自读 config.ini/.env, 此处不接触 key
            matches = api.get_odds(sport_key) or []
            for m in matches:
                api.save_to_db(m)
            return bool(matches)
        except Exception:
            # 网络/配额/密钥缺失 → 静默降级, 不打印任何可能含密钥的信息
            return False

    # ────────────────────────── 主入口 ──────────────────────────
    def __call__(self, home: str, away: str, league: str = "") -> Optional[Tuple[float, float, float]]:
        """返回该场比赛的尖庄共识概率 (ph, pd, pa); 无真实多庄数据 → None。"""
        self._reset_meta()

        home_forms = self._name_forms(home)
        away_forms = self._name_forms(away)
        if not home_forms or not away_forms:
            return None

        snap = self._query_snapshot(home_forms, away_forms)
        age = self._age_hours(snap["captured_at"]) if snap else None
        fresh = snap is not None and age is not None and age <= self.max_age_hours

        # 本地未命中 / 超龄 → 尽力而为拉一次实时刷新, 再查一遍
        if not fresh and self.live_refresh:
            if self._try_live_refresh(league):
                self.invalidate_cache()   # 刚落了新快照, 必须让缓存失效再查
                snap = self._query_snapshot(home_forms, away_forms)
                age = self._age_hours(snap["captured_at"]) if snap else None
                fresh = snap is not None and age is not None and age <= self.max_age_hours

        if not fresh:
            return None  # 诚实回落: 没有新鲜的真实多庄数据, 绝不编

        cons = self._consensus(snap["bookmakers_detail"])
        if cons is None:
            return None
        (ph, pd, pa), method, n_books = cons

        # ⚠ 关键: 命中反向快照时, 主客概率必须对调, 否则下注方向会完全相反
        orientation = self._orientation(snap, home_forms)
        if orientation == "swapped":
            ph, pa = pa, ph
            method = f"{method}(swapped)"

        self._set_meta(method, n_books, orientation, age, snap.get("captured_at"))
        return (ph, pd, pa)

    # ────────────────────────── 透明度元数据 ──────────────────────────
    def _reset_meta(self):
        self._set_meta(None, 0, None, None, None)

    def _set_meta(self, method, n_books, orientation, age, captured_at):
        self._tls.meta = {
            "consensus_method": method, "book_count": n_books,
            "orientation": orientation, "age_hours": age, "captured_at": captured_at,
        }
        # 镜像到实例属性, 兼容 prov.last_consensus_method 直读
        self.last_consensus_method = method
        self.last_book_count = n_books
        self.last_orientation = orientation
        self.last_age_hours = age
        self.last_captured_at = captured_at

    @property
    def last_meta(self) -> dict:
        """本线程最近一次调用的元数据 (供 evaluate 展示透明度)。"""
        return getattr(self._tls, "meta", None) or {
            "consensus_method": self.last_consensus_method,
            "book_count": self.last_book_count,
            "orientation": self.last_orientation,
            "age_hours": self.last_age_hours,
            "captured_at": self.last_captured_at,
        }

    def explain(self, home: str, away: str, league: str = "") -> dict:
        """调试用: 返回概率 + 全部元数据。"""
        probs = self(home, away, league)
        return {"probs": probs, **self.last_meta}


def build_sharp_provider(
    db_path: str = "data/football_data.db",
    max_age_hours: float = 720.0,
    preferred: str = "pinnacle",
    live_refresh: bool = True,
) -> LiveOddsSharpProvider:
    """工厂: 构造 provider。库/表不存在时抛异常, 由调用方 try 住并保持 None。

    环境变量覆盖 (便于运维调参, 无需改代码):
        SHAOXIANG_DB                 统一库路径
        M6_SHARP_MAX_AGE_HOURS       快照最大时龄
        M6_SHARP_LIVE_REFRESH        '0'/'false' 关闭实时刷新(省 API 配额)
        M6_SHARP_PREFERRED_BOOK      首选尖庄 key (默认 pinnacle)
    """
    max_age_hours = float(os.getenv("M6_SHARP_MAX_AGE_HOURS", max_age_hours))
    preferred = os.getenv("M6_SHARP_PREFERRED_BOOK", preferred)
    env_lr = os.getenv("M6_SHARP_LIVE_REFRESH")
    if env_lr is not None:
        live_refresh = env_lr.strip().lower() not in ("0", "false", "no", "off")

    prov = LiveOddsSharpProvider(
        db_path=db_path, max_age_hours=max_age_hours,
        preferred=preferred, live_refresh=live_refresh,
    )
    if not Path(prov.db_path).exists():
        raise FileNotFoundError(f"统一库不存在: {prov.db_path}")

    # 校验必需表存在, 否则没必要注册 provider
    conn = sqlite3.connect(prov.db_path)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('live_odds_raw','team_canonical')"
        )
        found = {r[0] for r in cur.fetchall()}
    finally:
        conn.close()
    missing = {"live_odds_raw", "team_canonical"} - found
    if missing:
        raise RuntimeError(f"统一库缺少必需表: {sorted(missing)}")
    return prov


if __name__ == "__main__":
    # 自测: 需在项目根执行 → python -m pipeline.sharp_provider
    prov = build_sharp_provider(live_refresh=False, max_age_hours=24 * 3650)
    for h, a, lg in [("富勒姆", "切尔西", "premier_league"),
                     ("曼城", "伯恩茅斯", "premier_league"),
                     ("瞎比队A", "瞎比队B", "premier_league")]:
        print(f"{h} vs {a} →", prov.explain(h, a, lg))
