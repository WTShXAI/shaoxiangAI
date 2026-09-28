#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
check_env_health.py — 哨响AI 环境复现一键校验（只读巡检）

用途：
    让接手者/自动化在 30 秒内确认「这台机器能否跑起系统」的核心前置项：
      ① venv 存在且为约定的 3.12.10
      ② 核心依赖可 import 且版本在区间内（sklearn/lightgbm/torch/...)
      ③ 7 个常驻计划任务状态符合预期（bridge/gq/net/vite 启用，sandbox 停用）
      ④ 数据主库 events.db / GQ.db / football_data.db 存在且非空
    全部只读：不写任何库、不碰进程、不跑 schema。

退出码：
    0 = 全部 PASS（或仅 WARNING）
    2 = 有 FAIL（环境不可复现，须人工介入）

设计原则（红线）：
    - 纯只读。schtasks /query 只读查询；不 /run /change /disable。
    - 不 import 业务模块（避免加载副作用 / 误触采集器）。
    - 依赖版本判定只看 `pip list` 输出字符串，不实际 import 重模型。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

REPO = r"D:\Architecture"

# 约定 venv 解释器
VENV_PY = os.path.join(REPO, ".venv", "Scripts", "python.exe")

# 核心依赖 + 期望区间（与 requirements.txt / 环境复现指南 对齐）
# 期望值取自 2026-09-24 实测；区间用于判定「是否落在可控范围」
EXPECTED_DEPS = {
    "scikit-learn": (">=1.9.0,<2.0.0", "1.9.0"),
    "numpy": ("<2.1", "2.0.2"),
    "pandas": ("<3", "2.3.3"),
    "scipy": ("<2", "1.18.0"),
    "joblib": ("<2", "1.5.3"),
    "lightgbm": ("<5", "4.7.0"),
    "xgboost": ("<3", "2.1.4"),
    "torch": ("<3", "2.13.0"),
}

# 计划任务期望状态：True=应启用, False=应停用（仅状态核对，不改动）
# 注：env 指南 §7.1 的 "ShaoxiangGQ_Watchdog" 已退役，2026-09-24 起由
#     "ShaoxiangAI_ProdGuardian" 接管 ws_collector/GQ token 自愈（见 prod_guardian 记忆）。
EXPECTED_TASKS = {
    "ShaoxiangBridge_Watchdog": True,
    "ShaoxiangAI_ProdGuardian": True,
    "ShaoxiangAI_NetWatch": True,
    "ShaoxiangVite": True,
    "ShaoxiangAI_DailyRecheck": True,
    "ShaoXiangOddsAssetDaily": True,
    "ShaoxiangSandbox2301": False,
}

# 数据主库（存在 + 体积 > 0 即视为就位）
CORE_DBS = {
    "events.db": os.path.join(REPO, "data", "events.db"),
    "GQ.db": os.path.join(REPO, "data", "GQ.db"),
    "football_data.db": os.path.join(REPO, "data", "football_data.db"),
}


@dataclass
class CheckResult:
    name: str
    status: str  # PASS / WARN / FAIL
    detail: str
    hint: str = ""


@dataclass
class EnvHealth:
    venv: CheckResult = None  # type: ignore
    deps: List[CheckResult] = field(default_factory=list)
    tasks: List[CheckResult] = field(default_factory=list)
    dbs: List[CheckResult] = field(default_factory=list)
    python_version: str = ""
    summary: str = ""
    exit_code: int = 0

    def all_results(self) -> List[CheckResult]:
        out: List[CheckResult] = []
        if self.venv:
            out.append(self.venv)
        out.extend(self.deps)
        out.extend(self.tasks)
        out.extend(self.dbs)
        return out

    def to_dict(self) -> Dict:
        return {
            "python_version": self.python_version,
            "summary": self.summary,
            "exit_code": self.exit_code,
            "checks": [
                {"name": r.name, "status": r.status, "detail": r.detail, "hint": r.hint}
                for r in self.all_results()
            ],
        }


