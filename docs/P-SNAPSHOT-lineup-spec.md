# P-SNAPSHOT-data: GQ 阵容(lineup)采集补全规格

> 状态: **WINDOW 备料 (仅规格, 不执行)**. 落地须走维护窗口(老板拍板 + 回归),
> 不擅改生产采集层 / 不碰 events.db 生产数据 / 不杀进程.
> 关联: `docs/WINDOW_PREP.md` §1, `docs/AUTONOMOUS_BACKLOG.md` T02.
> 纪律红线: IR-30 诚实 / IR-32 禁区 / §4 数据资产保全 / §6 不杀进程 / 售卖双签.
> 队列编号: **T02** — 自动化 dbda4380 每 10 分钟 pull 一项执行并打勾.

---

## §0 现状与根因 (实测)

采集层 `gq/content_collector.py` 当前**不接阵容**:

| 来源 | 端点 / 入口 | 实测返回 | 结论 |
|------|------------|---------|------|
| 主分析 tab 阵容 | `getMatchAnalysiseDataPB` + `parentMenuId=2, sonMenuId=0` | 多数场次 `0408006`(未公布) | 弱源, 仅赛事临近且 GQ 公布时才有可能有数据, 结构未解析 |
| 阵容专用端点 | `getMatchLineupListPB` / `getPCMatchLineupListPB` | 恒 `0400500`(参数错误 / SPA 会话保护) | **硬阻塞**: 无论传 `standardMatchId` 或 `matchId` 均失败, 需更深逆向 |

数据模型侧**已就绪**(无需迁移):
- `match_meta` 表已有 `lineup_home TEXT` / `lineup_away TEXT` 两列(`gq/db.py:277-278`), 当前全库 0 行。
- `upsert_match_meta` 的 `ALLOWED` 集合已含 `lineup_home` / `lineup_away`(`gq/db.py:440-441`)。
- `match_snapshot` 隔离库 `match_snapshot` 表已有 `lineup TEXT` 列(`scripts/match_snapshot.py:41`), 当前 freeze 未写入(恒 NULL)。

WS 侧 `ws_collector.py` **无任何 lineup 处理** → 阵容只能由 HTTP 内容采集层补齐, 不走实时盘口流。

**诚实结论**: 阵容缺口的根因是 GQ 阵容专用端点受 SPA 会话保护(0400500), 不是代码没接。
补全有"低风险即时"与"需逆向突破"两条路径, 见 §2。

---

## §1 目标与范围

- 目标: 让 `match_meta.lineup_home/lineup_away` 在赛事临近(阵容公布后)被填充, 并纳入
  P-SNAPSHOT-data 不可变冻结(防偷看快照), 使赛前已知态包含"预计首发"。
- 范围(本规格): 仅描述方案 + 代码草案 + 降级 + 回滚 + 验收门禁。**不写生产代码、不运行。**
- 非目标: 不解析赛中换人(阵容锁定于开赛, 赛中变更新增不在本规格)。

---

## §2 补全路径 (两条, 按风险排序)

### Tier A — 低风险即时 (走现有 getMatchAnalysiseDataPB, 无逆向)

复用已验证通过的 `getMatchAnalysiseDataPB` + `sonMenuId=0`(阵容 tab), 不改鉴权方式
(沿用 `auto_collector._build_headers()` 的 checkid/requestid, 不碰 cookie)。

- 新增 `LINEUP_MENU = (2, 0)`。
- 在 `fetch_match_content()` 内追加一次 `_fetch(mid, *LINEUP_MENU)`。
- 关键未知: `sonMenuId=0` 返回的 `basicInfoMap` 中阵容字段名未解析(需抓一场"已公布阵容"的比赛
  实测解码, 确认结构, 例如 `sThirdMatchLineupDTOMap` / `lineupDTOList` 之类)。
- 解码成功后映射到 `lineup_home` / `lineup_away`(按主客队 side 字段区分), 经 `upsert_match_meta` 写入。
- 风险: 多数场次返 `0408006`(未公布) → 该场 lineup 留空, 不影响其他字段。仅对"GQ 已公布阵容"的
  少数场次生效, 覆盖率低但**零逆向风险**。

