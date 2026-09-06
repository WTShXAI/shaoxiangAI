# -*- coding: utf-8 -*-
"""
基于已存盘的 raw OCR (data/leisu_capture/long_ocr/X_unknown/*.json 等) 重新分类。
修正初始分类器的遗漏:
  - 识别雷速赛前/赛中情报页 (距离开赛/赛事详情)
  - 识别机构盘口对比+走势页 (让步/大小/机构/走势 + 时间戳序列) ← 高价值
  - 识别并剔除无关支付/财务截图 (付款/支付宝/EBpay/贷款/总资产/账单详情)
秒级运行 (只读 JSON)。
"""
import os, json, re, glob

OCR_ROOT = r"D:\Architecture\data\leisu_capture\long_ocr"
STATS = r"D:\Architecture\data\long_classify_stats.json"
OUT_STATS = r"D:\Architecture\data\long_classify_stats_v2.json"

# 无关垃圾关键词 (支付/财务/个人) — 必须剔除
JUNK_KW = ("付款金额", "支付宝", "收款二维码", "EBpay", "EB", "贷款", "贷款金额", "年化利率",
           "等额本金", "总资产", "账单详情", "确认支付", "我的余额", "待付款", "客服",
           "优惠券", "活动红包", "剩余支付时间", "交易成功", "收款人", "户节晨", "睡眠")

def classify(joined):
    """joined = ' | '.join(文本行)。返回 type。"""
    # 1. 垃圾 (支付/财务) 优先剔除
    if sum(1 for k in JUNK_KW if k in joined) >= 2:
        return "Z_junk_payment"
    # 2. 机构盘口对比+走势页 (高价值): 让步/大小/胜平负/机构/走势/角球
    odds_trend = sum(1 for k in ("让步", "大小", "胜平负", "机构", "走势", "即时", "终盘", "初盘") if k in joined)
    if odds_trend >= 3:
        return "E_odds_trend"
    # 3. 雷速赛事情报页: 距离开赛 + 联赛 + 队名 VS
    if ("距离开赛" in joined) or (re.search(r"\d{2}:\d{2}:\d{2}", joined) and ("VS" in joined or "vs" in joined)):
        return "F_match_info"
    # 4. 投注账单页
    a_kw = sum(1 for k in ("投注单号", "总投注额", "总输赢", "总可赢额", "结算时间", "结果比分", "全场比分") if k in joined)
    if a_kw >= 2:
        return "A_bill"
    # 5. 滚球单场赔率页
    b_kw = sum(1 for k in ("全场独赢", "全场让球", "全场大小", "和局", "视频直播", "动画直播") if k in joined)
    has_live_time = bool(re.search(r"(上|下)半场\d{1,2}:\d{2}", joined))
    if b_kw >= 2 or (b_kw >= 1 and has_live_time):
        return "B_live_odds"
    # 6. 赛事列表页
    c_kw = sum(1 for k in ("进行中", "中场休息", "主胜", "客胜", "今日滚球", "早盘", "未开场") if k in joined)
    if c_kw >= 2:
        return "C_list"
    # 7. 直播文字实录
    d_kw = sum(1 for k in ("界外球", "球门球", "危险进攻", "射门", "获得角球机会", "技术统计") if k in joined)
    if d_kw >= 2:
        return "D_live_text"
    return "X_unknown"

def main():
    manifest = []
    files = sorted(glob.glob(os.path.join(OCR_ROOT, "*", "*.json")))
    buckets = {}
    for p in files:
        fname = os.path.basename(p).rsplit(".", 1)[0]
        # 找回原文件名(含扩展名) - 从父目录名也知道旧type, 但新分类重新算
        data = json.load(open(p, encoding="utf-8"))
        txts = [d["text"] for d in data]
        joined = " | ".join(txts)
        typ = classify(joined)
        buckets[typ] = buckets.get(typ, 0) + 1
        manifest.append({"file": fname, "type": typ, "n_lines": len(txts)})
    with open(OUT_STATS, "w", encoding="utf-8") as fp:
        json.dump({"total": len(files), "buckets": buckets, "manifest": manifest}, fp, ensure_ascii=False, indent=2)
    print("=== 重新分类完成 ===")
    print(f"总计 {len(files)} 张")
    for k in sorted(buckets, key=lambda x: -buckets[x]):
        print(f"  {k:20s} {buckets[k]:4d}  ({100*buckets[k]/len(files):.0f}%)")
    print(f"\n→ {OUT_STATS}")

if __name__ == "__main__":
    main()
