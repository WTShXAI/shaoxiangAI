"""
聊天 SSE 端点 — 路由归一至 api/v1/endpoints/ (2026-06-28)
=========================================================
迁移说明: 原 backend/routers/chat.py → 统一至 api/v1/endpoints/
"""
import json, asyncio, re, sys, os, logging
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse, JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

# ── 延迟导入 (避免循环依赖) ──
_build_bookmaker_report = None
_build_bookmaker_card = None
_build_analysis_card = None
_detect_match_type = None
_apply_dgate = None
_get_fifa_rank_diff = None

def _init_deps():
    global _build_bookmaker_report, _build_bookmaker_card, _build_analysis_card
    global _detect_match_type, _apply_dgate, _get_fifa_rank_diff
    if _build_bookmaker_report is None:
        from backend.services.bookmaker_reports import build_bookmaker_report, build_bookmaker_card, build_analysis_card
        _build_bookmaker_report = build_bookmaker_report
        _build_bookmaker_card = build_bookmaker_card
        _build_analysis_card = build_analysis_card
    if _detect_match_type is None:
        from rules.d_gate_engine import detect_match_type as _detect_match_type
    if _apply_dgate is None:
        from rules.d_gate_engine import apply_dgate as _apply_dgate
    if _get_fifa_rank_diff is None:
        # 从 main 模块获取 (FIFA rankings 在该模块加载)
        import backend.main as _bm
        _get_fifa_rank_diff = getattr(_bm, '_get_fifa_rank_diff', lambda h, a: None)

