"""Backfill ou_live_feed results from ESPN public soccer scoreboard (no API key, no credit cost).
This is a SEPARATE helper for the OU forward-validation; it does NOT modify capture_ou_results.py.
Strategy:
  - For each due event, map sport_key -> ESPN league code(s) (with candidate fallbacks).
  - Fetch ESPN scoreboard for (league, date) and date-1/+1 to absorb TZ-boundary drift.
  - Match by normalized home/away team names (contains-relation, both sides must hold).
  - Backfill total_goals/settle/model_direction/market_direction/model_hit/market_hit, result='done'.
  - Then run the identical validation block as capture_ou_results.py and write data/ou_forward_validation.json.
"""
import sqlite3, urllib.request, json, os, sys, datetime, unicodedata
from collections import defaultdict

DB = r"D:\Architecture\data\football_data.db"
OUT = r"D:\Architecture\data\ou_forward_validation.json"

# OU direction primitives (SSoT, same as capture script)
from pipeline.evaluation.ou_eval import ou_settle as settle, grade_direction as model_direction

# sport_key -> candidate ESPN league codes (first that yields data wins)
LEAGUE_MAP = {
    'soccer_argentina_primera_division': ['arg.1'],
    'soccer_brazil_campeonato': ['bra.1'],
    'soccer_brazil_serie_b': ['bra.2'],
    'soccer_chile_campeonato': ['chi.1'],
    'soccer_china_superleague': ['chn.1'],
    'soccer_conmebol_copa_sudamericana': ['conmebol.sudamericana', 'sudamericana', 'sudamerica.1'],
    'soccer_denmark_superliga': ['den.1'],
    'soccer_finland_veikkausliiga': ['fin.1', 'fin.2'],
    'soccer_korea_kleague1': ['kor.1', 'kor.2', 'korea.1'],
    'soccer_league_of_ireland': ['irl.1', 'irl.2'],
    'soccer_mexico_ligamx': ['mex.1'],
    'soccer_norway_eliteserien': ['nor.1'],
    'soccer_poland_ekstraklasa': ['pol.1', 'pol.2', 'poland.1'],
    'soccer_sweden_allsvenskan': ['swe.1'],
    'soccer_sweden_superettan': ['swe.2', 'swe.3'],
    'soccer_switzerland_superleague': ['sui.1', 'sui.2', 'switzerland.1'],
    'soccer_usa_mls': ['usa.1'],
}

UA = {'User-Agent': 'shaoxiang/1.0'}


def norm(s):
    if not s:
        return ''
    s = unicodedata.normalize('NFKD', str(s)).encode('ascii', 'ignore').decode()
    s = s.lower()
    out = []
    for ch in s:
        if ch.isalnum():
            out.append(ch)
        elif ch == ' ':
            out.append(' ')
    return ' '.join(''.join(out).split())


def fetch_scoreboard(league, ymd):
    url = f'https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard?dates={ymd}'
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def parse_events(data):
    """Return list of dicts: {home, away, home_score, away_score, completed} with normalized names."""
    out = []
    if not data:
        return out
    for e in data.get('events', []):
        try:
            comp = e['competitions'][0]
            cs = comp['competitors']
            home = next(x for x in cs if x['homeAway'] == 'home')
            away = next(x for x in cs if x['homeAway'] == 'away')
            hn = home['team'].get('displayName') or home['team'].get('name', '')
            an = away['team'].get('displayName') or away['team'].get('name', '')
            hs = home.get('score')
            aw = away.get('score')
            completed = bool(e.get('status', {}).get('type', {}).get('completed'))
            if hs is None or aw is None:
                continue
            out.append({'home': norm(hn), 'away': norm(an),
                        'total': int(hs) + int(aw), 'completed': completed})
        except Exception:
            continue
    return out


