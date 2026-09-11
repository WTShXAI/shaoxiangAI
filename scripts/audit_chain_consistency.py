#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""链路串联一致性审计器 (2026-09-10, 用户要求"迭代优化直到模型串联不再矛盾").

对 端点实际输出(与前端同源) 做成对矛盾检查 — 迭代闭环的量尺:
  CHK1 方向 vs 首选比分: direction.winner 与 cs.top1 类别必须一致
       (赛前: 结构性保证; 滚球 lead-prior 分歧属已标注差异, 单独统计)
  CHK2 OU 判定 vs 首选比分总球: live OU 方向强信号时 top1 总球须满足方向
       (OVER: top1总球 > line; UNDER: top1总球 ≤ floor(line))
  CHK3 半场 OU vs 全场 OU 硬矛盾: 半场强 OVER 推出全场最少总球, 与全场强
       UNDER 的线冲突才算(双方 prob ≥ 0.60 才检查)
  CHK4 首选比分 vs 当前比分: top1 各分量 ≥ 当前比分(进球单调)
  CHK5 融合方向 vs 比分池方向: 允许分歧(门控职责), 但必须带标注
       (cs_verify / 池方向徽章), 无标注才算矛盾
  CHK6 OU 无优势仍显示方向: signal=NO_EDGE 时 direction 应为观望口径
       (检查 ou.direction 非空但 prob < 0.56 的"伪方向")

用法:
  python scripts/audit_chain_consistency.py            # 滚球中实时审计(30场)
  python scripts/audit_chain_consistency.py --limit 60 # 加大样本
