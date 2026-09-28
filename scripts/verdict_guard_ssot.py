#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""T57 判定词表 + Tier 分层表的 **SSoT**（承接 T51 与 T53 的二选一合流）。

背景（本模块存在的唯一理由）:
  两个守卫各自写了一份「三态判定词表」与「豁免清单」, 二者**宽度不一致**:
    · T51 ``scripts/audit_verification_report_freshness.py`` 只认「两侧带引号」的写法,
      因此漏掉 ``scripts/mh_train_walkforward.py`` / ``mh_train_walkforward_x.py``
      行尾**无引号**的 ``NO EDGE (model >= market)``（T53 实测漏检两个绕过体）;
    · T53 ``scripts/audit_mh_train_div_bypass.py`` 放宽了词表, 但它的豁免清单
      （``NOISE_FILES`` / ``DOC_QUOTE_FILES``）只对自己可见,
      T51 仍会把 ``scripts/backtest_ou_signal.py`` 判成 UNEXPECTED。
  结果 = 同一个文件在两个守卫里落进不同 Tier: T53 眼里它是已登记的绕过体,
  T51 眼里它是未登记产出体 → 两个守卫互相打脸, 且放宽词表后 T51 会**永久红**在
  已知 offenders 上。故本模块定死「一份词表 + 一份 Tier 表 + 一份带理由登记册」。

规则（不可违反）:
  1. 判定词表只有一个: ``RE_VERDICT_TOKEN``（放宽版）。任何脚本**禁止**自带判定词
     正则（跨审计回归用例会因此变红）。
  2. Tier 判定只有一个: ``classify_verdict_file()``。
  3. 登记/豁免一律 ``Dict[str, str]``（**必须有理由**），空理由 = 不许登记
     （``registry_reasons_complete()`` 检出）→ 防止「每次自动豁免」退化成无守卫
     （T51 docstring 里写明的立场）。
  4. 本模块自身含有全部判定词字面量, 故两个扫描器都按 basename 自避
     （``GUARD_SELF_EXCLUDE``）—— 审计工具面本身不是产出体。

