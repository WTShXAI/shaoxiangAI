"""
预测报告生成器 - 简洁直接版本
替代 six_layer_conversation 中的冗长报告
结合 knowledge_base.db 增强预测
"""
import json
import logging
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Optional

logger = logging.getLogger(__name__)

def build_prediction_report(
    home: str, away: str, league: str,
    h_prob: float, d_prob: float, a_prob: float,
    prediction: str,
    d_gate_result: str = "",
    d_gate_active: bool = False,
    d_gate_mode: str = "",
    trap_warnings: list = None,
    odds: dict = None,
    lambda_fusion: dict = None,
    goal_prediction: dict = None,
    match_type: str = "",
    elapsed_ms: float = 0,
    channel_breakdown: dict = None,
    vip_scores: list = None,
    vip_view: str = "",
    vip_rec: str = "",
) -> str:
    """
    生成简洁直接的预测报告
    
    格式:
    1. 结论一行
    2. 概率巴条
    3. 比分推荐
    4. 关键信号
    5. 知识库参考
    """
    lines = []
    
    # ══════ 标题行 ══════
    league_tag = f" · {league}" if league else ""
    match_str = f"{home} vs {away}{league_tag}"
    tm = datetime.now(timezone.utc).strftime("%H:%M")
    lines.append(f"⚽ {match_str}")
    
    # ══════ 1. 结论 ══════
    pred_labels = {"H": "主胜", "D": "平局", "A": "客胜"}
    pred_icons = {"H": "主", "D": "平", "A": "客"}
    pred_code = prediction[0] if prediction in ["主胜","平局","客胜"] else ("H" if prediction=="主胜" else ("D" if prediction=="平局" else "A"))
    icon = pred_icons.get(pred_code, "")
    label = pred_labels.get(pred_code, prediction)
    
    # 置信度分级
    top_p = max(h_prob, d_prob, a_prob)
    if top_p > 0.70:
        conf_level = "高"
    elif top_p > 0.55:
        conf_level = "中"
    else:
        conf_level = "一般"
    
    # D-Gate 修饰
    dg_suffix = ""
    if d_gate_active:
        dg_suffix = f" ⚡D-Gate·{d_gate_mode or '激活'}"
    
    lines.append(f"\n📌 {icon} {label} | 置信度{conf_level}({top_p:.0%}){dg_suffix}")
    
    # ══════ 2. 概率条 ══════
    lines.append(f"\n{'─'*28}")
    max_bar = 28
    h_bar = int(h_prob * max_bar)
    d_bar = int(d_prob * max_bar)
    a_bar = int(a_prob * max_bar)
    lines.append(f"H {'█'*h_bar}{'░'*(max_bar-h_bar)} {h_prob:.0%}")
    lines.append(f"D {'█'*d_bar}{'░'*(max_bar-d_bar)} {d_prob:.0%}")
    lines.append(f"A {'█'*a_bar}{'░'*(max_bar-a_bar)} {a_prob:.0%}")
    
    # ══════ 3. 进球与比分 ══════
    if lambda_fusion and goal_prediction:
        lam_h = lambda_fusion.get("fused_lam", [0,0])[0]
        lam_a = lambda_fusion.get("fused_lam", [0,0])[1]
        gh = goal_prediction.get("home", 0)
        ga = goal_prediction.get("away", 0)
        gt = goal_prediction.get("total", 0)
        ou = goal_prediction.get("ou_prediction", "")
        ou_icon = "大" if "Over" in str(ou) else "小"
        
        lines.append(f"\n⚽ 预期进球: {home} {lam_h:.1f} - {lam_a:.1f} {away}")
        lines.append(f"🎯 推荐比分: {gh:.0f}-{ga:.0f} ({ou_icon}球)")
    
    # ══════ 4. 三线一致性 ══════
    if channel_breakdown:
        final = channel_breakdown.get("final", [])
        sky = channel_breakdown.get("sky", [])
        vip = channel_breakdown.get("vip_math", [])
        
        if sky and vip and final:
            sky_pred = "H" if sky[0] > max(sky[1], sky[2]) else ("D" if sky[1] > max(sky[0], sky[2]) else "A")
            vip_pred = "H" if vip[0] > max(vip[1], vip[2]) else ("D" if vip[1] > max(vip[0], vip[2]) else "A")
            final_pred = "H" if final[0] > max(final[1], final[2]) else ("D" if final[1] > max(final[0], final[2]) else "A")
            
            all_same = sky_pred == vip_pred == final_pred
            if all_same:
                lines.append(f"\n🤝 三线一致 → {pred_labels.get(final_pred, final_pred)}")
            else:
                lines.append(f"  SKY:{pred_labels.get(sky_pred,'?')} | VIP:{pred_labels.get(vip_pred,'?')} | → {pred_labels.get(final_pred,'?')}")
    
    # ══════ 4.5 VIP操盘手视角 ══════
    if vip_view:
        lines.append(f"\n🎭 庄家: {vip_view}")
    if vip_rec:
        # 截短推荐
        rec_short = vip_rec[:60] + ("..." if len(vip_rec) > 60 else "")
        lines.append(f"💡 VIP: {rec_short}")
    if vip_scores:
        score_str = " | ".join([f"{s.get('score','?')} {s.get('prob',0):.1%}" for s in vip_scores[:3]])
        lines.append(f"🎯 VIP比分: {score_str}")
    
    # ══════ 5. D-Gate与风控 ══════
    if d_gate_active and d_gate_result:
        # 简洁D-Gate提示
        dg_short = d_gate_result.replace("D-Gate", "").replace("激活", "").replace("模式", "").strip(":： ")
        if len(dg_short) > 40:
            dg_short = dg_short[:40] + "..."
        lines.append(f"\n🛡️ D-Gate: {dg_short}")
    
    if trap_warnings:
        # 最多显示2条陷阱警告
        for tw in trap_warnings[:2]:
            ttype = tw.get("type", "")
            tdir = tw.get("direction", "")
            tconf = tw.get("confidence", 0)
            if tconf > 0.5:
                lines.append(f"⚠️ 陷阱: {ttype} ({tdir}, {tconf:.0%})")
    
    # ══════ 6. 知识库参考 ══════
    try:
        kb_ref = _query_knowledge_base(h_prob, d_prob, prediction, odds)
        if kb_ref:
            lines.append(f"\n📚 {kb_ref}")
    except Exception as e:
        logger.warning("知识库查询失败: %s", e)
    
    # ══════ 7. 底部 ══════
    lines.append(f"\n{'─'*28}")
    lines.append(f"⚡ {elapsed_ms:.0f}ms · v4.1 · {match_type or '联赛'}")
    
    return "\n".join(lines)