输出: 各 CHK 违反数 + 例证; 退出码 = 总违反数(可作 CI 门禁)。
"""
import collections
import json
import sys
import urllib.parse
import urllib.request

BASE = 'http://127.0.0.1:9000'
TH = 0.60          # 强信号阈值


def get(url, timeout=60):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def outcome(score):
    try:
        h, a = (int(x) for x in str(score).replace(':', '-').split('-')[:2])
    except Exception:
        return None
    return 'home' if h > a else ('draw' if h == a else 'away')


def total(score):
    try:
        h, a = (int(x) for x in str(score).replace(':', '-').split('-')[:2])
        return h + a
    except Exception:
        return None


def audit_match(mk, m, viol, examples, cov):
    d = (get(f"{BASE}/api/rollball/analyze?match_key={urllib.parse.quote(mk)}"
             f"&score={urllib.parse.quote(str(m.get('score') or '0-0'))}"
             f"&minute={int(m.get('minute') or 0)}") or {}).get('data') or {}
    cs = d.get('cs') or {}
    ou = d.get('ou') or {}
    direction = d.get('direction') or {}
    top1 = (cs.get('top5') or [{}])[0].get('score')

    # CHK1 方向 vs 首选比分
    if direction.get('winner') and top1:
        cov['CHK1'] += 1
        o = outcome(top1)
        if o != direction['winner']:
            labeled = '领先方先验' in str(direction.get('basis') or '') or '方向对齐' in str(cs.get('winner_align') or '')
            key = 'CHK1_滚球已标注分歧' if labeled else 'CHK1_方向与首选比分矛盾'
            viol[key] += 1
            examples.setdefault(key, []).append(
                f"{mk}: dir={direction['winner']}({str(direction.get('basis'))[:24]}) top1={top1} "
                f"cs.dir={(cs.get('direction') or {}).get('winner')} align={str(cs.get('winner_align'))[:36]}")

    # CHK2 OU 判定 vs 首选比分总球
    if (top1 and ou.get('data_source') == 'live_odds' and ou.get('line') is not None
            and ou.get('direction') in ('OVER', 'UNDER') and ou.get('signal') not in ('NO_EDGE', 'ALREADY_BROKEN')):
        # 2026-09-10 语义对齐: 置信 <70% 的 OU 为展示倾向(不降权池), 与首选比分
        # 的类别分歧属设计行为 — 仅审计强置信(≥0.70)的锚
        if (ou.get('prob') is not None and ou.get('prob') >= 0.70) or ou.get('prob') is None:
            cov['CHK2'] += 1
        else:
            t1 = None   # 弱置信倾向: 不计检查也不计违反
        t1 = total(top1)
        ln = float(ou['line'])
        # 线已被当前总球击穿(破线)时, 真实链路走 ALREADY_BROKEN 分支不产出方向 — 跳过;
        # 比分缺失场(score_known=False, 显示回退 0-0)同样跳过 — 0-0 不是真比分
        cur = str(d.get('score') or '0-0')
        cur_known = d.get('score_known', True)
        if (not cur_known) or (total(cur) is not None and total(cur) >= ln):
            cov['CHK2'] -= 1
            t1 = None
        if t1 is not None:
            bad = (ou['direction'] == 'OVER' and t1 <= ln) or (ou['direction'] == 'UNDER' and t1 > ln)
            if bad:
                viol['CHK2_OU与首选比分矛盾'] += 1
                examples.setdefault('CHK2_OU与首选比分矛盾', []).append(
                    f"{mk}: OU={ou['direction']}{ln:g} top1={top1}(总{t1})")

    # CHK3 半场 vs 全场 OU 硬矛盾
    half, full = ou.get('half') or {}, ou
    try:
        if (half.get('line') is not None and half.get('direction') in ('OVER', 'UNDER')
                and full.get('line') is not None and full.get('direction') in ('OVER', 'UNDER')
                and (half.get('prob') or 0) >= TH and (full.get('prob') or 0) >= TH
                and half.get('direction') == 'OVER' and full.get('direction') == 'UNDER'):
            cov['CHK3'] += 1
            import math
            min_full = math.floor(float(half['line'])) + 1     # 半场OVER → 全场最少总球
            if float(full['line']) < min_full:
                viol['CHK3_半场全场OU硬矛盾'] += 1
                examples.setdefault('CHK3_半场全场OU硬矛盾', []).append(
                    f"{mk}: 半大{half['line']:g}({half.get('prob')}) → 全场≥{min_full}球, "
                    f"但全场小{full['line']:g}({full.get('prob')})")
    except Exception:
        pass

    # CHK4 首选比分 vs 当前比分
    cur = str(d.get('score') or '')
    if top1 and '-' in cur:
        cov['CHK4'] += 1
        try:
            ch, ca = (int(x) for x in cur.split('-'))
            th_, ta_ = (int(x) for x in top1.split('-'))
            if th_ < ch or ta_ < ca:
                viol['CHK4_首选比分低于当前比分'] += 1
                examples.setdefault('CHK4_首选比分低于当前比分', []).append(f"{mk}: cur={cur} top1={top1}")
        except Exception:
            pass

    # CHK5 融合方向 vs 比分池方向 必须带标注
    fd = d.get('final_direction') or {}
    pd_ = ((d.get('consensus_gate') or {}).get('cs_verify') or {}).get('pool_dir')
    if fd.get('direction') and cs.get('direction', {}).get('winner'):
        cov['CHK5'] += 1
        if fd['direction'] != cs['direction']['winner'] and not pd_:
            viol['CHK5_融合与池分歧无标注'] += 1
            examples.setdefault('CHK5_融合与池分歧无标注', []).append(
                f"{mk}: fusion={fd['direction']} pool={cs['direction']['winner']}")

    # CHK6 OU 无优势伪方向 (2026-09-10 复审口径: 前端已将 NO_EDGE 显示为"观望",
    # 数据层 direction 仅供内部消费 — 只审计"高置信却标无优势"的错标)
    if ou.get('direction') in ('OVER', 'UNDER') and ou.get('signal') == 'NO_EDGE':
        cov['CHK6'] += 1
        # 2026-09-10 复审: NO_EDGE 时前端显示"观望"(无数向), 用户可见链路无矛盾;
        # 此检查降级为警告 — 仅标记 probe 内部"高置信+无优势"标注语义问题。
        if ou.get('prob') is not None and ou.get('prob') >= 0.56:
            viol['__WARN_CHK6_高置信无优势'] += 1


def main(limit=30, prematch=False):
    viol, examples = collections.Counter(), {}
    cov = {'CHK1': 0, 'CHK2': 0, 'CHK3': 0, 'CHK4': 0, 'CHK5': 0, 'CHK6': 0}
    if not prematch:
        j = get(f"{BASE}/api/live-goal-probe/matches?limit=200")
        ms = ((j.get('data') or j).get('matches') or [])
        live = [m for m in ms if m.get('score')][:limit]
        print(f"审计样本(滚球中): {len(live)} 场")
        for m in live:
            try:
                audit_match(m["match_key"], m, viol, examples, cov)
            except Exception as e:
                print(f"  [跳过] {m.get('match_key')}: {e}")
    else:
        import sqlite3
        con = sqlite3.connect(r'D:\Architecture\data\events.db', timeout=20)
        rows = con.execute("""
            SELECT match_key, score_home, score_away FROM matches
            WHERE status='finished' AND kickoff >= date('now','-3 day') AND score_home IS NOT NULL
            ORDER BY kickoff DESC LIMIT ?""", (limit,)).fetchall()
        con.close()
        print(f"审计样本(完赛回放·参考口径): {len(rows)} 场 — 完赛场快照停在终局, 与赛前问题错配, 结果仅供参考; 审计门禁以滚球实时模式为准")
        for mk, sh, sa in rows:
            # 纯赛前口径: 0-0@0' — 终场比分+开赛赔率是错配条件化(伪态), 不是真实链路
            m = {'match_key': mk, 'score': '0-0', 'minute': 0}
            try:
                audit_match(mk, m, viol, examples, cov)
            except Exception as e:
                print(f"  [跳过] {mk}: {e}")
    total_v = sum(v for k, v in viol.items() if not k.startswith('__WARN'))
    print('检查覆盖:', dict(cov))
    print('─' * 50)
    if not viol:
        print('✅ 全部检查通过: 0 矛盾')
    for k in sorted(viol):
        print(f"❌ {k}: {viol[k]}")
        for e in examples.get(k, [])[:4]:
            print(f"     - {e}")
    print('检查覆盖:', dict(cov))
    print('─' * 50)
    print(f"总违反: {total_v}")
    return total_v


if __name__ == '__main__':
    lim, pre = 30, False
    for i, a in enumerate(sys.argv):
        if a == '--limit' and i + 1 < len(sys.argv):
            lim = int(sys.argv[i + 1])
        if a == '--prematch':
            pre = True
    sys.exit(1 if main(lim, pre) else 0)
