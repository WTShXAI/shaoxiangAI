"""reports/ 陈旧度只读审计（T17, 自动化 dbda4380 任务队列）。

目标：枚举 reports/ 顶层产物，按 mtime 计算陈旧度，对照 autonomous_monitor 的
校准漂移 / 重训门控状态，给出分类与重生成建议。

分类口径：
- STALE_HARD           : age_days > threshold_days(默认30) → 过期，建议重生成或归档评审
- LIVE                 : monitor 活动校准报告(prediction_calibration_report.*)，由 monitor 周期刷新
- INVALIDATED_BY_RETRAIN: 模型评估类报告，且 retrain_gate.suggest=True（模型待重训，结论待作废）
- DRIFT_IMPACTED       : 模型评估类报告，且 calibration.drift.drift_alert=True（校准漂移告警）
- ORPHANED             : 非活动引用静态产物，age_days > orphan_window(默认14) 且非模型/校准类
- OK                   : 新鲜 / 活动基础设施报告 / 模型类且当前无漂移无重训建议

交叉对照来源：reports/monitor_status.json（autonomous_monitor 每周期落盘）。
- calibration.drift.drift_alert → DRIFT_IMPACTED 触发
- retrain_gate.suggest        → INVALIDATED_BY_RETRAIN 触发

安全红线：
- 只读：不打开 events.db，不写任何数据库，不删不改任何源报告。
- 仅落盘新审计报告 reports/stale_reports_audit.{json,md}。
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR = os.path.join(ROOT, "reports")
MONITOR_STATUS = os.path.join(REPORTS_DIR, "monitor_status.json")
LIVE_CALIB_REPORT = "prediction_calibration_report.json"

# 模型/分析类报告（受 retrain_gate / drift 影响）→ 过期或漂移即失效
MODEL_EVAL_PATTERNS = (
    "candles_", "p0_", "ht_anchor", "walkforward", "_oos_", "_eval", "calibration",
    "kronos", "odds_multi", "fused_", "fl_model", "microstructure",
    "independent_divergence", "league_shrink", "draw_calibration",
    "narrative_feature", "league_calibration", "goalscale", "efootball",
    "direction_", "research_", "evaluate_", "robustness", "paper_track",
    "candidates_", "cs_", "devig_power", "backtest", "predictions_",
)

# 活动基础设施报告（由 autonomous pipeline 周期刷新，不受 retrain 作废）
ACTIVE_INFRA_PATTERNS = (
    "monitor_status", "monitor_history", "verification_report",
    "verification_sample_growth", "env_health_check", "stale_reports_audit",
    "audit_", "data_integrity", "prediction_calibration_report",
)


def days_old(epoch, now=None):
    """返回给定 mtime epoch 距 now 的天数（浮点）。"""
    if now is None:
        now = time.time()
    return (now - epoch) / 86400.0


def is_stale(epoch, now=None, threshold_days=30):
    """age_days > threshold_days 即陈旧。"""
    return days_old(epoch, now) > threshold_days


def is_model_eval(basename):
    return any(basename.startswith(p) or p in basename for p in MODEL_EVAL_PATTERNS)


def is_active_infra(basename):
    return any(basename.startswith(p) for p in ACTIVE_INFRA_PATTERNS)


def load_monitor_status(path):
    """读取 monitor_status.json；缺失/损坏返回 None（绝不抛错）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def classify_report(basename, age_days, monitor, threshold_days=30, orphan_window=14):
    """对单份报告分类。纯函数，便于单测。"""
    calib = (monitor or {}).get("calibration") or {}
    retrain = (monitor or {}).get("retrain_gate") or {}
    drift_alert = bool(((calib.get("drift") or {}).get("drift_alert")) is True)
    retrain_suggest = bool(retrain.get("suggest"))

    if age_days > threshold_days:
        return "STALE_HARD"
    if basename == LIVE_CALIB_REPORT:
        return "LIVE"
    if is_model_eval(basename):
        if retrain_suggest:
            return "INVALIDATED_BY_RETRAIN"
        if drift_alert:
            return "DRIFT_IMPACTED"
        return "OK"
    if is_active_infra(basename):
        return "OK"
    return "ORPHANED" if age_days > orphan_window else "OK"


def suggest_regen(status, basename):
    """按分类给重生成/处置建议（纯文本）。"""
    if status == "STALE_HARD":
        if is_model_eval(basename) or basename == LIVE_CALIB_REPORT:
            return "建议重生成（模型/校准类，过期失效）"
        return "建议归档评审（静态产物，过期）"
    if status == "INVALIDATED_BY_RETRAIN":
        return "模型待重训 → 重训评估后结论方有效，旧报告暂不引用"
    if status == "DRIFT_IMPACTED":
        return "校准漂移告警 → 复核/重生成"
    if status == "ORPHANED":
        return "无活动引用 → 建议归档"
    return ""


