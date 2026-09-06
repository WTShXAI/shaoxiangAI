#!/usr/bin/env python3
"""
ingest_user_bets.py — 把用户下注历史(表格粘贴文本 / 文件) 解析入 events.db.user_bets。
"只记录输赢": 过滤掉 投注失败 + 走水(pnl=0), 仅保留实际输赢盘口作为模型特征。

用法:
  # 从文件
  python scripts/ingest_user_bets.py data/user_bets_raw.txt
  # 从 stdin (bash heredoc)
  python scripts/ingest_user_bets.py < data/user_bets_raw.txt
"""
import re, sqlite3, sys, os

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'events.db')


def parse(text: str):
    """按块切(每条编号开头), 逐块正则提取字段. 块大小 6-15 行, 容忍空白."""
    text = text.replace('\r\n', '\n').strip()
    blocks = re.split(r'\n(?=\s*\d+\s)', text)
    rows = []
    for b in blocks:
        b = b.strip()
        if not b: continue
        m = re.search(r'^(\d+)\s', b)
        if not m: continue
        no = int(m.group(1))
        bet_time = (re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\}', b) or [None, ''])[1]
        # 玩法: "滚球   全场大小" / "赛前   全场波胆" / "滚球   加时赛-波胆"
        pm = re.search(r'(滚球|赛前)\s+(.+?)\n', b)
        phase = pm.group(1) if pm else ''
        mtype = pm.group(2).strip() if pm else ''
        is_half = 1 if '上半场' in mtype or '半场' in mtype else (2 if '加时' in mtype else 0)
        # 联赛: "[足球] 亚美尼亚精英联赛" / "[篮球] WNBA ..."
        lm = re.search(r'\[[^\]]+\]\s+(.+?)\n', b)
        league = lm.group(1).strip() if lm else ''
        # 比赛: "先锋FC VS 风暴战士FC"
        mm = re.search(r'(.+?)\s+VS\s+(.+?)\n', b)
        home = mm.group(1).strip() if mm else ''
        away = mm.group(2).strip() if mm else ''
        match_key = f'{home} vs {away}' if home else None
        # 选项 + 赔率: 两步法(避开贪婪跨行). 先找 @数字 → 反向取最近非空行.
        odds_m = re.search(r'@(\d+(?:\.\d+)?)', b)
        odds = float(odds_m.group(1)) if odds_m else None
        if odds_m:
            pos = odds_m.start()
            pre = b[:pos].rstrip()
            last_nl = pre.rfind('\n')
            selection = pre[last_nl + 1:].strip() if last_nl >= 0 else pre.strip()
        else:
            selection = ''
        # 下注时比分: "下注时比分 (0-1)"
        osm = re.search(r'下注时比分\s+\(([^)]+)\)', b)
        open_score = osm.group(1).strip() if osm else ''
        # 结果比分: "结果比分 (全场比分 5-4)" / "结果比分 (上半场比分 1-0)"
        fsm = re.search(r'结果比分\s+\(([^)]+)\)', b)
        final_score = fsm.group(1).strip() if fsm else ''
        # 投注额 / 净输赢 / 返还金额: 三列(可能含-号)
        nm = re.search(r'\n\s*(\d+(?:\.\d+1?))\s+(-?\d+(?:\.\d+1?))\s+(-?\d+(?:\.\d+1?))\s*\n', b)
        if nm:
            stake = float(nm.group(1)); pnl = float(nm.group(2)); refund = float(nm.group(3))
        else:
            stake = pnl = refund = 0.0
        # 状态
        stm = re.search(r'(投注成功|投注失败|全部提前兑现成功|走水)', b)
        status = stm.group(1) if stm else ''
        result = 'win' if pnl > 0 else ('loss' if pnl < 0 else 'push')
        rows.append(dict(
            bet_no=no, bet_time=bet_time, match_key=match_key,
            league=league, market_type=mtype, phase=phase, is_half=is_half,
            selection=selection, odds=odds, open_score=open_score,
            final_score=final_score, stake=stake, pnl=pnl, status=status,
            result=result, raw=b,
        ))
    return rows


def keep_betting_outcome(rows):
    """只记录输赢的盘口(过滤 投注失败 + 走水/pnl=0)."""
    return [r for r in rows if r['status'] == '投注成功' and r['pnl'] != 0]


def insert_db(rows):
    c = sqlite3.connect(DB, timeout=30)
    n = 0
    for r in rows:
        try:
            c.execute("""INSERT OR IGNORE INTO user_bets
              (bet_no, bet_time, match_key, league, market_type, phase, is_half,
               selection, odds, open_score, final_score, stake, pnl, status, result, raw)
              VALUES
              (:bet_no,:bet_time,:match_key,:league,:market_type,:phase,:is_half,
               :selection,:odds,:open_score,:final_score,:stake,:pnl,:status,:result,:raw)""", r)
            n += 1
        except Exception as e:
            print(f'  insert err bet_no={r["bet_no"]}: {e}')
    c.commit(); c.close()
    return n


if __name__ == '__main__':
    src = sys.argv[1] if len(sys.argv) > 1 else None
    text = open(src, encoding='utf-8').read() if src else sys.stdin.read()
    if not text.strip():
        print('空输入. 用法: python scripts/ingest_user_bets.py <file>  或  <stdin>')
        sys.exit(1)
    rows = parse(text)
    keep = keep_betting_outcome(rows)
    drop = [r for r in rows if r not in keep]
    print(f'parsed={len(rows)} keep(只输赢)={len(keep)} drop(走水/投注失败)={len(drop)}')
    if drop:
        for d in drop[:5]:
            print(f'  drop no={d["bet_no"]} status={d["status"]} pnl={d["pnl"]}')
    if not keep:
        print('无"只输赢"数据可入库.')
        sys.exit(0)
    n = insert_db(keep)
    print(f'inserted={n} into {DB} (user_bets)')
    # 简单汇总
    total_stake = sum(r['stake'] for r in keep)
    total_pnl = sum(r['pnl'] for r in keep)
    wins = [r for r in keep if r['result']=='win']
    losses = [r for r in keep if r['result']=='loss']
    print(f'汇总: 单数={len(keep)} 投注额={total_stake:.2f} 净输赢={total_pnl:.2f} ROI={total_pnl/total_stake*100:.1f}% 赢={len(wins)} 输={len(losses)} 命中率={len(wins)/len(keep)*100:.1f}%')