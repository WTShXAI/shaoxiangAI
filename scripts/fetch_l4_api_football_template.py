#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_l4_api_football_template.py — L4(伤病/阵容) 获取模板 [待激活]

⚠️ 当前状态: BLOCKED — 需 API key (api-football / api-sports.io 免费档 100 req/day)
   注册: https://www.api-football.com/  ->  dashboard 拿 key
   填入下方 API_KEY 后取消注释即可运行。

为什么需要它:
   - ESPN 免费 API 确认无结构化 injuries (summary/rosters/boxscore 均无)
   - worldcup26.ir 无 lineup/injury 端点
   - transfermarkt 反爬(人机验证)无法直接爬
   - thestatsapi / tonkabits 均需付费/申请 key
   => api-football 免费档是唯一含 /players/injuries + /fixtures/lineups 的免费源

激活步骤:
   1. 注册 api-football 拿 API_KEY
   2. 设环境变量 API_FOOTBALL_KEY 或改下方常量
   3. 取消 main() 注释运行 -> 写入 wc_injuries / wc_lineups_l4 表

设计(留接口, 不编造数据):
   GET https://v3.football.api-sports.io/players/injuries?fixture={fid}
   GET https://v3.football.api-sports.io/fixtures/lineups?fixture={fid}
   返回: 每队首发11 + 替补 + 伤停名单(injured/suspended)
   对齐: 用 fixture date + 队名 -> matches.match_id
"""
import sqlite3, os, json, sys

DB = "data/football_data.db"
API_KEY = os.environ.get("API_FOOTBALL_KEY", "<<YOUR_KEY_HERE>>")
API_BASE = "https://v3.football.api-sports.io"
# WC2026 在 api-football 的 league id (需确认, 通常 1=World Cup)
LEAGUE_ID = 1
SEASON = 2026

def main():
    if API_KEY.startswith("<<"):
        print("❌ 请先设置 API_FOOTBALL_KEY (注册 api-football 免费档)")
        sys.exit(1)
    # 1. 拉 WC2026 fixtures 拿 fixture id
    # 2. 逐场 /players/injuries + /fixtures/lineups
    # 3. upsert wc_injuries(fixture_id, team, injured[], suspended[])
    # 4. 对齐 matches.match_id
    print("[template] 接口已就绪, 待 key 激活。设计见文件头注释。")

if __name__ == "__main__":
    # main()  # 激活后取消注释
    print("L4 获取模板: 当前 BLOCKED (需 api-football key)。运行 main() 前先设 API_FOOTBALL_KEY。")
