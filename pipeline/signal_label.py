"""三色情绪标签系统 (红绿灯 + 关键词) — 哨响AI "最强信号" 速查表.

来源: 用户提供的标签体系 (2026-08-08)。
输入: 方向(direction) + 有符号偏差(deviation_pct, +=跑赢庄家预期 / -=跑输庄家预期)
       + ROI(可选) + style(默认 balanced / aggressive 最醒目 / conservative 稳健 / strategy 老手)。
输出: {color, emoji, tag, tag_text, meaning} —— tag 形如 "🔴 冷平预警", 可直接渲染。

═══ 核心标签表 ═══
🔴 高风险/冷门类 (跑输预期 + 高偏差)
  · 冷门预警      跑输预期 + 高偏差          庄家没防, 市场在忽视, 冷门概率大
  · 平局陷阱      平局跑输 + 平局风险高      平局赔率高, 但庄家不慌, 小心诱盘
  · 反买信号      ROI偏低 + 偏差极大         大众方向错了, 考虑反向操作
  · 庄家无视      跑输预期 + 无资金支撑      庄家根本不怕这个结果打出
🟡 中性/观察类 (势均力敌 + 信号强但不极端)
  · 拉锯格局      势均力敌 + 平局信号        谁都难赢, 容易走平
  · 信号冲突      最强信号与基本面不符        数据强, 但逻辑说不通, 观望
  · 博冷区        高偏差 + ROI≈0             可小注搏冷, 不适合重仓
🟢 顺向/价值类 (跑赢预期 + 低偏差)
  · 价值方向      跑赢预期 + ROI为正         市场低估, 赔率有肉
  · 庄家防范      跑赢预期 + 资金集中        庄家在防这一路
  · 顺势跟进      偏差小 + 趋势一致          数据、资金、基本面一条线

═══ 速查表 (填字段自动出标签) ═══
  平局 - 60pp+  → 🔴 冷平预警
  主胜 + 30pp+  → 🟢 主胜价值
  客胜 - 40pp+  → 🟡 客胜诱盘

style 仅在「高偏差 + ROI≈0」边界情形调整措辞(不改变红黄绿主色):
  · aggressive(最醒目): 边界给红色(冷平预警/反买信号)
  · conservative(稳健): 边界给黄色(高偏平局·慎选 / 博冷区)
  · strategy(老手):     边界给反买/小注博冷措辞
  · balanced(默认):     按信号符号给最贴切标签
"""
from typing import Optional

# 方向归一化: H/D/A 或 home/draw/away 或中文 → 主胜/平局/客胜
_DIR_MAP = {
    'H': '主胜', '主胜': '主胜', 'home': '主胜', '主': '主胜',
    'D': '平局', '平局': '平局', 'draw': '平局', '平': '平局',
    'A': '客胜', '客胜': '客胜', 'away': '客胜', '客': '客胜',
    'PASS': 'PASS', None: 'PASS',
}

# 阈值 (单位 pp)
COLD_DEV = 60.0       # 冷门/冷平阈值 (用户举例 -60pp+)
EXTREME_DEV = 50.0    # 反买/庄家防范极端阈值
HIGH_DEV = 30.0       # 高偏差阈值
TRAP_DEV = 40.0       # 诱盘阈值 (用户举例 客胜 -40pp → 客胜诱盘)
ROI_LOW = -5.0        # ROI 偏低 (触发反买)
ROI_NEAR_ZERO = 5.0   # |ROI|<=此值视为 ≈0 (触发博冷区/边界)

_COLOR_EMOJI = {'red': '🔴', 'yellow': '🟡', 'green': '🟢'}


def normalize_direction(direction) -> str:
    if direction is None:
        return 'PASS'
    return _DIR_MAP.get(str(direction).strip(), str(direction).strip())


def _tag(color: str, tag: str, meaning: str) -> dict:
    return {
        'color': color,
        'emoji': _COLOR_EMOJI[color],
        'tag': f"{_COLOR_EMOJI[color]} {tag}",
        'tag_text': tag,
        'meaning': meaning,
    }