### Tier B — 需逆向突破 (破 getMatchLineupListPB 0400500)

`getMatchLineupListPB` 恒返 `0400500`, 根因是"参数受 SPA 会话保护"——即 GQ Web 前端在调用该端点时
注入了某个**由 SPA JS 客户端计算/持有的参数或头**(非 `auto_collector._build_headers()` 现有 checkid/requestid 所能覆盖)。

突破子路径(按优先级):
1. **真实浏览器抓包复刻**: 在 GQ Web 打开一场"已公布阵容"的比赛, 用 DevTools/Charles 捕获
   `getMatchLineupListPB` 请求, 逐字段比对与 `getMatchAnalysiseDataPB` 成功的请求差异
   (多出的 header? 不同的 body 参数名? 是否需 `matchId` 而非 `standardMatchId`? 是否需 cookie session?)。
   这是最可靠的路径 —— 直接复刻浏览器发出的完整请求。
2. **SPA JS 逆向**: 在 GQ 前端打包 JS 中定位 `getMatchLineupListPB` 调用点, 提取其参数构造逻辑
   (重点: 是否有签名 token / 时间戳 / 会话派生值)。
3. **getPCMatchLineupListPB 对照**: PC 端同名端点可能参数要求不同, 作为对照试验。

**诚实标注**: Tier B 成功率不确定, 依赖能否复刻浏览器请求。若 3 条子路径均失败, 则阵容补全
**仅能靠 Tier A**(覆盖率低), 并在 WINDOW_PREP 标注"阵容补全 INCONCLUSIVE / 受 GQ 会话保护不可破"。

---

## §3 content_collector 改造草案 (非落地, 供窗口执行参考)

```python
# gq/content_collector.py — 草案, 不执行
LINEUP_MENU = (2, 0)   # parentMenuId=2, sonMenuId=0 → 阵容(tab, 多数返 0408006)

def _parse_lineup(d: dict) -> tuple[str, str]:
    """从 sonMenuId=0 解码的 basicInfoMap 提取主/客阵容 JSON。结构待实测确认。"""
    bim = (d.get("basicInfoMap") or {})
    # TODO(逆向/实测): 确认阵容字段名, 例 sThirdMatchLineupDTOMap / lineupDTOList
    raw = bim.get("__LINEUP_FIELD_TBD__")
    if not raw:
        return "", ""
    # 按 side/home_away 区分主客; 结构确认后补映射
    home = json.dumps(raw.get("home") or raw.get("1") or {}, ensure_ascii=False)
    away = json.dumps(raw.get("away") or raw.get("2") or {}, ensure_ascii=False)
    return home, away

def fetch_match_content(mid) -> dict:
    out = {...}  # 现有 preview/injuries/result/info
    # —— Tier A 阵容 (新增) ——
    dl = _fetch(mid, *LINEUP_MENU)
    if dl:
        home, away = _parse_lineup(dl)
        if home:
            out["lineup_home"] = home; out["ok_count"] += 1
        if away:
            out["lineup_away"] = away; out["ok_count"] += 1
    return out

def collect_and_store(match_key: str, mid) -> bool:
    c = fetch_match_content(mid)
    if c["ok_count"] == 0:
        return False
    upsert_match_meta(match_key, mid=str(mid),
                      preview=c["preview"], injuries_home=c["injuries"],
                      news=c["info"],
                      lineup_home=c.get("lineup_home", ""),
                      lineup_away=c.get("lineup_away", ""))
    ...
```

Tier B 仅在逆向突破后补一个 `_fetch_lineup_pb(mid)` 走 `getMatchLineupListPB`, 复用同一 `_parse_lineup`。

---

## §4 match_meta / match_snapshot 集成点