def get_events_for(league_codes, ymd):
    """Try candidate codes, return merged events (dedup by normalized pair)."""
    merged = {}
    for lg in league_codes:
        d = fetch_scoreboard(lg, ymd)
        for ev in parse_events(d):
            key = (ev['home'], ev['away'])
            if key not in merged:
                merged[key] = ev
    return list(merged.values())


def _toks(s):
    return set(norm(s).split())


def _tok_overlap(a, b):
    """Normalized token overlap = |A∩B| / min(|A|,|B|). 1.0 == identical token set."""
    ta, tb = _toks(a), _toks(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def match(events, h, a, min_overlap=0.5):
    H, A = norm(h), norm(a)
    # 1) exact contains (original logic — fast & precise)
    for ev in events:
        # direct or swapped, both sides must hold via contains
        if (H in ev['home'] or ev['home'] in H) and (A in ev['away'] or ev['away'] in A):
            return ev
        if (H in ev['away'] or ev['away'] in H) and (A in ev['home'] or ev['home'] in A):
            return ev
    # 2) token-overlap fallback (handles abbreviation/spelling diffs:
    #    Athletico/Atletico, Ulsan Hyundai FC/Ulsan HD, Bucheon FC 1995/Bucheon FC)
    best, bestsc = None, 0.0
    for ev in events:
        sc = max(_tok_overlap(h, ev['home']) * 0.5 + _tok_overlap(a, ev['away']) * 0.5,
                 _tok_overlap(h, ev['away']) * 0.5 + _tok_overlap(a, ev['home']) * 0.5)
        if sc > bestsc:
            best, bestsc = ev, sc
    if bestsc >= min_overlap:
        return best
    return None


def main():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    for col, typ in [('model_direction', 'TEXT'), ('market_direction', 'TEXT'),
                     ('model_hit', 'INTEGER'), ('market_hit', 'INTEGER')]:
        try:
            con.execute(f"ALTER TABLE ou_live_feed ADD COLUMN {col} {typ}")
        except Exception:
            pass

    REDERIVE = '--rederive' in sys.argv
    if REDERIVE:
        # 用修正后的市场感知模型重新推导已结算行 (model_direction/model_hit),
        # 使库内存储与 SSoT ou_eval.grade_direction 一致。不影响 total_goals/settle。
        n = 0
        for r in con.execute("SELECT id, ou_point, over_odds, under_odds, total_goals, settle FROM ou_live_feed WHERE result='done'"):
            mdir, _ = model_direction(r['ou_point'], r['over_odds'], r['under_odds'])
            st = r['settle'] or settle(r['total_goals'], r['ou_point'])
            mhit = 1 if (mdir in ('OVER', 'UNDER') and mdir == st) else 0
            mktd = 'OVER' if r['over_odds'] < r['under_odds'] else 'UNDER'
            khit = 1 if mktd == st else 0
            con.execute("""UPDATE ou_live_feed SET model_direction=?, model_hit=?, market_direction=?, market_hit=?
                WHERE id=?""", (mdir, mhit, mktd, khit, r['id']))
            n += 1
        con.commit()
        print(f"[rederive] updated {n} done rows with market-aware model")

    now = datetime.datetime.now(datetime.timezone.utc)
    due = []
    for r in con.execute("SELECT * FROM ou_live_feed WHERE result IS NULL OR result='pending_retry'"):
        ct = r['commence_time'].replace('Z', '+00:00')
        try:
            ct = datetime.datetime.fromisoformat(ct)
        except Exception:
            continue
        if ct < now - datetime.timedelta(hours=2):
            due.append(r)
    print(f"due events (past >2h, no result): {len(due)}")

    # cache per (sport_key, ymd)
    cache = {}
    def events_for(sport_key, ymd):
        k = (sport_key, ymd)
        if k not in cache:
            codes = LEAGUE_MAP.get(sport_key, [])
            cache[k] = get_events_for(codes, ymd)
        return cache[k]

    captured = 0
    unmatched = 0
    unm_list = []
    for r in due:
        ct = datetime.datetime.fromisoformat(r['commence_time'].replace('Z', '+00:00'))
        base = ct.date()
        # pool 3 days to absorb TZ drift
        pool = []
        for off in (-1, 0, 1):
            d = (base + datetime.timedelta(days=off)).strftime('%Y%m%d')
            pool.extend(events_for(r['sport_key'], d))
        ev = match(pool, r['home_team'], r['away_team'])
        if not ev or not ev['completed']:
            unmatched += 1
            unm_list.append((r['sport_key'], r['home_team'], r['away_team'], r['commence_time'][:10]))
            continue
        total = ev['total']
        mdir, _ = model_direction(r['ou_point'], r['over_odds'], r['under_odds'])
        st = settle(total, r['ou_point'])
        mhit = 1 if (mdir in ('OVER', 'UNDER') and mdir == st) else 0
        ov, un = r['over_odds'], r['under_odds']
        mktd = 'OVER' if ov < un else 'UNDER'
        khit = 1 if mktd == st else 0
        con.execute("""UPDATE ou_live_feed SET result='done', total_goals=?, settle=?,
            model_direction=?, market_direction=?, model_hit=?, market_hit=? WHERE id=?""",
            (total, st, mdir, mktd, mhit, khit, r['id']))
        captured += 1
    con.commit()
    print(f"backfilled results for {captured} events this run ({unmatched} unmatched / no ESPN data)")

    # ---- validation over all done (identical to capture script) ----
    rows = [dict(r) for r in con.execute("SELECT * FROM ou_live_feed WHERE result='done'").fetchall()]
    n = len(rows)
    print(f"\n=== OU direction validation (n_done={n}) ===")
    if n:
        mh = sum(r['model_hit'] for r in rows)
        kh = sum(r['market_hit'] for r in rows)
        print(f"  model_hit={100*mh/n:.1f}%  market_fav_hit={100*kh/n:.1f}%")
        over_all = sum(1 for r in rows if r['settle'] == 'OVER')
        under_all = n - over_all
        print(f"  actual settle: OVER={100*over_all/n:.1f}%  UNDER={100*under_all/n:.1f}%")
        for r in rows:
            _, r['_grade'] = model_direction(r['ou_point'])
        trap = [r for r in rows if 'trap' in r['_grade']]
        hon = [r for r in rows if 'trap' not in r['_grade'] and r['model_direction'] != 'NEUTRAL']
        if trap:
            print(f"  trap lines: n={len(trap)} model_hit={100*sum(r['model_hit'] for r in trap)/len(trap):.1f}%")
        if hon:
            print(f"  honest lines: n={len(hon)} model_hit={100*sum(r['model_hit'] for r in hon)/len(hon):.1f}%")
        by = defaultdict(list)
        for r in rows:
            by[r['ou_point']].append(r)
        for ln in sorted(by):
            sub = by[ln]
            sn = len(sub)
            sm = sum(x['model_hit'] for x in sub)
            print(f"    line {ln}: n={sn} model_hit={100*sm/sn:.1f}%")
        rep = {"n": n, "model_hit": round(100*mh/n, 2), "market_fav_hit": round(100*kh/n, 2),
               "over_pct": round(100*over_all/n, 2), "trap_n": len(trap),
               "trap_model_hit": round(100*sum(r['model_hit'] for r in trap)/len(trap), 2) if trap else None,
               "honest_n": len(hon),
               "honest_model_hit": round(100*sum(r['model_hit'] for r in hon)/len(hon), 2) if hon else None,
               "source": "espn_web_backfill"}
        with open(OUT, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=2)
        print(f"  wrote {OUT}")
        if unm_list:
            print(f"\n--- unmatched ({len(unm_list)}) ---")
            for u in unm_list[:30]:
                print("  ", u)
    else:
        print("  (no results captured yet)")
    con.close()


if __name__ == '__main__':
    main()
