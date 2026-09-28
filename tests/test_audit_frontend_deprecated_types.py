#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T06 B2 前端废弃类型审计 — 单元测试（纯函数, 临时 fixture, 不碰真实前端/events.db）。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audit_frontend_deprecated_types import audit_types  # noqa: E402


def _make_fixture(tmp_path: Path):
    """构造最小前端结构：src/types/index.ts 定义若干类型 + 组件引用。"""
    src = tmp_path / "src"
    types_dir = src / "types"
    types_dir.mkdir(parents=True)
    (types_dir / "index.ts").write_text(
        "export interface ModelComparison { models: ModelVersion[] }\n"
        "export interface ModelVersion { name: string }\n"
        "export interface ValueLayerRow { outcome: string }\n"
        "export interface MultibookConsensus { books: string[] }\n"
        "export interface TerminalDecisionCard { rows: ValueLayerRow[] }\n",
        encoding="utf-8",
    )
    pages = src / "pages"
    pages.mkdir()
    # 渲染组件：引用 ValueLayerRow / TerminalDecisionCard / MultibookConsensus
    (pages / "Modal.tsx").write_text(
        "import { ValueLayerRow, TerminalDecisionCard } from '@/types'\n"
        "export function Block({ card }: { card: TerminalDecisionCard }) {\n"
        "  const rows: ValueLayerRow[] = card.rows\n"
        "  return null\n}\n"
        "export function Cross({ c }: { c: MultibookConsensus }) { return c.books }\n",
        encoding="utf-8",
    )
    # 孤儿类型：ModelComparison 仅定义，无任何渲染引用
    return tmp_path


def test_rendered_detection(tmp_path):
    root = _make_fixture(tmp_path)
    data = audit_types(root)
    by_id = {r["identifier"]: r for r in data["results"]}
    assert by_id["ValueLayerRow"]["status"] == "RENDERED"
    assert by_id["TerminalDecisionCard"]["status"] == "RENDERED"
    assert by_id["MultibookConsensus"]["status"] == "RENDERED"
    assert by_id["ValueLayerRow"]["render_refs"] >= 1


def test_orphan_def_detection(tmp_path):
    root = _make_fixture(tmp_path)
    data = audit_types(root)
    by_id = {r["identifier"]: r for r in data["results"]}
    # ModelComparison 定义了但只有 ModelVersion 被引用（ModelComparison 本身无渲染）
    assert by_id["ModelComparison"]["status"] in ("ORPHAN_DEF", "TYPE_ONLY")
    assert by_id["ModelComparison"]["render_refs"] == 0


def test_crossbook_redline_flag(tmp_path):
    root = _make_fixture(tmp_path)
    data = audit_types(root)
    rb = data["summary"]["crossbook_rendered_redline"]
    assert "MultibookConsensus" in rb  # 仍在渲染 → 红线告警


def test_absent_identifier(tmp_path):
    root = _make_fixture(tmp_path)
    data = audit_types(root)
    # 清单中的 TrainingStatus 在 fixture 中不存在 → ABSENT
    by_id = {r["identifier"]: r for r in data["results"]}
    assert by_id["TrainingStatus"]["status"] == "ABSENT"


def test_scan_counts(tmp_path):
    root = _make_fixture(tmp_path)
    data = audit_types(root)
    assert data["summary"]["scanned_files"] == 2
    assert data["summary"]["identifiers_checked"] >= 10