def _run(cmd: List[str], timeout: int = 30) -> Optional[str]:
    """只读命令执行；失败返回 None，绝不抛。

    字节捕获 + errors=replace：Windows 计划任务(schtasks)等输出为 GBK 编码，
    text=True 在子线程 utf-8 解码会崩，改手动按 GBK→utf-8 容错解码。
    """
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            shell=(os.name != "posix"),
        )
        raw = (p.stdout or b"") + (p.stderr or b"")
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("gbk", errors="replace")
    except Exception:
        return None


def check_venv() -> CheckResult:
    if not os.path.isfile(VENV_PY):
        return CheckResult(
            "venv",
            "FAIL",
            f"venv 不存在: {VENV_PY}",
            "运行 python -m venv .venv 后 pip install -r requirements.txt",
        )
    out = _run([VENV_PY, "--version"])
    ver = (out or "").strip().replace("Python ", "")
    # 期望 3.12.x
    ok = ver.startswith("3.12")
    return CheckResult(
        "venv",
        "PASS" if ok else "WARN",
        f"venv python={ver} @ {VENV_PY}",
        "" if ok else "约定 3.12.10；其他版本可能行为不一致",
    )


def _pip_list(venv_py: str) -> Dict[str, str]:
    if not os.path.isfile(venv_py):
        return {}
    out = _run([venv_py, "-m", "pip", "list"])
    if not out:
        return {}
    versions: Dict[str, str] = {}
    for line in out.splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)\s+([0-9][0-9A-Za-z.\-]*)", line)
        if m:
            versions[m.group(1).lower()] = m.group(2)
    return versions


def _version_ge(v: str, floor: str) -> bool:
    def parse(x: str):
        return [int(p) for p in re.findall(r"\d+", x)[:3]] + [0, 0, 0]

    return parse(v) >= parse(floor)


def _version_lt(v: str, ceil: str) -> bool:
    def parse(x: str):
        return [int(p) for p in re.findall(r"\d+", x)[:3]] + [0, 0, 0]

    return parse(v) < parse(ceil)


def check_deps(venv_py: str) -> List[CheckResult]:
    versions = _pip_list(venv_py)
    results: List[CheckResult] = []
    for dep, (spec, expected) in EXPECTED_DEPS.items():
        got = versions.get(dep.lower())
        if not got:
            results.append(
                CheckResult(
                    f"dep:{dep}",
                    "FAIL",
                    f"未安装（期望 {spec}, 实测 无）",
                    f"pip install {dep}",
                )
            )
            continue
        # 解析 spec 区间
        low = re.search(r">=([0-9][0-9A-Za-z.\-]*)", spec)
        ceil = re.search(r"<([0-9][0-9A-Za-z.\-]*)", spec)
        ok = True
        if low and not _version_ge(got, low.group(1)):
            ok = False
        if ceil and not _version_lt(got, ceil.group(1)):
            ok = False
        results.append(
            CheckResult(
                f"dep:{dep}",
                "PASS" if ok else "FAIL",
                f"{dep}=={got} (期望 {spec})",
                "" if ok else "版本越界，可能影响推理一致性",
            )
        )
    return results


def _query_tasks() -> Dict[str, str]:
    """只读 schtasks /query，返回 {task_name: 'Ready'/'Disabled'/...}。

    兼容中英文本地化输出：
      英文: TaskName: / Status:  → Ready / Running / Disabled
      中文: 任务名: / 模式:      → 就绪 / 正在运行 / 已禁用(禁用)
    """
    out = _run(["schtasks", "/query", "/fo", "LIST"])
    if not out:
        return {}
    tasks: Dict[str, str] = {}
    name = None
    for line in out.splitlines():
        line = line.rstrip()
        low = line.lower()
        if low.startswith("taskname:") or low.startswith("任务名:"):
            val = line.split(":", 1)[1].strip()
            name = val.rsplit("\\", 1)[-1]
        elif low.startswith("status:") or low.startswith("模式:") and name:
            raw = line.split(":", 1)[1].strip()
            status = _normalize_task_status(raw)
            tasks[name] = status
            name = None
    return tasks