def _boundary(dir_cn: str, dev: float, adev: float, style: str) -> dict:
    """边界情形: 高偏差 + ROI≈0 (历史打出不稳)。style 决定措辞。"""
    if dir_cn == '平局' and dev < 0:
        if style == 'conservative':
            return _tag('yellow', '高偏平局·慎选', '偏差极大但ROI为0, 历史打出不稳, 不宜重注')
        if style == 'strategy':
            return _tag('red', '反买平局·小注博冷', f'-{adev:.0f}pp 强统计异常, 小注抓赔率价值')
        # aggressive / balanced
        if adev >= COLD_DEV:
            return _tag('red', '冷平预警', '平局跑输预期极深, 势均力敌局面, 典型冷平温床')
        return _tag('red', '平局陷阱', '平局跑输预期+平局风险高, 诱盘嫌疑')
    if dev < 0:
        if dir_cn == '客胜':
            if style == 'strategy':
                return _tag('red', '反买客胜·小注博冷', f'-{adev:.0f}pp 强统计异常, 小注反向搏价值')
            return _tag('yellow', '客胜诱盘', '客胜跑输预期, 市场热度偏客, 小心诱盘')
        if style == 'conservative':
            return _tag('yellow', '博冷区', '高偏差+ROI≈0, 可小注搏冷, 不宜重仓')
        if style == 'strategy':
            return _tag('red', '反买信号', f'-{adev:.0f}pp 强统计异常, 小注反向搏价值')
        return _tag('red', '冷门预警' if adev >= COLD_DEV else '庄家无视',
                    '庄家没防/不防这一路, 冷门或诱盘嫌疑')
    # dev > 0
    if style == 'conservative':
        return _tag('yellow', '博冷区', '跑赢预期但ROI≈0, 谨慎小注')
    return _tag('green', f'{dir_cn}价值', '跑赢预期, 方向被看好, 但ROI≈0需谨慎')


def compute_signal_label(direction, deviation_pct: Optional[float] = None,
                         roi: Optional[float] = None, style: str = 'balanced') -> dict:
    """计算三色情绪标签。

    direction: '主胜'/'平局'/'客胜' 或 H/D/A。
    deviation_pct: 有符号偏差(pp)。+=跑赢庄家预期, -=跑输庄家预期。
    roi: 真实下注 ROI(%); None 表示未知(按 ≈0 处理边界)。
    style: 'balanced'(默认) / 'aggressive' / 'conservative' / 'strategy'。
    """
    dir_cn = normalize_direction(direction)
    if dir_cn == 'PASS' or direction is None:
        return _tag('yellow', '无信号', '无明确最强信号方向, 观望')

    if deviation_pct is None:
        return _tag('yellow', '观察中', f'{dir_cn}方向信号, 偏差数据缺失, 观望')

    dev = float(deviation_pct)
    adev = abs(dev)
    roi_low = (roi is not None and roi < ROI_LOW)
    roi_zero = (roi is None or abs(roi) <= ROI_NEAR_ZERO)  # ≈0 或未知
    # roi_pos 在下方正偏差分支内联判断

    # A) 极端负 + ROI极低 → 反买信号 (red, 无歧义)
    if dev < 0 and adev >= EXTREME_DEV and roi_low:
        return _tag('red', '反买信号', '大众方向错了, 考虑反向操作搏价值')

    # B) 边界: 高偏差 + ROI≈0 (历史打出不稳) → style 决定措辞
    if adev >= HIGH_DEV and roi_zero:
        return _boundary(dir_cn, dev, adev, style)

    # C) 极端负 → 冷门/冷平 (red)
    if dev < 0 and adev >= COLD_DEV:
        if dir_cn == '平局':
            return _tag('red', '冷平预警', '平局跑输预期极深, 势均力敌局面, 典型冷平温床')
        return _tag('red', '冷门预警', '庄家没防, 市场在忽视, 冷门概率大')

    # D) 负 + 高(30-60, 未到冷门) → 诱盘/庄家无视 (red/yellow)
    if dev < 0 and adev >= HIGH_DEV:
        if dir_cn == '客胜':
            return _tag('yellow', '客胜诱盘', '客胜跑输预期, 市场热度偏客, 小心诱盘')
        if dir_cn == '平局':
            return _tag('red', '平局陷阱', '平局赔率高但庄家不慌, 诱盘嫌疑')
        return _tag('red', '庄家无视', '跑输预期且无资金支撑, 庄家根本不怕这路打出')

    # E) 正 + 高 → 价值/庄家防范 (green)
    if dev > 0 and adev >= HIGH_DEV:
        if adev >= EXTREME_DEV:
            return _tag('green', '庄家防范', '跑赢预期+资金集中, 庄家在防这一路')
        if roi is not None and roi > ROI_NEAR_ZERO:
            return _tag('green', f'{dir_cn}价值', '跑赢预期+ROI为正, 市场低估有肉')
        return _tag('green', f'{dir_cn}价值', '跑赢预期, 方向被看好')

    # F) 平局 + 低偏差 → 拉锯格局 (yellow)
    if dir_cn == '平局' and adev < HIGH_DEV:
        return _tag('yellow', '拉锯格局', '势均力敌, 谁都难赢, 容易走平')

    # G) 正 + 低 → 顺势跟进 (green)
    if dev > 0:
        return _tag('green', '顺势跟进', '偏差小+趋势一致, 数据资金基本面一条线')

    # H) 负 + 低 → 信号冲突 (yellow, 观望)
    return _tag('yellow', '信号冲突', '信号与基本面存疑, 观望')