@router.post("/chat")
async def chat_endpoint(request: Request):
    """文本对话 — SSE流式"""
    _init_deps()
    try:
        raw_body = await request.body()
        body = json.loads(raw_body.decode('utf-8'))
    except (ValueError, TypeError, UnicodeDecodeError):
        body = {}
    msg = body.get("message", "")
    match_type = _detect_match_type(msg)
    logger.info(f"[Chat] Received msg={msg[:40]!r} len={len(msg)} match_type={match_type}")

    async def generate():
        _init_msg = json.dumps({'type': 'text', 'content': '🔍 哨响AI v4.1 分析中...\n\n'})
        yield f"data: {_init_msg}\n\n"

        _msg_lower = msg.lower().strip()
        _live_keywords = ['今晚', '今天', '赛程', 'fixtures', 'fixture', '什么比赛', '有哪些比赛', '接下来', 'scheduled', 'upcoming']
        _standings_keywords = ['积分', '积分榜', 'standings', '排名', '小组排名', 'group']
        _scorers_keywords = ['射手', '射手榜', 'scorers', '进球榜', '谁进', 'top scorer']
        _live_now_keywords = ['直播', 'live', '正在进行', 'in play']

        if any(kw in _msg_lower for kw in _live_keywords + _standings_keywords + _scorers_keywords + _live_now_keywords):
            try:
                from data_collector.football_data_live import FootballDataLive
                fdl = FootballDataLive()
                reply_parts = []
                if any(kw in _msg_lower for kw in _live_now_keywords):
                    live = fdl.get_live_scores()
                    reply_parts.append(f"📡 实时直播 ({len(live)}场)\n")
                    for m in live[:10]:
                        sc = m.get('score', {})
                        ft = sc.get('fullTime', {})
                        reply_parts.append(f"  {m.get('homeTeam',{}).get('name','?')} {ft.get('home','?')}-{ft.get('away','?')} {m.get('awayTeam',{}).get('name','?')}\n")
                if any(kw in _msg_lower for kw in _live_keywords):
                    fixtures = fdl.get_wc2026_fixtures()
                    reply_parts.append(f"\n📅 待赛赛程 ({len(fixtures)}场)\n")
                    for f in fixtures[:15]:
                        reply_parts.append(f"  {f.get('utcDate','')[:16].replace('T',' ')} {f.get('homeTeam',{}).get('name','?')} vs {f.get('awayTeam',{}).get('name','?')}\n")
                if any(kw in _msg_lower for kw in _standings_keywords):
                    standings = fdl.get_wc2026_standings()
                    reply_parts.append(f"\n🏆 积分榜 ({len(standings)}个组)\n")
                    for s in standings:
                        reply_parts.append(f"  [{s.get('group','?')}]\n")
                        for t in s.get('table', [])[:4]:
                            reply_parts.append(f"    {t.get('position','?')}. {t.get('team',{}).get('name','?')} {t.get('points',0)}pts\n")
                if any(kw in _msg_lower for kw in _scorers_keywords):
                    scorers = fdl.get_wc2026_scorers()
                    reply_parts.append(f"\n⚽ 射手榜 Top 10\n")
                    for i, s in enumerate(scorers[:10], 1):
                        reply_parts.append(f"  {i}. {s.get('player',{}).get('name','?')} ({s.get('team',{}).get('name','?')}) {s.get('goals',0)}球\n")
                if reply_parts:
                    full_reply = ''.join(reply_parts)
                    for chunk in [full_reply[i:i+300] for i in range(0, len(full_reply), 300)]:
                        yield f"data: {json.dumps({'type':'text','content':chunk})}\n\n"
                        await asyncio.sleep(0.02)
                    yield f"data: {json.dumps({'type':'done'})}\n\n"
                    return
            except Exception as e:
                logger.warning(f"[Chat] 实时数据查询失败: {e}")

        _form_teams = re.findall(r'(.+?)\s+(?:vs|VS|对)\s+(.+?)(?:\s+\d|$)', msg)
        if _form_teams:
            _fh = re.sub(r'\s*\d+\.\d+.*$', '', _form_teams[0][0].strip()).strip()
            _fa = re.sub(r'\s*\d+\.\d+.*$', '', _form_teams[0][1].strip()).strip()
            try:
                from data_collector.football_data_live import FootballDataLive
                _fdl = FootballDataLive()
                _form_report = _fdl.format_form_report(_fh, _fa)
                if '战绩数据不足' not in _form_report and 'error' not in _form_report.lower():
                    yield f"data: {json.dumps({'type':'text','content':_form_report + chr(10) + chr(10)})}\n\n"
                    await asyncio.sleep(0.01)
            except Exception as _fe:
                logger.debug(f"[Chat] Trend/Form获取失败: {_fe}")

        try:
            from modules.six_layer_conversation import SixLayerConversationEngine

            odds_match = re.findall(r'(\d+\.\d+)', msg)
            teams = re.findall(r'(.+?)\s+(?:vs|VS|对)\s+(.+?)(?:\s|\$|，|,)', msg)
            home = teams[0][0].strip() if teams else ""
            away = teams[0][1].strip() if teams else ""

            if not home:
                single_team = re.match(r'^([\u4e00-\u9fffA-Za-z\s]{1,20})\s+(\d+\.\d+)\s+(\d+\.\d+)\s+(\d+\.\d+)', msg.strip())
                if single_team:
                    home = single_team.group(1).strip()
                    away = "?"
                    odds_match = [single_team.group(2), single_team.group(3), single_team.group(4)]

            odds = None
            if len(odds_match) >= 3:
                odds = {'home': float(odds_match[0]), 'draw': float(odds_match[1]), 'away': float(odds_match[2])}

            handicap = None
            ou_line = None
            water_level = None
            over_water = 1.90
            under_water = 1.92
            if len(odds_match) >= 6:
                handicap = float(odds_match[3])
                ou_line = float(odds_match[4])
                water_level = float(odds_match[5])
            elif len(odds_match) >= 5:
                handicap = float(odds_match[3])
                ou_line = float(odds_match[4])
            elif len(odds_match) >= 4 and '让' in msg:
                handicap = float(odds_match[3])
            if '让' in msg:
                _hc_match = re.search(r'让\s*(\d+\.?\d*)', msg)
                if _hc_match:
                    try: handicap = float(_hc_match.group(1))
                    except (ValueError, TypeError): pass
            if ou_line is None:
                ou_match = re.search(r'[大小].*?/(\d+\.\d+)', msg) or re.search(r'[大小]\D*?(\d+\.\d+)', msg)
                if ou_match:
                    try: ou_line = float(ou_match.group(1))
                    except (ValueError, TypeError): pass
                if ou_line is None:
                    ou_match = re.search(r'[大小]\D*?(\d+)', msg)
                    if ou_match:
                        try: ou_line = float(ou_match.group(1))
                        except (ValueError, TypeError): pass
            if odds:
                odds['_handicap'] = handicap or 0.0
                odds['_ou_line'] = ou_line or 2.5
                odds['_over_water'] = over_water
                odds['_under_water'] = under_water

            engine = SixLayerConversationEngine(enable_l6=False)
            result = engine.process(msg, home, away, "世界杯" if not home else "", odds)

            report = result.analysis_report
            for chunk in [report[i:i+300] for i in range(0, len(report), 300)]:
                yield f"data: {json.dumps({'type':'text','content':chunk})}\n\n"
                await asyncio.sleep(0.02)

            if home and away and odds and len(odds) >= 3:
                bm_report = _build_bookmaker_report(home, away, odds, result.h_prob, result.d_prob, result.a_prob)
                for chunk in [bm_report[i:i+300] for i in range(0, len(bm_report), 300)]:
                    yield f"data: {json.dumps({'type':'text','content':chunk})}\n\n"
                    await asyncio.sleep(0.01)

            hp, dp, ap = result.h_prob, result.d_prob, result.a_prob
            d_gate_active = False
            d_gate_mode = ""

            if odds and len(odds) >= 3:
                oh_p, od_p, oa_p = odds.get('home',2), odds.get('draw',3.2), odds.get('away',3.5)
                inv_p = 1/oh_p + 1/od_p + 1/oa_p
                imp_h = 1/oh_p/inv_p; imp_d = 1/od_p/inv_p; imp_a = 1/oa_p/inv_p
                _spread_p = abs(imp_h - imp_a)
                _d_boost_est = imp_d * (0.268 / 0.257)
                if _spread_p > 0.50: _d_boost_est *= 0.60
                elif 0.03 <= _spread_p < 0.08: _d_boost_est *= 1.15
                else: _d_boost_est *= 1.08

                dg = _apply_dgate(imp_h, imp_d, imp_a, odds,
                                  handicap=handicap, ou_line=ou_line, water_level=water_level,
                                  fifa_rank_diff=None, group_round=None, match_type=match_type,
                                  h_adj=None, a_adj=None, d_boosted=_d_boost_est)
                d_gate_active = dg['d_gate_active']
                d_gate_mode = dg['d_gate_mode']
                prediction = "平局" if dg['verdict'] == 'D' else ("主胜" if dg['verdict'] == 'H' else "客胜")
            else:
                if dp > 0.28 and dp > max(hp, ap) * 0.85:
                    prediction = "平局"
                elif hp > ap: prediction = "主胜"
                else: prediction = "客胜"

            card = {"home":home or "?","away":away or "?",
                    "h_prob":round(hp,4),"d_prob":round(dp,4),"a_prob":round(ap,4),
                    "d_gate":result.d_gate_result or "","time_ms":round(result.total_time_ms,1),
                    "prediction": prediction, "d_gate_active": d_gate_active,
                    "d_gate_mode": d_gate_mode, "match_type": match_type}
            if hp == 0 and dp == 0 and ap == 0 and odds:
                oh2, od2, oa2 = odds.get('home',2.0), odds.get('draw',3.2), odds.get('away',3.5)
                inv_fb = 1/oh2 + 1/od2 + 1/oa2
                card['h_prob'] = round(1/oh2/inv_fb, 4)
                card['d_prob'] = round(1/od2/inv_fb, 4)
                card['a_prob'] = round(1/oa2/inv_fb, 4)

            if d_gate_active and d_gate_mode:
                from modules.six_layer_conversation import SixLayerConversationEngine as _SLC
                engine_ref = _SLC.__new__(_SLC)
                if hp == 0 and dp == 0 and ap == 0 and odds:
                    oh2, od2, oa2 = odds.get('home',2.0), odds.get('draw',3.2), odds.get('away',3.5)
                    inv_use = 1/oh2 + 1/od2 + 1/oa2
                    hp_use, dp_use, ap_use = 1/oh2/inv_use, 1/od2/inv_use, 1/oa2/inv_use
                else: hp_use, dp_use, ap_use = hp, dp, ap
                card['d_gate'] = engine_ref._apply_d_gate(hp_use, dp_use, ap_use, d_gate_override=True, gate_mode=d_gate_mode)
                card['risk_tags'] = [f'd_gate_{d_gate_mode}']
            else:
                d_margin = dp - max(hp, ap)
                if d_margin < 0.02: card['risk_tags'] = ['d_gate_junk']
                elif d_margin < 0.05: card['risk_tags'] = ['d_gate_fuzzy']
                else: card['risk_tags'] = []

            trap_warnings = []
            if home and away and odds and len(odds) >= 3:
                try:
                    from bookmaker_sim.bookmaker_trap_detector import BookmakerTrapDetector as _BTD
                    _trap_det = _BTD()
                    _trap_rpt = _trap_det.detect({
                        "home":home,"away":away,"league":"世界杯",
                        "odds_h":odds.get('home',2.0),"odds_d":odds.get('draw',3.2),"odds_a":odds.get('away',3.5),
                        "asian_handicap":handicap,"water_level":water_level or 0.92})
                    for sig in _trap_rpt.signals:
                        trap_warnings.append({"type":sig.trap_type.value,"confidence":round(sig.confidence,2),"direction":sig.direction,"description":sig.description})
                    card['trap_score'] = round(_trap_rpt.aggregate_score, 1)
                    card['trap_recommendation'] = _trap_rpt.recommendation
                except (ImportError, AttributeError, ValueError) as e:
                    logger.debug(f"[Trap] 检测跳过: {e}")
            card['trap_warnings'] = trap_warnings

            has_ignore_draw_trap = any(t.get('direction') == 'ignore_draw' for t in trap_warnings)
            _draw_margin = dp - max(hp, ap)
            if has_ignore_draw_trap and _draw_margin < -0.05:
                card['risk_tag'] = 'ignore_draw'
                card['draw_punish_rate'] = 0.3
                card['risk_tag_reason'] = (f"陷阱检测诱平信号+平局边际({_draw_margin:+.3f})弱")
                if d_gate_active:
                    if hp > ap: prediction = "主胜"
                    else: prediction = "客胜"
                    d_gate_active = False
                    card['d_gate_active'] = False
                    card['d_gate'] = f"[陷阱压倒] D-Gate原判断被诱平信号覆盖, 切回{prediction}"
            elif has_ignore_draw_trap:
                card['risk_tag'] = 'weak_ignore_draw'
                card['draw_punish_rate'] = 0.5
                card['risk_tag_reason'] = f"陷阱检测诱平信号, 平局边际{_draw_margin:+.3f}"
                if d_gate_active: card['d_gate'] += " [⚠️与诱平信号冲突]"
            elif d_gate_active:
                card['risk_tag'] = 'favor_draw'
                card['draw_punish_rate'] = 1.0
                card['risk_tag_reason'] = f"D-Gate 模式{d_gate_mode}激活"
            elif _draw_margin < -0.10:
                card['risk_tag'] = 'weak_draw'
                card['draw_punish_rate'] = 0.7
                card['risk_tag_reason'] = f"平局边际{_draw_margin:+.3f}显著为负"
            else:
                card['risk_tag'] = 'neutral'
                card['draw_punish_rate'] = 1.0
                card['risk_tag_reason'] = ""

            if odds and len(odds) >= 3:
                oh2, od2, oa2 = odds.get('home',2), odds.get('draw',3.2), odds.get('away',3.5)
                inv = 1/oh2 + 1/od2 + 1/oa2
                card['implied'] = {'home':round(1/oh2/inv,3),'draw':round(1/od2/inv,3),'away':round(1/oa2/inv,3)}

            if result.h_prob + result.d_prob + result.a_prob > 0:
                try:
                    from optimize.poisson_predictor import PoissonPredictor
                    pp = PoissonPredictor()
                    scores_raw = pp.predict_scores(result.h_prob, result.d_prob, result.a_prob, "default", 3)
                    if scores_raw:
                        _punish = card.get('draw_punish_rate', 1.0)
                        _risk_tag = card.get('risk_tag', 'neutral')
                        _processed = []
                        for s in scores_raw[:5]:
                            raw_p = s.get('probability', 0)
                            score_str = str(s.get('score', '?'))
                            try: _h, _a = score_str.split('-'); _is_draw = (_h == _a)
                            except (ValueError, AttributeError): _is_draw = False
                            if _is_draw and _risk_tag in ('ignore_draw','weak_ignore_draw'):
                                eff_p = raw_p * _punish; _tag = '风控低参考'; _star = 0
                            elif _is_draw and _risk_tag == 'weak_draw':
                                eff_p = raw_p * _punish; _tag = '平局概率偏低'; _star = max(0, 1 if raw_p > 0.08 else 0)
                            elif _is_draw and _risk_tag == 'favor_draw':
                                eff_p = raw_p; _tag = '模型看好'; _star = 3
                            else: eff_p = raw_p; _tag = ''; _star = 0
                            _processed.append({'score':score_str,'prob':f"{eff_p:.1%}",'raw_prob':raw_p,'eff_prob':eff_p,'outcome':s.get('outcome','?'),'is_draw':_is_draw,'tag':_tag,'star':_star})
                        _processed.sort(key=lambda x: x['eff_prob'], reverse=True)
                        for idx, item in enumerate(_processed[:3]):
                            if item['star'] == 0 and not item.get('tag'): item['star'] = 3 - idx
                        card['scores'] = _processed[:3]
                except (ImportError, ValueError, KeyError) as e:
                    logger.debug(f"[ScorePred] 跳过: {e}")

            if home and away and odds and len(odds) >= 3:
                try:
                    _fifa_diff = _get_fifa_rank_diff(home, away)
                    card['analysis'] = _build_analysis_card(home, away, odds,
                        result.h_prob, result.d_prob, result.a_prob,
                        handicap, ou_line, water_level,
                        fifa_rank_diff=_fifa_diff, match_type=match_type)
                    card['bookmaker'] = _build_bookmaker_card(
                        home, away, odds, result.h_prob, result.d_prob, result.a_prob,
                        d_gate_mode=d_gate_mode, ou_line=ou_line, handicap=handicap)
                except Exception as e:
                    logger.warning(f"[Chat] bookmaker_card failed: {e}", exc_info=True)
            yield f"data: {json.dumps({'type':'predict_card','data':card})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type':'error','content':str(e)})}\n\n"
        yield f"data: {json.dumps({'type':'done'})}\n\n"
    return StreamingResponse(generate(), media_type="text/event-stream")

@router.get("/chat/health")
async def chat_health():
    return {"status": "ok", "version": "v4.1"}