def _normalize_task_status(raw: str) -> str:
    """本地化状态 → 归一英文。"""
    r = raw.lower()
    if r in ("就绪", "ready"):
        return "Ready"
    if r in ("正在运行", "running"):
        return "Running"
    if r in ("已禁用", "禁用", "disabled"):
        return "Disabled"
    return raw


def check_tasks() -> List[CheckResult]:
    actual = _query_tasks()
    if not actual:
        return [
            CheckResult(
                "tasks",
                "WARN",
                "无法读取计划任务（schtasks 不可用/权限不足）",
                "以管理员运行或手动核对 §7.1 七个任务",
            )
        ]
    results: List[CheckResult] = []
    for task, want_enabled in EXPECTED_TASKS.items():
        status = actual.get(task)
        if status is None:
            results.append(
                CheckResult(
                    f"task:{task}",
                    "FAIL",
                    f"任务不存在（期望 {'启用' if want_enabled else '停用'}）",
                    "schtasks /create 按 §7.1 重建",
                )
            )
            continue
        is_enabled = status.lower() in ("ready", "running")
        ok = is_enabled == want_enabled
        results.append(
            CheckResult(
                f"task:{task}",
                "PASS" if ok else "FAIL",
                f"状态={status}（期望 {'启用' if want_enabled else '停用'}）",
                "" if ok else "schtasks /change /tn <name> /enable|/disable",
            )
        )
    return results


def check_dbs() -> List[CheckResult]:
    results: List[CheckResult] = []
    for label, path in CORE_DBS.items():
        if not os.path.isfile(path):
            results.append(
                CheckResult(
                    f"db:{label}",
                    "FAIL",
                    f"缺失: {path}",
                    "从备份还原或随仓库/采集累积",
                )
            )
            continue
        size = os.path.getsize(path)
        ok = size > 0
        results.append(
            CheckResult(
                f"db:{label}",
                "PASS" if ok else "FAIL",
                f"{size/1024/1024/1024:.2f} GB @ {path}",
                "" if ok else "空文件，数据未就位",
            )
        )
    return results


def run_health(venv_py: str = VENV_PY) -> EnvHealth:
    h = EnvHealth()
    venv_res = check_venv()
    h.venv = venv_res
    if venv_res.status == "PASS":
        h.python_version = venv_res.detail.split("python=")[-1].split()[0]
    h.deps = check_deps(venv_py if venv_res.status != "FAIL" else venv_py)
    h.tasks = check_tasks()
    h.dbs = check_dbs()

    fails = [r for r in h.all_results() if r.status == "FAIL"]
    warns = [r for r in h.all_results() if r.status == "WARN"]
    if fails:
        h.exit_code = 2
        h.summary = f"FAIL×{len(fails)} WARN×{len(warns)} — 环境不可复现，须人工介入"
    elif warns:
        h.exit_code = 0
        h.summary = f"WARN×{len(warns)} — 可运行，有非阻断项"
    else:
        h.exit_code = 0
        h.summary = "ALL PASS — 环境可复现 GO"
    return h


def main() -> int:
    h = run_health()
    print("=" * 60)
    print("哨响AI 环境健康校验 (只读)")
    print("=" * 60)
    for r in h.all_results():
        mark = {"PASS": "✓", "WARN": "!", "FAIL": "✗"}[r.status]
        print(f"[{mark}] {r.name:22s} {r.status:4s} {r.detail}")
        if r.hint:
            print(f"    ↳ {r.hint}")
    print("-" * 60)
    print(f"结论: {h.summary}")
    print("=" * 60)
    # 同时落盘报告（只读产物，不碰库）
    try:
        os.makedirs(os.path.join(REPO, "reports"), exist_ok=True)
        out_path = os.path.join(REPO, "reports", "env_health_check.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(h.to_dict(), f, ensure_ascii=False, indent=2)
        print(f"报告: {out_path}")
    except Exception:
        pass
    return h.exit_code


if __name__ == "__main__":
    sys.exit(main())