def signed_deviation_from_freq(neighbor_freq: dict, market_prob: dict,
                                direction: Optional[str] = None) -> Optional[float]:
    """由「经验频率 vs 市场隐含概率」算有符号偏差(pp)。

    neighbor_freq / market_prob: {主胜/平局/客胜: 0..1}
    direction: 指定信号方向; 省略则取经验频率最高者作为"最强信号"。
    返回 (freq[dir] - market[dir]) * 100 (跑赢预期为正)。
    """
    dirs = ('主胜', '平局', '客胜')
    if not neighbor_freq or not market_prob:
        return None
    if direction is None:
        direction = max(dirs, key=lambda d: neighbor_freq.get(d, 0.0))
    nf = neighbor_freq.get(direction)
    mp = market_prob.get(direction)
    if nf is None or mp is None:
        return None
    return round((nf - mp) * 100.0, 2)


if __name__ == '__main__':
    # 自测: 覆盖用户三个速查表例子 + 边界
    cases = [
        ('平局', -60.4, 0.0, 'balanced'),   # → 🔴 冷平预警
        ('主胜', 30.0, None, 'balanced'),    # → 🟢 主胜价值
        ('客胜', -40.0, None, 'balanced'),   # → 🟡 客胜诱盘
        ('平局', -60.4, 0.0, 'conservative'),# → 🟡 高偏平局·慎选
        ('平局', -60.4, 0.0, 'strategy'),    # → 🔴 反买平局·小注博冷
        ('主胜', -45.0, -10.0, 'balanced'),  # → 🔴 庄家无视 (ROI低走A)
        ('平局', -50.0, 2.0, 'balanced'),    # → 🔴 平局陷阱
        ('平局', 10.0, None, 'balanced'),    # → 🟡 拉锯格局
        ('客胜', 35.0, 8.0, 'balanced'),     # → 🟢 客胜价值
        ('主胜', 5.0, None, 'balanced'),     # → 🟢 顺势跟进
    ]
    for d, dev, roi, st in cases:
        r = compute_signal_label(d, dev, roi, st)
        print(f"{d:>3} dev={dev:>6} roi={str(roi):>5} [{st:>11}] -> {r['tag']}  ({r['meaning']})")
