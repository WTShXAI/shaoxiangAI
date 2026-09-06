import json, re

# Parse the 12 tonight bets from the captured snapshot
snap = json.load(open(r'D:/Architecture/analysis/betrec_snap.json', encoding='utf-8'))
refs = snap['data']['refs']
cells = [v.get('name','') for v in refs.values() if v.get('role')=='cell']

def parse_detail(d):
    m = re.search(r'\[足球\]\s*(.+?)\s+(.*?) @([\d.]+)\s+(滚球|赛前)全场(波胆|大小|让球)', d)
    league = m.group(1) if m else None
    pick = m.group(2) if m else None
    odds = float(m.group(3)) if m else None
    live = m.group(3) if m else None
    play = (m.group(4)+m.group(5)) if m else None
    # teams + match time
    tm = re.search(r'(.+?) VS (.+?)\s+(\d{4}-\d\d-\d\d \d\d:\d\d)', d)
    home, away, mtime = (tm.group(1), tm.group(2), tm.group(3)) if tm else (None,None,None)
    # live score at bet
    ls = re.search(r'下注时比分 \(([\d-]+)\)', d)
    live_score = ls.group(1) if ls else None
    # cashout
    co = re.search(r'提前兑现 ([\d.]+) \(含本金\)', d)
    cashout = float(co.group(1)) if co else None
    return dict(league=league, pick=pick, odds=odds, play=play, home=home, away=away,
                match_time=mtime, live_score_at_bet=live_score, cashout=cashout)

# Each bet = 10 cells: [status][idx][datetime+betid][play][detail][stake][maxwin]... but pattern varies.
# Reconstruct by scanning for datetime cells as anchors.
bets = []
i = 0
text_cells = [c for c in cells if c.strip()]
# Bet blocks start with '投注成功' then idx then datetime... Find datetime anchors
pat = re.compile(r'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\(GMT\+8\) (\d+)')
idxs = [k for k,c in enumerate(text_cells) if pat.search(c)]
for k in idxs:
    dt_betid = text_cells[k]
    dm = re.match(r'(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\(GMT\+8\) (\d+)', dt_betid)
    dt = dm.group(1); betid = dm.group(2)
    play = text_cells[k+1] if k+1 < len(text_cells) else ''
    detail = text_cells[k+2] if k+2 < len(text_cells) else ''
    # stake = first number after detail that is not part of detail; maxwin next
    # find numeric cells after k+2
    nums = []
    j = k+3
    while j < len(text_cells) and len(nums) < 2:
        c = text_cells[j]
        if re.match(r'^[\d.]+$', c):
            nums.append(float(c))
        j += 1
    stake = nums[0] if len(nums) > 0 else None
    maxwin = nums[1] if len(nums) > 1 else None
    p = parse_detail(detail)
    p.pop('play', None)  # avoid collision with outer play
    bets.append(dict(dt=dt, betid=betid, play=play, stake=stake, max_win=maxwin, **p))

total_stake = sum(b['stake'] for b in bets if b['stake'])
print(f'Parsed {len(bets)} bets, total stake = {total_stake:.2f}')
print()
for b in bets:
    co = b.pop('cashout')
    print(f"{b['dt'][11:16]} | {b['play']} | {b['league']} | {b['pick']}@{b['odds']} | live {b['live_score_at_bet']} | stake {b['stake']} | maxwin {b['max_win']} | cashout {co}")
    # store cashout back
    b['cashout'] = co

# Known cashout P&L (cashout 含本金 => net = cashout - stake)
cashed = [b for b in bets if b['cashout'] is not None]
openb = [b for b in bets if b['cashout'] is None]
net_cash = sum(b['cashout'] - b['stake'] for b in cashed)
print(f'\nCashed-out bets: {len(cashed)} | net from cashouts (含本金) = {net_cash:+.2f}')
print(f'Open bets (no cashout, pending final): {len(openb)}')
for b in openb:
    print(f"  {b['dt'][11:16]} {b['league']} {b['pick']}@{b['odds']} stake {b['stake']} maxwin {b['max_win']}")

json.dump(bets, open(r'D:/Architecture/analysis/tonight_real_bets.json','w',encoding='utf-8'), ensure_ascii=False, indent=2)
print('\nSaved -> D:/Architecture/analysis/tonight_real_bets.json')