def _query_knowledge_base(h_prob: float, d_prob: float, prediction: str, odds: dict = None) -> Optional[str]:
    """查询knowledge_base.db获取参考信息"""
    try:
        db_path = Path(__file__).parent.parent / "data" / "knowledge_base.db"
        if not db_path.exists():
            return None
        
        conn = sqlite3.connect(str(db_path))
        cur = conn.cursor()
        
        results = []
        
        # 1. 查找匹配的OU线
        if odds:
            oh = odds.get('home', odds.get('H', 2.0))
            od = odds.get('draw', odds.get('D', 3.2))
            oa = odds.get('away', odds.get('A', 3.5))
            
            # 估算OU
            ih, id_, ia = 1/oh, 1/od, 1/oa
            total = ih + id_ + ia
            spread = ih/total - ia/total
            est_lam = 2.0 + 2.5 * abs(spread)
            
            ou_lines = [2.0, 2.25, 2.5, 2.75, 3.0, 3.25, 3.5]
            closest_ou = min(ou_lines, key=lambda x: abs(x - est_lam))
            
            # 查OU知识
            cur.execute("""
                SELECT avg_goals, exact_pct, over_pct, under_pct, h_win_pct, d_pct
                FROM ou_knowledge WHERE ou_line = ?
            """, (closest_ou,))
            row = cur.fetchone()
            if row:
                avg_g, exact_p, over_p, under_p, h_win, d_pct_kb = row
                
                # 简洁版本
                if over_p > under_p:
                    results.append(f"OU{closest_ou}:均{avg_g:.1f}球 · {over_p:.0f}%超线")
                else:
                    results.append(f"OU{closest_ou}:均{avg_g:.1f}球 · {under_p:.0f}%穿线")
            
            # 查安全区
            if closest_ou == 3.0:
                cur.execute("SELECT push_pct, net_bookmaker_yield FROM ou_safety_efficiency WHERE ou_line = 3.0")
                row2 = cur.fetchone()
                if row2:
                    results.append(f"OU3.0走水{row2[0]:.0f}%")
        
        # 2. 平局偏高风险提示
        if d_prob > 0.16 and d_prob < 0.30:
            results.append(f"平局{d_prob:.0%}注意D-Gate")
        
        conn.close()
        
        return " | ".join(results) if results else None
        
    except Exception as e:
        logger.debug("知识库查询异常: %s", e)
        return None