def build_report(reports_dir, monitor_status_path, threshold_days=30,
                 orphan_window=14, now=None, output_json=None, output_md=None):
    """枚举 reports/ 顶层文件（跳过子目录），分类并落盘审计报告。

    返回 (report_dict, output_json_path, output_md_path)。
    """
    if now is None:
        now = time.time()
    monitor = load_monitor_status(monitor_status_path)

    entries = []
    for f in sorted(os.listdir(reports_dir)):
        fp = os.path.join(reports_dir, f)
        if not os.path.isfile(fp):
            continue  # 跳过子目录（_model_archives/_scripts_archives/asset_snapshots/cn 等）
        try:
            st = os.stat(fp)
        except OSError:
            continue
        epoch = st.st_mtime
        age_days = days_old(epoch, now)
        status = classify_report(f, age_days, monitor, threshold_days, orphan_window)
        entries.append({
            "name": f,
            "size_bytes": st.st_size,
            "mtime_iso": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch)),
            "age_days": round(age_days, 2),
            "status": status,
            "suggestion": suggest_regen(status, f),
        })

    # 汇总
    by_status = {}
    for e in entries:
        by_status.setdefault(e["status"], 0)
        by_status[e["status"]] += 1

    calib = (monitor or {}).get("calibration") or {}
    retrain = (monitor or {}).get("retrain_gate") or {}
    drift_alert = bool(((calib.get("drift") or {}).get("drift_alert")) is True)
    retrain_suggest = bool(retrain.get("suggest"))

    summary = {
        "total_reports": len(entries),
        "by_status": by_status,
        "threshold_days": threshold_days,
        "orphan_window_days": orphan_window,
        "monitor_present": monitor is not None,
        "monitor_retrain_suggest": retrain_suggest,
        "monitor_drift_alert": drift_alert,
        "stale_hard_count": by_status.get("STALE_HARD", 0),
        "invalidated_by_retrain": by_status.get("INVALIDATED_BY_RETRAIN", 0),
        "drift_impacted": by_status.get("DRIFT_IMPACTED", 0),
    }

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "reports_dir": reports_dir,
        "summary": summary,
        "entries": entries,
    }

    if output_json:
        with open(output_json, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
    if output_md:
        _write_md(report, output_md)
    return report, output_json, output_md


def _write_md(report, path):
    s = report["summary"]
    lines = []
    lines.append("# reports/ 陈旧度审计报告")
    lines.append("")
    lines.append(f"- 生成时间: {report['generated_at']}")
    lines.append(f"- 扫描目录: `{report['reports_dir']}`")
    lines.append(f"- 陈旧阈值: >{s['threshold_days']}d ｜ 孤儿窗口: >{s['orphan_window_days']}d")
    lines.append(f"- monitor 状态: {'存在' if s['monitor_present'] else '缺失'} ｜ "
                 f"重训建议={s['monitor_retrain_suggest']} ｜ 漂移告警={s['monitor_drift_alert']}")
    lines.append("")
    lines.append(f"## 汇总（共 {s['total_reports']} 份）")
    lines.append("")
    order = ["STALE_HARD", "INVALIDATED_BY_RETRAIN", "DRIFT_IMPACTED",
             "ORPHANED", "LIVE", "OK"]
    for st in order:
        if st in s["by_status"]:
            lines.append(f"- {st}: {s['by_status'][st]}")
    lines.append("")
    lines.append("## 需关注项（STALE_HARD / INVALIDATED / DRIFT / ORPHANED）")
    lines.append("")
    lines.append("| 报告 | 年龄(d) | 状态 | 建议 |")
    lines.append("|------|--------|------|------|")
    for e in report["entries"]:
        if e["status"] in ("OK", "LIVE"):
            continue
        lines.append(f"| {e['name']} | {e['age_days']} | {e['status']} | {e['suggestion']} |")
    lines.append("")
    lines.append("## 全量清单")
    lines.append("")
    lines.append("| 报告 | 大小(B) | mtime | 年龄(d) | 状态 |")
    lines.append("|------|--------|-------|--------|------|")
    for e in report["entries"]:
        lines.append(f"| {e['name']} | {e['size_bytes']} | {e['mtime_iso']} | "
                     f"{e['age_days']} | {e['status']} |")
    lines.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


def main():
    ap = argparse.ArgumentParser(description="reports/ 陈旧度只读审计")
    ap.add_argument("--reports-dir", default=REPORTS_DIR)
    ap.add_argument("--monitor-status", default=MONITOR_STATUS)
    ap.add_argument("--threshold-days", type=float, default=30)
    ap.add_argument("--orphan-window", type=float, default=14)
    ap.add_argument("--output-json", default=os.path.join(REPORTS_DIR, "stale_reports_audit.json"))
    ap.add_argument("--output-md", default=os.path.join(REPORTS_DIR, "stale_reports_audit.md"))
    args = ap.parse_args()

    report, oj, om = build_report(
        args.reports_dir, args.monitor_status,
        threshold_days=args.threshold_days, orphan_window=args.orphan_window,
        output_json=args.output_json, output_md=args.output_md,
    )
    s = report["summary"]
    print(f"[audit_stale_reports] 扫描 {s['total_reports']} 份报告")
    print(f"  STALE_HARD={s['stale_hard_count']}  "
          f"INVALIDATED_BY_RETRAIN={s['invalidated_by_retrain']}  "
          f"DRIFT_IMPACTED={s['drift_impacted']}")
    print(f"  monitor: retrain_suggest={s['monitor_retrain_suggest']}  "
          f"drift_alert={s['monitor_drift_alert']}")
    print(f"  报告已落盘: {oj}\n              {om}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
