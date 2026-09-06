# -*- coding: utf-8 -*-
"""
long/ 419张截图快速分类扫描。
每张跑OCR → 取关键词 → 判页面类型 → 统计分布 + 落 raw_ocr JSON 供后续解析。
只分类不深度解析, 为写解析器探路。
"""
import os, json, time, re, glob
from rapidocr_onnxruntime import RapidOCR

LONG = r"D:\Architecture\long"
OUT_DIR = r"D:\Architecture\data\leisu_capture\long_ocr"
os.makedirs(OUT_DIR, exist_ok=True)
STATS = r"D:\Architecture\data\long_classify_stats.json"

def classify(txts):
    """根据OCR文本行判页面类型。返回 (type, confidence_reason)。"""
    joined = " | ".join(txts)
    # A 投注账单页: 结算/投注单号/总输赢/结果比分
    a_kw = sum(1 for k in ("投注单号", "总投注额", "总输赢", "总可赢额", "结算时间", "结果比分", "全场比分") if k in joined)
    # B 滚球单场赔率页: 下半场XX:XX + 全场独赢/让球/大小 + 和局 + 直播
    b_kw = sum(1 for k in ("全场独赢", "全场让球", "全场大小", "和局", "视频直播", "动画直播", "真人主播") if k in joined)
    has_live_time = bool(re.search(r"(上|下)半场\d{1,2}:\d{2}", joined))
    # C 赛事列表页: 进行中/中场休息 + 多个联赛名 + 主胜/客胜
    c_kw = sum(1 for k in ("进行中", "中场休息", "主胜", "客胜", "今日滚球", "早盘") if k in joined)
    # D 直播文字实录: 界外球/角球/射门/危险进攻/进攻 + 事件分钟数
    d_kw = sum(1 for k in ("界外球", "球门球", "危险进攻", "射门", "获得角球机会") if k in joined)

    scores = {"A_bill": a_kw, "B_live_odds": b_kw + (2 if has_live_time else 0), "C_list": c_kw, "D_live_text": d_kw}
    best = max(scores, key=scores.get)
    if scores[best] == 0:
        return "X_unknown", "无关键词"
    return best, json.dumps(scores, ensure_ascii=False)

def main():
    files = sorted(glob.glob(os.path.join(LONG, "*.*")))
    print(f"共 {len(files)} 张")
    eng = RapidOCR()
    t0 = time.time()
    buckets = {}
    manifest = []
    for i, f in enumerate(files):
        try:
            result, _ = eng(f)
            txts = [t for _, t, _ in result] if result else []
        except Exception as e:
            txts = []
            print(f"  [ERR] {os.path.basename(f)}: {e}")
        typ, reason = classify(txts)
        buckets[typ] = buckets.get(typ, 0) + 1
        fname = os.path.basename(f)
        manifest.append({"file": fname, "type": typ, "reason": reason, "n_lines": len(txts)})
        # 落 raw OCR (按类型分目录), 供后续解析
        sub = os.path.join(OUT_DIR, typ)
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, fname.rsplit(".", 1)[0] + ".json"), "w", encoding="utf-8") as fp:
            json.dump([{"text": t} for t in txts], fp, ensure_ascii=False)
        if (i + 1) % 20 == 0:
            el = time.time() - t0
            print(f"  [{i+1}/{len(files)}] {el:.0f}s  分布: {buckets}")
    with open(STATS, "w", encoding="utf-8") as fp:
        json.dump({"total": len(files), "buckets": buckets, "manifest": manifest}, fp, ensure_ascii=False, indent=2)
    print(f"\n=== 完成 {time.time()-t0:.0f}s ===")
    print("分布:", buckets)
    print(f"stats → {STATS}")
    print(f"raw OCR → {OUT_DIR}")

if __name__ == "__main__":
    main()