- `match_meta`: 列已存在 (`lineup_home`/`lineup_away`), `upsert_match_meta` 已支持 → **零 schema 变更**。
- `match_snapshot.freeze()`: 当前仅读 `preview/news/injury_home` (`scripts/match_snapshot.py:109-110`)。
  补全后须扩展:
  ```python
  # 草案: freeze 纳入阵容 (仅当 match_meta 已有值, 不主动采集)
  m = ec.execute("SELECT preview, news, injuries_home, lineup_home, lineup_away "
                 "FROM match_meta WHERE match_key=?", (match_key,)).fetchone()
  ...
  # fields 增加 lineup_home/lineup_away, 纳入 sha256 hash; INSERT 列增加两字段
  ```
  隔离库 `match_snapshot` 表 `lineup TEXT` 列已存在 → 可改为两列 `lineup_home`/`lineup_away` 或保留单
  `lineup` JSON 列(存 `{home,away}`)。**幂等 ALTER 补列 + 默认 dry-run 守卫**沿用 T01 模式。

---

## §5 优雅降级 (必含)

| 返回码 / 状态 | 处理 |
|--------------|------|
| `0408006` (未公布) | 跳过该场阵容, 不写、不覆盖 match_meta 既有值 |
| `0400500` (Tier B 参数错) | 记日志 + 退避, **不重试同场**; 回退 Tier A |
| 解码失败 / 结构空 | 不写, 保留原值 |
| 阵容字段全空 | 不调 `upsert_match_meta` 的 lineup 参数(避免清空) |

原则: 阵容是**稀疏增强字段**, 缺失不得影响 preview/injuries/info 既有采集(现有 `ok_count` 隔离)。

---

## §6 防偷看保证 (anti-peek 不破)

- 阵容于开赛前 ~60–75 min 由 GQ 公布, 属**赛前已知态**, 纳入 P-SNAPSHOT-data 冻结**不破 anti-peek**。
- 冻结时机: 阵容须在**开赛前**已被 content_collector 采集入 match_meta; freeze 在开赛快照点读入即可。
- 严禁: 赛中(开赛后)更新阵容并重新冻结 —— 赛中阵容变更属赛果信息, 一旦落入快照即破 anti-peek。
  → freeze 仅读 match_meta 已存值, **不主动触发采集**, 从源头杜绝赛中污染。

---

## §7 回滚检查单

1. `match_meta.lineup_home/lineup_away` 列已存在 → 回滚**无需 DROP**, 停止采集即留空。
2. `match_snapshot` 隔离库: 若 ALTER 补列, 回滚用 `ALTER TABLE match_snapshot DROP COLUMN`
   (SQLite 3.35+ 支持; 或重建表) —— 隔离库可自由操作, 不影响 events.db。
3. `content_collector.py`: 回滚 = 撤销 `LINEUP_MENU` / `_parse_lineup` / freeze 扩展(git revert 该文件)。
4. 验证: 回滚后 `fetch_match_content` 不返回 lineup 键, `upsert_match_meta` 不再传 lineup。

---

## §8 验收门禁 (落地时)

- 单测 (临时库, 不碰 events.db):
  - `_parse_lineup` 对已知结构返回正确主/客 JSON; 空结构返回 `("", "")`。
  - `0408006` / `0400500` / 解码失败 三态均不写、不抛。
  - `upsert_match_meta` 仅传非空 lineup, 不覆盖既有。
- 集成验证 (只读): 选一场"已公布阵容"的历史比赛, 走 Tier A 解码, 确认 `match_meta.lineup_home/away`
  非空且结构与预期一致; 跑 `pytest` 全绿。
- freeze 验证: 构造含 lineup 的 match_meta, freeze 后 `match_snapshot` 含 lineup 且 hash 随 lineup 变化。
- 纪律: 不写 events.db 生产数据(只读 mode=ro 取源)、不杀进程、不在线 VACUUM。

---

## §9 执行前置 (WINDOW 项, 须老板拍板)

- [ ] 老板批准维护窗口(采集层改动 + 可能 ALTER 隔离库)
- [ ] Tier B 逆向突破成功 OR 明确接受仅 Tier A(低覆盖)
- [ ] 回归 `tests/` 全绿 + 采集层节流不影响 WS 实时流
- [ ] 回滚检查单预演通过
- 未满足任一 → 本规格保持"备料", 不落地。
