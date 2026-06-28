"""qtx.com世界杯文章批量收录工具
用法: python scripts/qtx_fetcher.py [start_id] [end_id]
示例: python scripts/qtx_fetcher.py 287000 287224  # 批量拉取
"""
import json, sys, time
from pathlib import Path

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / 'data' / 'qtx_articles'
DATA_DIR.mkdir(parents=True, exist_ok=True)

# 已知文章索引 (手动收录)
KNOWN = {
    287221: "0败的佛得角vs全胜的阿根廷",
    287220: "2026美加墨世界杯小组赛盘点 72场237球全纪录",
    287212: "本届世界杯唯一0进球球队诞生 巴拿马三战0球2分",
    287211: "世界杯32强球队大洲分布 亚洲仅两队晋级",
    287210: "葡萄牙1/16淘汰赛将战克罗地亚",
    287209: "民主刚果首次晋级世界杯淘汰赛",
    287203: "凯恩独享英格兰队史射手王",
    287201: "巴拿马是本届世界杯唯一0进球球队",
    287197: "2026美加墨世界杯小组赛最终积分排名",
    287196: "世界杯1/16决赛赛程对阵时间图表",
    287195: "世界杯32强全部出炉",
    287194: "韩媒批韩国黄金一代耻辱出局",
    287190: "世界杯德国vs巴拉圭前瞻预测分析",
    287149: "世界杯淘汰赛多少支队伍",
    287147: "世界杯G组大结局 比利时获得榜首位置晋级",
    287145: "世界杯32强确定28席",
    287144: "佛得角成史上进淘汰赛面积最小国家",
    287138: "世界杯比利时5-1新西兰",
    287137: "南美6支世界杯参赛球队仅乌拉圭出局",
    287136: "世界杯6月28日赛程直播时间表",
    287135: "世界杯H组大结局 西班牙、佛得角晋级",
    287134: "佛得角首次参加世界杯就晋级淘汰赛",
    287133: "世界杯1/16淘汰赛阿根廷将战佛得角",
    287132: "世界杯佛得角0-0沙特阿拉伯",
    287129: "挪威主帅回应哈兰德未出场",
    287128: "哈兰德姆巴佩王不见王",
    287127: "韩国排名滑至第7 出线概率跌破5成",
    287126: "法国第三次小组赛全胜",
}

# 分类标记
CATEGORIES = {
    "match_report": [287138, 287132],  # 比赛战报
    "group_summary": [287147, 287135, 287220],  # 小组总结
    "preview": [287190, 287210, 287133, 287221],  # 前瞻
    "data": [287197, 287196, 287195, 287136],  # 数据/赛程
    "analysis": [287211, 287212, 287137, 287127, 287126, 287144, 287134],  # 分析
    "news": [287194, 287203, 287201, 287209, 287145, 287149, 287129, 287128],  # 新闻
}

def save_index():
    """保存文章索引到数据库"""
    index = [{"id": aid, "title": title, 
              "url": f"https://www.qtx.com/worldcup/{aid}.html"}
             for aid, title in sorted(KNOWN.items())]
    
    with open(ROOT / 'data' / 'qtx_article_index.json', 'w', encoding='utf-8') as f:
        json.dump({"total": len(index), "articles": index, "categories": CATEGORIES}, 
                  f, ensure_ascii=False, indent=2)
    
    print(f"✅ 文章索引: {len(index)}篇")
    print(f"   分类: {', '.join(f'{k}({len(v)})' for k,v in CATEGORIES.items())}")
    return index

if __name__ == '__main__':
    save_index()
