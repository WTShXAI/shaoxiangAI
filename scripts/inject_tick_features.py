#!/usr/bin/env python3
"""
哨响AI · tick特征注入器
==========================
在 shaoxiang_feature_library.db 的 features 表中追加 6 个 tick 特征列，
基于庄家定价模板白皮书 v1.0 的发现。

特征:
  ftick_1x2_winner   — 1X2赔率在1.0-1.49且尾数2或7 (信号: 更可能正确)
  ftick_1x2_trap     — 1X2赔率在1.0-1.49且尾数4 (信号: 庄家陷阱)
  ftick_ou_missing   — OU赔率在庄家禁区清单中 (49个从不使用的值)
  ftick_ou_standard  — OU赔率==1.71 (庄家标准定价点)
  ftick_ou_asym      — OU over/under不对称 (over偏爱=1, under偏爱=-1, 中性=0)
  ftick_gran         — 1X2刻度粒度 (0.01=0, 0.05=1, 0.1=2, 0.5=3, 1.0+=4)

幂等: 列已存在则跳过注入，可直接重跑。
"""

import sqlite3

DB = r'D:\Architecture\data\shaoxiang_feature_library.db'

# 庄家禁区: 137万条OU赔率中从未出现的49个0.01刻度
OU_MISSING_GRID = {
    1.00, 2.05, 2.15, 2.18, 2.22, 2.24, 2.27, 2.32, 2.34,
    2.37, 2.39, 2.41, 2.43, 2.45, 2.46, 2.48, 2.50, 2.52,
    2.54, 2.55, 2.57, 2.59, 2.60, 2.62, 2.64, 2.65, 2.67,
    2.68, 2.70, 2.71, 2.73, 2.74, 2.75, 2.76, 2.78, 2.79,
    2.80, 2.82, 2.84, 2.85, 2.86, 2.88, 2.89, 2.90, 2.92,
    2.93, 2.95, 2.97, 2.99
}

# over偏爱的值 (over使用率 > under使用率 + 0.2pp, 从138万条中统计)
OU_OVER_FAVORED = {1.88, 2.25, 2.26, 2.38, 2.40, 2.42, 2.49, 2.63}
# under偏爱的值 (under使用率 > over使用率 + 0.2pp)
OU_UNDER_FAVORED = {1.45, 1.47, 1.49, 1.56, 1.58, 1.59, 1.61, 1.65, 1.80, 1.95}


def tick_granularity(odds):
    """根据赔率值返回刻度粒度编码: 0.01=0, 0.05=1, 0.1=2, 0.5=3, 1.0+=4"""
    if odds < 1.5:
        return 0  # 0.01精度区
    elif odds < 2.0:
        return 1  # 混合精度(0.01/0.02/0.03/0.05)
    elif odds < 3.0:
        return 2  # 0.05精度区
    elif odds < 5.0:
        return 3  # 0.1精度区
    else:
        return 4  # 0.5+精度区


def compute_features(row):
    """从一行特征计算6个tick特征"""
    ft = {}

    # === 1X2 tick特征 ===
    def x1_last_digit(odds):
        if odds is None: return None
        return int(round(odds * 100)) % 10

    for key, fav in [('x1_h', 'h'), ('x1_d', 'd'), ('x1_a', 'a')]:
        o = row.get(key)
        if o is None:
            continue
        d = x1_last_digit(o)
        in_range = 1.0 <= o < 1.5

        # 找到正确的选项是哪个维度
        is_winner = in_range and d in (2, 7)
        is_trap = in_range and d == 4

    # 用最低赔率的那档（庄家最看好）来算——因为标签未知时只有这个方向有意义
    odds_list = [(row.get('x1_h'), 'h'), (row.get('x1_d'), 'd'), (row.get('x1_a'), 'a')]
    odds_list = [(o, k) for o, k in odds_list if o is not None]
    if odds_list:
        best_odds, best_key = min(odds_list, key=lambda x: x[0])
        d = x1_last_digit(best_odds)
        ft['ftick_1x2_winner'] = 1 if (1.0 <= best_odds < 1.5 and d in (2, 7)) else 0
        ft['ftick_1x2_trap'] = 1 if (1.0 <= best_odds < 1.5 and d == 4) else 0
        ft['ftick_gran'] = tick_granularity(best_odds)
    else:
        ft['ftick_1x2_winner'] = 0
        ft['ftick_1x2_trap'] = 0
        ft['ftick_gran'] = -1

    # === OU tick特征 ===
    ou_over = row.get('xou_over')
    ou_under = row.get('xou_under')
    if ou_over is not None and ou_under is not None:
        ou_rounded = round(ou_over, 2)
        ft['ftick_ou_missing'] = 1 if ou_rounded in OU_MISSING_GRID else 0
        ft['ftick_ou_standard'] = 1 if ou_rounded == 1.71 else 0

        if ou_rounded in OU_OVER_FAVORED:
            ft['ftick_ou_asym'] = 1
        elif ou_rounded in OU_UNDER_FAVORED:
            ft['ftick_ou_asym'] = -1
        else:
            ft['ftick_ou_asym'] = 0
    else:
        ft['ftick_ou_missing'] = 0
        ft['ftick_ou_standard'] = 0
        ft['ftick_ou_asym'] = 0

    return ft


def main():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

    # 检查列是否已存在
    existing = [r[1] for r in c.execute("PRAGMA table_info(features)")]
    new_cols = ['ftick_1x2_winner', 'ftick_1x2_trap', 'ftick_ou_missing',
                'ftick_ou_standard', 'ftick_ou_asym', 'ftick_gran']
    to_add = [col for col in new_cols if col not in existing]

    if not to_add:
        print('tick特征已存在，跳过注入。')
        c.close()
        return 0

    print(f'注入 {len(to_add)} 个tick特征: {to_add}')

    # 添加列
    for col in to_add:
        c.execute(f'ALTER TABLE features ADD COLUMN {col} INTEGER DEFAULT 0')

    # 计算并更新
    rows = [dict(r) for r in c.execute('SELECT * FROM features')]
    updated = 0
    for r in rows:
        ft = compute_features(r)
        sets = ', '.join(f'{k}={ft[k]}' for k in to_add)
        c.execute(f'UPDATE features SET {sets} WHERE id=?', (r['id'],))
        updated += 1

    c.commit()
    print(f'已更新 {updated} 行')
    print(f'新特征列: {[k for k in new_cols if k in existing or k in to_add]}')
    c.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
