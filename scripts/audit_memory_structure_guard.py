"""T65 自动化 memory 结构不变式守卫（只读审计 + 不改 memory 内容）。

T60 修掉的是「事后发现有人复制粘贴了重复 header」，但**没有任何守卫防止下一次再犯**：
`parse_history()` 的段语义一旦再被截断，看板 `reports/automation_health.json` 会静默变小，
而 `test_real_memory_file_parseable` 的 `>= 20` 断言要到下一次全量回归才会红。
本脚本把 T60 的教训固化成**结构不变式（S1-S6）**，任何一条被破坏即 FAIL：

  S1 标题唯一       —— 文件里每个 `## ` 标题文本必须只出现一次（D1 的 verbatim 重复 header）
  S2 历史段唯一     —— `count_history_sections(text) == 1`（段落被切成两段时必然 != 1）
  S3 覆盖           —— 全文件所有 `- <date>` bullet 都必须被解析成条目（D1 截断 / D2 丢行的指纹）
  S4 首末轮语义     —— `first_round/last_round` 必须取 (date, 时刻) 极值，不是文件首/末行（D3）
  S5 字段完整       —— 每个条目 date/time/body 非空，且每个 date bullet 都带时刻 token（D2 前置）
  S6 重复条目       —— (date, 时刻) 完全相同的条目数（信息，不判 FAIL，但会静默放大轮次）

S4 同时是**唯一不能用真实文档证明的不变式**：只有当文件首行的条目恰好不是最旧条目时，
「取极值」与「取行序」才会给出不同答案。真实文档恰好满足（首行 = 2026-09-27 22:4x，
最旧 = 2026-09-25 12:3x），故本脚本**记录该判别证据**，真正的 fail-closed 回归
在 `tests/test_audit_memory_structure_guard.py` 用合成文档（最新条目置于文件首行）完成。

用法:
  python scripts/audit_memory_structure_guard.py   # 只读，产出 reports/memory_structure_guard_audit.*

只读面：仅读取 `.workbuddy/memory/automations/.../memory.md`；不打开任何数据库、
不碰 events.db、不写 memory 内容（只写自有报告）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
from datetime import datetime, timezone

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_PATH = os.path.join(
    REPO_ROOT, ".workbuddy", "memory", "automations",
    "dbda4380-1bc4-41b5-9e04-1117d1ba56b9", "memory.md",
)
HEALTH_SCRIPT = os.path.join(REPO_ROOT, "scripts", "snapshot_automation_health.py")
OUT_JSON = os.path.join(REPO_ROOT, "reports", "memory_structure_guard_audit.json")
OUT_MD = os.path.join(REPO_ROOT, "reports", "memory_structure_guard_audit.md")

HISTORY_HEADING = "## 执行历史"
# 任何以 "## 执行历史" 打头的标题都算历史段 opener（含 `## 执行历史（仅高层...）`）
H2_RE = re.compile(r"^\s*##\s+(?P<title>.+?)\s*$")
# 形如 "- 2026-09-25 12:3x ..." 的 bullet（不要求带时刻，时刻由 Q5 单独校验）
DATUM_BULLET_RE = re.compile(r"^\s*-\s*(\d{4}-\d{2}-\d{2})\b(?P<rest>.*)$")
TIME_TOKEN_RE = re.compile(r"\d{1,2}:\d{1,2}")

_HEALTH_SPEC = importlib.util.spec_from_file_location(
    "snapshot_automation_health_t65", HEALTH_SCRIPT)
HEALTH = importlib.util.module_from_spec(_HEALTH_SPEC)
_HEALTH_SPEC.loader.exec_module(HEALTH)


# --------------------------------------------------------------------------- #
# 基础抽取
# --------------------------------------------------------------------------- #
def iter_h2_headings(text: str) -> list[dict]:
    """全部 `## ` 标题（含 lineno 与标题文本，不含 `##` 前缀）。"""
    out = []
    for i, ln in enumerate(text.splitlines()):
        m = H2_RE.match(ln)
        if m:
            out.append({"lineno": i + 1, "title": m.group("title"),
                        "raw": ln.strip()})
    return out


def heading_titles(text: str) -> list[str]:
    return [h["title"] for h in iter_h2_headings(text)]


def datum_bullets(text: str) -> list[dict]:
    """全文件所有可能的历史 bullet（`-<date> ...`），不论是否在历史段内。"""
    out = []
    for i, ln in enumerate(text.splitlines()):
        m = DATUM_BULLET_RE.match(ln)
        if m:
            out.append({"lineno": i + 1, "date": m.group(1),
                        "rest": m.group("rest"), "raw": ln.strip()})
    return out


def history_line_numbers(text: str) -> set[int]:
    """历史段内**行号集合**（0-based），与 `parse_history()` 的段判定逐行一致。

    注意：段外命令行单独用 `parse_history_line()` 去判会得到"能解析"的错误结论
    （`:59` 的段闸门是在 parse_history 里做的，不在行级函数里）—— 本函数负责把
    「段内/段外」这件事显式算出来，S3 才算真的覆盖到 T60 D1 那种游离 bullet。
    """
    out: set[int] = set()
    in_history = False
    for i, ln in enumerate(text.splitlines()):
        s = ln.strip()
        if s.startswith(HISTORY_HEADING):
            in_history = True
            continue
        if s.startswith("## "):
            in_history = False
            continue
        if in_history:
            out.add(i)
    return out


def extreme_rounds(entries: list[dict]) -> dict:
    """按 (date, 时刻) 取首/末轮 —— S4 要求 summarize() 用的就是这个语义。"""
    ordered = sorted(entries, key=lambda e: (e["date"], HEALTH._time_key(e["time"])))
    first, last = (ordered[0] if ordered else None), (ordered[-1] if ordered else None)
    return {
        "first": f"{first['date']} {first['time']}" if first else None,
        "last": f"{last['date']} {last['time']}" if last else None,
    }


# --------------------------------------------------------------------------- #
# 六条不变式
# --------------------------------------------------------------------------- #
def check_heading_uniqueness(text: str) -> dict:
    """S1：`## ` 标题逐字不得重复。"""
    heads = iter_h2_headings(text)
    seen: dict[str, list[int]] = {}
    for h in heads:
        seen.setdefault(h["title"], []).append(h["lineno"])
    dups = {t: ls for t, ls in seen.items() if len(ls) > 1}
    return {
        "total_headings": len(heads),
        "distinct_headings": len(seen),
        "duplicates": dups,
        "ok": not dups,
    }


def check_history_section_count(text: str) -> dict:
    """S2：`## 执行历史*` 段必须恰好 1 个。"""
    hits = [h for h in iter_h2_headings(text)
            if h["raw"].startswith(HISTORY_HEADING)]
    return {
        "count": HEALTH.count_history_sections(text),
        "matched_titles": [h["raw"] for h in hits],
        "ok": HEALTH.count_history_sections(text) == 1,
    }


def check_bullet_coverage(text: str) -> dict:
    """S3：所有 `- <date>` bullet 都必须**落在历史段内并解析成条目**。

    两种失效分开记账，避免把 benign 与非 benign 混成一个数字：
      stranded          段外的 date bullet —— 结构损坏（T60 D1 截断形态）→ RED
      dropped_in_section 段内但行级/语义判据未收下 —— 可能是漏记的运行条目（D2 指纹），
                        只报 AMBER，不判 FAIL（现行解析器有意忽略「无 T 号且非巡检」的 bullet）
    """
    bullets = datum_bullets(text)
    hist = history_line_numbers(text)
    stranded = [{"lineno": b["lineno"], "raw": b["raw"][:160]}
                for b in bullets if (b["lineno"] - 1) not in hist]
    dropped = []
    for b in bullets:
        if (b["lineno"] - 1) in hist and HEALTH.parse_history_line(b["raw"]) is None:
            dropped.append({"lineno": b["lineno"], "raw": b["raw"][:160]})
    return {
        "date_bullets": len(bullets),
        "parsed_entries": HEALTH.parse_history(text).__len__(),
        "stranded": stranded,
        "dropped_in_section": dropped,
        "ok": not stranded and not dropped,
    }


def check_field_completeness(text: str) -> dict:
    """S5：条目 date/time/body 非空，且每个 date bullet 都带时刻 token。"""
    entries = HEALTH.parse_history(text)
    bad_entries = [{"lineno": i + 1,
                    "reason": "empty_date" if not e["date"] else
                              ("empty_time" if not e["time"] else "empty_body"),
                    "raw": (e["date"] + " " + e["time"]).strip()}
                   for i, e in enumerate(entries)
                   if not (e["date"] and e["time"] and e["body"])]
    no_time = [{"lineno": b["lineno"], "raw": b["raw"][:120]}
               for b in datum_bullets(text) if not TIME_TOKEN_RE.search(b["rest"])]
    return {
        "entries": len(entries),
        "malformed_entries": bad_entries,
        "bullets_without_time": no_time,
        "ok": not bad_entries and not no_time,
    }


def check_duplicate_entries(text: str) -> dict:
    """S6（信息）：(date, 时刻) 完全相同的条目数 —— 会静默放大轮次，但不判 FAIL。"""
    entries = HEALTH.parse_history(text)
    seen: dict[tuple, int] = {}
    for e in entries:
        seen[(e["date"], e["time"])] = seen.get((e["date"], e["time"]), 0) + 1
    dups = {f"{k[0]} {k[1]}": v for k, v in seen.items() if v > 1}
    return {"entries": len(entries), "duplicate_keys": len(dups), "duplicates": dups,
            "ok": not dups}


def check_extremes_semantics(text: str) -> dict:
    """S4：真实文档上判定「首末轮取极值」是否确实生效（与「取文件行序」可区分）。"""
    entries = HEALTH.parse_history(text)
    if not entries:
        return {"ok": False, "detail": "no entries parsed"}
    extreme = extreme_rounds(entries)
    file_first = f"{entries[0]['date']} {entries[0]['time']}"
    file_last = f"{entries[-1]['date']} {entries[-1]['time']}"
    summarize = HEALTH.summarize(entries)["totals"]
    return {
        "extreme": extreme,
        "file_order_first": file_first,
        "file_order_last": file_last,
        "summarize_first": summarize["first_round"],
        "summarize_last": summarize["last_round"],
        # True = 现行语义确实与「取行序」不同，D3 修复在生效
        "distinguishable_from_file_order": (file_first != extreme["first"]),
        "matches_summarize": (summarize["first_round"] == extreme["first"]
                              and summarize["last_round"] == extreme["last"]),
        "ok": (file_first != extreme["first"]) and
              (summarize["first_round"] == extreme["first"]),
    }


# --------------------------------------------------------------------------- #
# 审计
# --------------------------------------------------------------------------- #
def read_memory(path: str = MEMORY_PATH) -> str:
    if not os.path.exists(path):
        raise SystemExit(f"[NOGO] memory 不存在: {path}")
    with open(path, encoding="utf-8") as f:
        return f.read()


def build_findings(text: str) -> dict:
    s1 = check_heading_uniqueness(text)
    s2 = check_history_section_count(text)
    s3 = check_bullet_coverage(text)
    s5 = check_field_completeness(text)
    s4 = check_extremes_semantics(text)
    s6 = check_duplicate_entries(text)

    red, amber = [], []
    if not s1["ok"]:
        red.append({"id": "R1_DUPLICATE_HEADING",
                    "detail": f"`## ` 标题重复: {s1['duplicates']}；旧语义会在重复处截断"})
    if not s2["ok"]:
        red.append({"id": "R2_MULTIPLE_HISTORY_SECTIONS",
                    "detail": f"`## 执行历史*` 段数 = {s2['count']}（应为 1）"})
    if s3["stranded"]:
        red.append({"id": "R3_STRANDED_BULLET",
                    "detail": f"{len(s3['stranded'])} 个 `- <date>` bullet 落在历史段之外"
                              f"（行号 {[b['lineno'] for b in s3['stranded']]}），"
                              f"这些运行条目会被静默忽略 —— T60 D1 的截断形态"})
    if not s4["ok"]:
        red.append({"id": "R4_EXTREMES_SEMANTICS_REGRESSED",
                    "detail": f"first_round 未取 (date, 时刻) 极值: extreme="
                              f"{s4.get('extreme')} / summarize={s4.get('summarize_first')}"})
    if not s5["ok"]:
        red.append({"id": "R5_MALFORMED_ENTRY",
                    "detail": f"畸形条目 {len(s5['malformed_entries'])} / "
                              f"缺时刻 bullet {len(s5['bullets_without_time'])}"})
    if s3["dropped_in_section"]:
        amber.append({"id": "A2_DROPPED_BULLET_IN_SECTION",
                      "detail": f"{len(s3['dropped_in_section'])} 个段内 date bullet 未被收成条目"
                                f"（行号 {[b['lineno'] for b in s3['dropped_in_section']]}）—— "
                                f"若为漏记的运行条目，说明 D2 正则问题已回归"})
    if s6["duplicate_keys"]:
        amber.append({"id": "A1_DUPLICATE_ENTRY_KEYS",
                      "detail": f"{s6['duplicate_keys']} 个 (date, 时刻) 重复，会静默放大轮次: "
                                f"{list(s6['duplicates'])[:5]}"})

    headings = heading_titles(text)
    return {
        "verdict": "FAIL" if red else "PASS",
        "red": red,
        "amber": amber,
        "invariants": {"S1_heading_unique": s1, "S2_history_section_single": s2,
                       "S3_bullet_coverage": s3, "S4_extremes_semantics": s4,
                       "S5_field_complete": s5, "S6_duplicate_noise": s6},
        "observed": {
            "headings": headings,
            "history_headings": s2["matched_titles"],
            "entries": HEALTH.parse_history(text).__len__(),
            "date_bullets": s3["date_bullets"],
        },
    }


def render_md(findings: dict) -> str:
    iv = findings["invariants"]
    s1, s2, s3 = iv["S1_heading_unique"], iv["S2_history_section_single"], iv["S3_bullet_coverage"]
    s4, s5, s6 = iv["S4_extremes_semantics"], iv["S5_field_complete"], iv["S6_duplicate_noise"]
    lines = [
        "# T65 自动化 memory 结构不变式守卫",
        "",
        f"> 生成于 {datetime.now(timezone.utc).isoformat()} · 只读审计，不改 memory 内容、"
        f"不碰任何数据库",
        "",
        f"**判定: {findings['verdict']}**",
        "",
        "| 不变式 | 检查内容 | 实测 | 结果 |",
        "|---|---|---|---|",
        f"| S1 | `## ` 标题逐字唯一 | {s1['distinct_headings']}/{s1['total_headings']} 去重 | "
        f"{'OK' if s1['ok'] else 'RED'} |",
        f"| S2 | `## 执行历史*` 段数 == 1 | {s2['count']} | {'OK' if s2['ok'] else 'RED'} |",
        f"| S3 | date bullet 全覆盖 | {s3['parsed_entries']}/{s3['date_bullets']} 解析，"
        f"段外游离 {len(s3['stranded'])} · 段内漏收 {len(s3['dropped_in_section'])} | "
        f"{'OK' if s3['ok'] else 'RED'} |",
        f"| S4 | 首末轮取 (date,时刻) 极值 | first={s4.get('extreme', {}).get('first')} · "
        f"与行序可区分={s4.get('distinguishable_from_file_order')} | "
        f"{'OK' if s4['ok'] else 'RED'} |",
        f"| S5 | 条目字段完整 | 条目 {s5['entries']}，缺时刻 bullet "
        f"{len(s5['bullets_without_time'])} | {'OK' if s5['ok'] else 'RED'} |",
        f"| S6 | (date,时刻) 重复噪声 | {s6['duplicate_keys']} 组"
        f"{'' if s6['ok'] else ' · ' + str(list(s6['duplicates'])[:3])} | INFO |",
        "",
        "## 为什么这六条能挡住 T60",
        "",
        "- **D1（文件结构事故）** = `## 执行历史` 出现第二次。S1 抓逐字重复，S2 抓段数 != 1。",
        "- **D2（正则丢行）** = 带区间时刻的整行不匹配 → 该 bullet 连条目都不是。S3 直接比对"
        "「文件里有几个 date bullet」与「解出几条」，S5 顺带要求每个 bullet 都带时刻 token。",
        "- **D3（首末轮取行序）** = 新条目追加在顶部，取行序会把 first_round 顶到最新一轮。S4 要求"
        "取 (date, 时刻) 极值；真实文档首行不是最旧条目，故「取极值」与「取行序」可区分，判定有效。",
        "- **S6 不判 FAIL**：重复条目（同一轮被反复记录）是噪声不是结构损坏，但会静默放大轮次，"
        "故只报数。",
        "",
        "## 覆盖边界（诚实标注）",
        "",
        "本守卫**不能**挡住以下情况，勿过度信任：",
        f"- 「近似重复」标题（如 `## 执行历史（仅高层…）` 与 `## 执行历史` 各一份）逐字不同，S1 不抓；"
        f"当前文件标题为 {findings['observed']['headings']}。",
        "- 同一轮被写进历史段的**多次**（S6 只报数不 FAIL）。",
        "- 条目内容本身失真（写错结论）—— 本守卫只管结构，不管事实。",
        "",
    ]
    if findings["red"]:
        lines += ["## RED", ""] + [f"- `{r['id']}` — {r['detail']}" for r in findings["red"]] + [""]
    if findings["amber"]:
        lines += ["## AMBER", ""] + [f"- `{r['id']}` — {r['detail']}" for r in findings["amber"]] + [""]
    return "\n".join(lines) + "\n"


def write_report(findings: dict) -> None:
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    with open(OUT_JSON, "w", encoding="utf-8") as f:
        json.dump(findings, f, ensure_ascii=False, indent=2)
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write(render_md(findings))


def main() -> int:
    findings = build_findings(read_memory())
    write_report(findings)
    iv = findings["invariants"]
    print(f"[OK] 判定 {findings['verdict']} · 条目 {iv['S3_bullet_coverage']['parsed_entries']}"
          f"/bullet {iv['S3_bullet_coverage']['date_bullets']} · "
          f"标题 {iv['S1_heading_unique']['total_headings']} · 段 {iv['S2_history_section_single']['count']}"
          f" -> {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