诚实边界: 放宽词表是**用精度换召回**。它让「注释里写 EDGE」也命中, 噪声面上涨
（EMITTER_OK 8→24 / DOC_META 4→21 / NOISE_KNOWN 18→22, T57 实测）; 压噪声的责任
交给 Tier 表而不是词表。放宽**不改变任何既有判定结论**: 三源仍全 NO EDGE / INCONCLUSIVE,
P0 FAILED 不变。
"""
from __future__ import annotations

import os
import re
from typing import Dict, Optional, Tuple

# ── 自避 ────────────────────────────────────────────────────────────────
MODULE_BASENAME: str = os.path.basename(__file__)      # verdict_guard_ssot.py
GUARD_SELF_EXCLUDE: Tuple[str, ...] = (MODULE_BASENAME,)

# ── 判定词表 ────────────────────────────────────────────────────────────
#: 放宽版（T53 提出, T57 定为 SSoT）: 不要求字面量两侧带引号, 故能捞出行尾无引号的写法。
#: 边界用 ``(?<![A-Za-z0-9_])``/``(?![A-Za-z0-9_])`` 而非 ``\\b``, 使 ``NO_EDGE`` 里的
#: 下划线也被算作词内字符（``NO_OVEREDGE`` 不应命中）。
RE_VERDICT_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])(?:NO_EDGE|NO EDGE|INCONCLUSIVE|EDGE|BEATS MARKET|TIE)(?![A-Za-z0-9_])"
)

#: 精确版（两侧带引号）—— 只保留给「低召回对照 / 回归用例」, 生产扫描不得使用。
RE_VERDICT_QUOTED = re.compile(r"""(?:"|')(NO_EDGE|NO EDGE|EDGE|INCONCLUSIVE)(?:"|')""")

#: IR-30 合法三态枚举（与 ``verification/gates.py`` 一致, 供调用方复用, 不在此判定）。
ALLOWED_VERDICTS: Tuple[str, ...] = ("EDGE", "NO EDGE", "INCONCLUSIVE")

# ── Tier（唯一命名）────────────────────────────────────────────────────
TIER_EMITTER: str = "EMITTER_OK"            # verification/ 包内 = gates.verdict 唯一出口
TIER_RENDER: str = "RENDER_CONSUMER"        # 只映射配色, 不产判定
TIER_NOISE: str = "NOISE_KNOWN"             # 词汇撞车: 此处判定词**不是**三态判定
TIER_DOC: str = "DOC_META"                  # 叙述/抄值/审计工具面
TIER_EXPECTED: str = "EXPECTED_TIER"        # 已登记的真绕过体（须显式登记, 退役→AMBER）
TIER_FALSE_POSITIVE: str = "KNOWN_FALSE_POSITIVE"   # 已判定的假阳性（阈值字典键等）
TIER_UNEXPECTED: str = "UNEXPECTED"         # 未登记 → RED（防豁免清单静默长大）

#: 与旧名兼容的别名（T53 旧调用点用 ``TIER_SINGLE_EXIT`` / ``TIER_AUDIT_TOOL``）。
TIER_SINGLE_EXIT: str = TIER_EMITTER
TIER_AUDIT_TOOL: str = TIER_DOC

RENDER_CONSUMER_ALLOWED: Tuple[str, ...] = ("scripts/build_dashboard.py",)
NOISE_PREFIXES: Tuple[str, ...] = ("analysis/", "sandbox/")
DOC_META_PREFIXES: Tuple[str, ...] = ("tests/",)
DOC_META_MARKERS: Tuple[str, ...] = ("audit", "_audit")

# ── 带理由登记册（空理由即不许登记）──────────────────────────────────────
#: 真绕过体: 不走 gates.verdict 而自印三态（T53 实测三个）。
EMITTER_REGISTRY: Dict[str, str] = {
    "scripts/mh_train_div.py": (
        "自印 VERDICT(divergence vs market): ΔLL -> 'BEATS MARKET'/'TIE'/'NO EDGE' "
        "(scripts/mh_train_div.py:67); 复用 gates 只会让 BEATS MARKET 翻成 NO EDGE"
    ),
    "scripts/mh_train_walkforward.py": (
        "同族绕过体 (scripts/mh_train_walkforward.py:118, 行尾无引号写法); "
        "T51 旧词表因要求两侧带引号而漏检"
    ),
    "scripts/mh_train_walkforward_x.py": (
        "同族绕过体 (scripts/mh_train_walkforward_x.py:82, 行尾无引号写法)"
    ),
}

#: 已判定的假阳性: 判定词是阈值字典键, 不是产出体。
FALSE_POSITIVE_REGISTRY: Dict[str, str] = {
    "pipeline/fusion_wdl_proto.py": (
        "判定词出现在阈值字典键位（如 pass 阈值的 'EDGE' 一类键）, 非三态产出体; "
        "T53 已知假阳性, 必须与登记册同批维护"
    ),
}

#: 词汇撞车: 这些文件里的判定词是**盘口信号**语义（OVER/UNDER/NO_EDGE = 不下注）。
NOISE_FILE_REGISTRY: Dict[str, str] = {
    "scripts/backtest_ou_signal.py": (
        "盘口/信号分类词: backend_signal() 返回 OVER/UNDER/NO_EDGE, 与 analysis/live_goal_probe.py "
        "同一套信号语义(不下注), 非验证台三态（T58 讨论改名, 在改名前先登记为噪声）"
    ),
    "scripts/audit_signal_vocab_migration.py": (
        "T62 只读盘点脚本: 词表本身即被盘点对象, 文件内大量 NO_EDGE/NO_BET 字面量属扫描面(self-exclude), "
        "非三态产出体; 登记理由=免被 T58 活体撞车扫描判为未登记"
    ),
}

#: 叙述性抄值: 硬抄当前三态结论文本, 不是产出体（T38「抄值污染」通道）。
DOC_QUOTE_REGISTRY: Dict[str, str] = {
    "scripts/model_g1_reach_plan.py": (
        "docstring 与结论文本里硬抄了当前三态(candles INCONCLUSIVE n=181 / market NO EDGE / "
        "KNN NO EDGE), 属叙述性抄值, 不是产出体"
    ),
}


def registry_reasons_complete() -> Dict[str, str]:
    """检出「登记了但没写理由」的条目 —— 空理由不许登记（防豁免清单静默长大）。"""
    bad: Dict[str, str] = {}
    for name, reg in (("EMITTER_REGISTRY", EMITTER_REGISTRY),
                      ("FALSE_POSITIVE_REGISTRY", FALSE_POSITIVE_REGISTRY),
                      ("NOISE_FILE_REGISTRY", NOISE_FILE_REGISTRY),
                      ("DOC_QUOTE_REGISTRY", DOC_QUOTE_REGISTRY)):
        for path, reason in reg.items():
            if not str(reason).strip():
                bad[path] = name
    return bad


def all_registered_files() -> Tuple[str, ...]:
    """四个登记册的并集（供「退役未登记」类检查使用）。"""
    seen = set(EMITTER_REGISTRY) | set(FALSE_POSITIVE_REGISTRY) \
        | set(NOISE_FILE_REGISTRY) | set(DOC_QUOTE_REGISTRY)
    return tuple(sorted(seen))


def reason_of(path: str, rel: str) -> Optional[str]:
    """某文件在登记册中的理由（未登记返回 None）。rel 用仓库相对路径。"""
    for reg in (EMITTER_REGISTRY, FALSE_POSITIVE_REGISTRY,
                NOISE_FILE_REGISTRY, DOC_QUOTE_REGISTRY):
        if rel in reg:
            return reg[rel]
    return None


def classify_verdict_file(rel: str) -> str:
    """判定词 → Tier 的**唯一**判定函数（白名单外一律 UNEXPECTED → RED）。

    判定次序即优先级: 包内出口 > 渲染面 > 登记册(绕过体/假阳性) > 噪声 > 元工具面 > 未登记。
    """
    rel = rel.replace("\\", "/")
    if rel.startswith("verification/"):
        return TIER_EMITTER
    if rel in RENDER_CONSUMER_ALLOWED:
        return TIER_RENDER
    if rel in EMITTER_REGISTRY:
        return TIER_EXPECTED
    if rel in FALSE_POSITIVE_REGISTRY:
        return TIER_FALSE_POSITIVE
    if rel.startswith(NOISE_PREFIXES) or rel in NOISE_FILE_REGISTRY:
        return TIER_NOISE
    if (rel.startswith(DOC_META_PREFIXES) or rel in DOC_QUOTE_REGISTRY
            or any(m in rel for m in DOC_META_MARKERS)):
        return TIER_DOC
    return TIER_UNEXPECTED


__all__ = [
    "ALLOWED_VERDICTS", "DOC_META_MARKERS", "DOC_META_PREFIXES", "DOC_QUOTE_REGISTRY",
    "EMITTER_REGISTRY", "FALSE_POSITIVE_REGISTRY", "GUARD_SELF_EXCLUDE",
    "MODULE_BASENAME", "NOISE_FILE_REGISTRY", "NOISE_PREFIXES",
    "RENDER_CONSUMER_ALLOWED", "RE_VERDICT_QUOTED", "RE_VERDICT_TOKEN",
    "TIER_AUDIT_TOOL", "TIER_DOC", "TIER_EMITTER", "TIER_EXPECTED", "TIER_FALSE_POSITIVE",
    "TIER_NOISE", "TIER_RENDER", "TIER_SINGLE_EXIT", "TIER_UNEXPECTED",
    "all_registered_files", "classify_verdict_file", "reason_of",
    "registry_reasons_complete",
]
