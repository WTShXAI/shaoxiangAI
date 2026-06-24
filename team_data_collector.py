"""
team_data_collector.py — 球队数据采集器
========================================
架构: Python缓存层 + WorkBuddy WebSearch回调
用法:
  1. collector = TeamDataCollector()
  2. data = collector.get_team_data("哥伦比亚")
  3. 如果 data['stale'] == True → 提示Agent执行WebSearch
  4. Agent搜索后 → collector.update_cache("哥伦比亚", raw_text)
  5. VIP重新读取 → 获得结构化数据
"""

import json, os, time
from pathlib import Path

CACHE_DIR = Path(__file__).parent / ".temp" / "team_data"
CACHE_TTL = 86400  # 24小时过期


class TeamDataCollector:
    """球队数据采集 — 缓存+WebSearch桥接"""
    
    def __init__(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    
    # ━━━ 查询模板 ━━━
    SEARCH_TEMPLATES = {
        "recent_form": "{team} 近10场比赛 比分 2026",
        "lineup": "{team} vs {opponent} 首发阵容 阵型 2026世界杯",
        "news": "{team} 伤病 教练 新闻 2026世界杯",
        "h2h": "{team_a} vs {team_b} 历史交锋",
    }
    
    # ━━━ 读取缓存 ━━━
    def get_team_data(self, team: str, opponent: str = None) -> dict:
        """读取球队数据，标记是否过期"""
        cache_file = CACHE_DIR / f"{team}.json"
        
        if cache_file.exists():
            data = json.loads(cache_file.read_text(encoding='utf-8'))
            age = time.time() - data.get('_updated', 0)
            data['_stale'] = age > CACHE_TTL
            data['_age_hours'] = age / 3600
            return data
        
        # 无缓存 → 返回搜索指令
        return {
            '_stale': True,
            '_search_needed': self._build_search_queries(team, opponent),
            'recent_matches': [],
            'lineup': None,
            'injuries': [],
            'coach_news': [],
        }
    
    def _build_search_queries(self, team: str, opponent: str = None) -> list:
        queries = [
            self.SEARCH_TEMPLATES["recent_form"].format(team=team),
            self.SEARCH_TEMPLATES["news"].format(team=team),
        ]
        if opponent:
            queries.append(
                self.SEARCH_TEMPLATES["lineup"].format(team=team, opponent=opponent)
            )
        return queries
    
    # ━━━ 更新缓存 ━━━
    def update_cache(self, team: str, field: str, data: dict):
        """Agent搜索后调用，更新缓存"""
        cache_file = CACHE_DIR / f"{team}.json"
        
        existing = {}
        if cache_file.exists():
            existing = json.loads(cache_file.read_text(encoding='utf-8'))
        
        existing[field] = data
        existing['_updated'] = time.time()
        
        cache_file.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding='utf-8')
    
    # ━━━ 解析WebSearch结果 ━━━
    def parse_recent_matches(self, raw_text: str) -> list:
        """从WebFetch文本中提取比分记录"""
        matches = []
        import re
        # 匹配: "球队名 X-Y 对手名" 或 "X-Y 对手"
        pattern = r'(\d+)[-:](\d+)\s+(\S+)'
        for m in re.finditer(pattern, raw_text):
            matches.append({
                'goals_for': int(m.group(1)),
                'goals_against': int(m.group(2)),
                'opponent': m.group(3)
            })
        return matches[-10:]  # 最近10场
    
    def parse_lineup(self, raw_text: str) -> dict:
        """提取阵型"""
        formations = ['4-4-2', '4-3-3', '4-2-3-1', '3-4-3', '3-5-2', 
                      '5-3-2', '5-4-1', '4-5-1', '3-4-2-1']
        found = None
        for f in formations:
            if f in raw_text:
                found = f
                break
        return {'formation': found, 'raw': raw_text[:500]}

    def parse_injuries(self, raw_text: str) -> list:
        """v4.0: 从WebFetch文本提取伤病信息"""
        injuries = []
        import re
        
        # 匹配常见伤病描述模式
        # "XX受伤" "XX缺阵" "XX伤停" "XX因伤缺席" "XX(伤)"
        injury_patterns = [
            r'([^\s,，。]+?)(?:因伤|受伤|伤停|伤病|拉伤|扭伤|骨折|肌肉伤)(?:[^\n]{0,30})',
            r'([^\s,，。]+?)(?:缺阵|缺席|缺战|无法出战)(?:[^\n]{0,30})',
        ]
        
        for pattern in injury_patterns:
            for m in re.finditer(pattern, raw_text):
                name = m.group(1).strip()
                if len(name) >= 2 and len(name) <= 20 and name not in ['因', '已', '可能', '预计']:
                    # 尝试提取出场次数
                    caps_match = re.search(rf'{re.escape(name)}[^\n]*?(\d+)\s*场', raw_text)
                    caps = int(caps_match.group(1)) if caps_match else 0
                    injuries.append({
                        'player': name,
                        'caps': caps,
                        'context': m.group(0)[:100],
                    })
        
        # 去重
        seen = set()
        unique = []
        for inj in injuries:
            if inj['player'] not in seen:
                seen.add(inj['player'])
                unique.append(inj)
        
        return unique[:10]

    def parse_coach_news(self, raw_text: str) -> list:
        """v4.0: 提取教练相关新闻"""
        import re
        coach_keywords = ['教练', '主帅', '主教练', '换帅', '下课', '新帅', '战术',
                          'coach', 'manager', '战术体系', '阵型调整']
        news = []
        for line in raw_text.split('\n'):
            line = line.strip()
            if not line or len(line) < 10:
                continue
            for kw in coach_keywords:
                if kw in line:
                    news.append(line[:200])
                    break
        return news[:5]

    def parse_team_strength(self, raw_text: str) -> dict:
        """v4.0: 提取球队实力指标"""
        import re
        result = {
            'fifa_rank': None,
            'recent_goals_scored': 0,
            'recent_goals_conceded': 0,
            'clean_sheets': 0,
        }
        
        # FIFA排名
        rank_match = re.search(r'(?:FIFA|世界)\s*排名\s*[:：]?\s*(\d+)', raw_text)
        if not rank_match:
            rank_match = re.search(r'排名\s*第?\s*(\d+)', raw_text)
        if rank_match:
            result['fifa_rank'] = int(rank_match.group(1))
        
        return result

    # ━━━ 批量操作 (v4.0) ━━━
    def batch_prefetch(self, matches: list) -> dict:
        """
        批量预取多场比赛的球队数据
        
        Args:
            matches: [{"home": "英格兰", "away": "克罗地亚"}, ...]
        Returns:
            {"ready": [...], "stale": [...], "search_queries": [...]}
        """
        ready = []
        stale = []
        all_queries = []
        
        for match in matches:
            home = match['home']
            away = match['away']
            
            home_data = self.get_team_data(home, away)
            away_data = self.get_team_data(away, home)
            
            match_info = {'home': home, 'away': away}
            
            if not home_data.get('_stale') and not away_data.get('_stale'):
                ready.append(match_info)
            else:
                stale.append(match_info)
                if home_data.get('_stale'):
                    all_queries.extend(home_data.get('_search_needed', []))
                if away_data.get('_stale'):
                    all_queries.extend(away_data.get('_search_needed', []))
        
        return {
            'ready': ready,
            'stale': stale,
            'search_queries': list(set(all_queries)),  # 去重
            'ready_count': len(ready),
            'stale_count': len(stale),
        }

    def get_match_context(self, home: str, away: str) -> dict:
        """v4.0: 获取完整比赛上下文 (供VIP/SKY集成)"""
        home_data = self.get_team_data(home, away)
        away_data = self.get_team_data(away, home)
        
        return {
            'home': {
                'name': home,
                'recent_form': home_data.get('recent_matches', []),
                'formation': home_data.get('lineup', {}).get('formation'),
                'injuries': home_data.get('injuries', []),
                'coach_news': home_data.get('coach_news', []),
                'stale': home_data.get('_stale', True),
            },
            'away': {
                'name': away,
                'recent_form': away_data.get('recent_matches', []),
                'formation': away_data.get('lineup', {}).get('formation'),
                'injuries': away_data.get('injuries', []),
                'coach_news': away_data.get('coach_news', []),
                'stale': away_data.get('_stale', True),
            },
            'all_fresh': not home_data.get('_stale') and not away_data.get('_stale'),
        }
    
    # ━━━ 注入VIP的特征 ━━━
    def extract_vip_features(self, team: str) -> dict:
        """从缓存提取VIP可用特征"""
        data = self.get_team_data(team)
        if data.get('_stale'):
            return {'error': '数据过期，需WebSearch刷新'}
        
        features = {}
        
        # 防线趋势
        if 'recent_matches' in data:
            conceded = [m.get('goals_against', 0) for m in data['recent_matches'][:5]]
            if conceded:
                weights = [0.5, 0.35, 0.25, 0.18, 0.12][:len(conceded)]
                features['defensive_trend'] = sum(c*w for c,w in zip(conceded, weights))
                features['defense_degrading'] = features['defensive_trend'] > 1.5
        
        # 阵容变化影响
        if 'lineup' in data and data['lineup'].get('formation'):
            features['formation'] = data['lineup']['formation']
        
        # 伤病影响
        if 'injuries' in data:
            features['key_player_out'] = len([i for i in data['injuries'] 
                if i.get('caps', 0) > 50])  # 50场以上=核心
        
        return features


# ━━━ 快速测试 ━━━
if __name__ == "__main__":
    collector = TeamDataCollector()
    result = collector.get_team_data("哥伦比亚", "乌兹别克斯坦")
    print(f"缓存状态: {'过期' if result['_stale'] else '有效'}")
    if result['_stale']:
        print(f"需要搜索: {result['_search_needed']}")
