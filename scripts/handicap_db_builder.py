#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
handicap_db_builder.py — Build structured handicap DB from 70 WC2026 screenshots.
Transcribed manually via multimodal Read tool on 2026-07-08.
Outputs: odds_db/handicap_db.json + analysis report.
"""
import json
import os
import sys
from datetime import datetime

DB_ROOT = r"D:\Architecture\odds_db"
OUTPUT_JSON = os.path.join(DB_ROOT, "handicap_db.json")
OUTPUT_REPORT = os.path.join(DB_ROOT, "handicap_analysis_report.md")

# ── All 70 records transcribed from screenshots ──
# Schema: home, away, date_gmt8, time_gmt8,
#   oh/od/oa (primary 1X2),
#   hcp_line/hcp_home_odds/hcp_away_odds (primary Asian Handicap),
#   ou_line/ou_over/ou_under (primary O/U),
#   hcp_depth_label (auto-computed)

RECORDS = [
    # === MD1: 6.13 ===
    {"home": "加拿大", "away": "波黑", "date": "2024-06-13", "time": "03:00",
     "oh": 1.84, "od": 3.45, "oa": 4.60,
     "hcp_line": -0.5, "hcp_ho": 1.85, "hcp_ao": 2.07,
     "ou_line": "2/2.5", "ou_o": 2.04, "ou_u": 1.86},

    {"home": "美国", "away": "巴拉圭", "date": "2024-06-13", "time": "09:00",
     "oh": 2.10, "od": 3.20, "oa": 3.75,
     "hcp_line": -0.5, "hcp_ho": 2.12, "hcp_ao": 1.81,
     "ou_line": "2", "ou_o": 1.88, "ou_u": 2.02},

    # === MD2: 6.14 ===
    {"home": "卡塔尔", "away": "瑞士", "date": "2024-06-14", "time": "03:00",
     "oh": 13.0, "od": 6.70, "oa": 1.21,
     "hcp_line": -2.0, "hcp_ho": 1.99, "hcp_ao": 2.07,
     "ou_line": "3", "ou_o": 1.99, "ou_u": 1.91},

    {"home": "巴西", "away": "摩洛哥", "date": "2024-06-14", "time": "06:00",
     "oh": 1.70, "od": 3.60, "oa": 5.30,
     "hcp_line": -0.75, "hcp_ho": 1.91, "hcp_ao": 2.01,
     "ou_line": "2/2.5", "ou_o": 1.99, "ou_u": 1.91},

    {"home": "海地", "away": "苏格兰", "date": "2024-06-14", "time": "09:00",
     "oh": 5.90, "od": 4.60, "oa": 1.49,
     "hcp_line": 1.0, "hcp_ho": 2.08, "hcp_ao": 1.84,
     "ou_line": "2.5/3", "ou_o": 1.83, "ou_u": 2.07},

    {"home": "澳大利亚", "away": "土耳其", "date": "2024-06-14", "time": "12:00",
     "oh": 4.95, "od": 3.75, "oa": 1.71,
     "hcp_line": 0.75, "hcp_ho": 1.99, "hcp_ao": 1.93,
     "ou_line": "2.5", "ou_o": 2.08, "ou_u": 1.82},

    # === MD3: 6.15 ===
    {"home": "德国", "away": "库拉索", "date": "2024-06-15", "time": "01:00",
     "oh": 1.03, "od": 18.5, "oa": 23.0,
     "hcp_line": -3.5, "hcp_ho": 1.99, "hcp_ao": 1.91,
     "ou_line": "4/4.5", "ou_o": 1.88, "ou_u": 2.00},

    {"home": "瑞典", "away": "突尼斯", "date": "2024-06-15", "time": "10:00",
     "oh": 1.92, "od": 3.40, "oa": 4.10,
     "hcp_line": -0.5, "hcp_ho": 1.92, "hcp_ao": 2.00,
     "ou_line": "2/2.5", "ou_o": 1.99, "ou_u": 1.91},

    {"home": "科特迪瓦", "away": "厄瓜多尔", "date": "2024-06-15", "time": "07:00",
     "oh": 3.50, "od": 2.88, "oa": 2.36,
     "hcp_line": 0.0, "hcp_ho": 1.89, "hcp_ao": 2.03,
     "ou_line": "1.5/2", "ou_o": 1.83, "ou_u": 2.07},

    {"home": "荷兰", "away": "日本", "date": "2024-06-15", "time": "04:00",
     "oh": 2.03, "od": 3.50, "oa": 3.60,
     "hcp_line": -0.5, "hcp_ho": 2.03, "hcp_ao": 1.89,
     "ou_line": "2.5", "ou_o": 2.00, "ou_u": 1.90},

    # === MD4: 6.16 ===
    {"home": "伊朗", "away": "新西兰", "date": "2024-06-16", "time": "09:00",
     "oh": 1.85, "od": 3.35, "oa": 4.55,
     "hcp_line": -0.5, "hcp_ho": 1.85, "hcp_ao": 2.05,
     "ou_line": "2", "ou_o": 1.81, "ou_u": 2.07},

    {"home": "比利时", "away": "埃及", "date": "2024-06-16", "time": "03:00",
     "oh": 1.63, "od": 4.00, "oa": 5.20,
     "hcp_line": -0.75, "hcp_ho": 1.82, "hcp_ao": 2.08,
     "ou_line": "2.5", "ou_o": 2.03, "ou_u": 1.85},

    {"home": "沙特阿拉伯", "away": "乌拉圭", "date": "2024-06-16", "time": "06:00",
     "oh": 7.30, "od": 4.45, "oa": 1.45,
     "hcp_line": 1.25, "hcp_ho": 1.83, "hcp_ao": 2.07,
     "ou_line": "2.5", "ou_o": 2.03, "ou_u": 1.85},

    {"home": "西班牙", "away": "佛得角共和国", "date": "2024-06-16", "time": "00:00",
     "oh": 1.09, "od": 10.0, "oa": 19.0,
     "hcp_line": -2.5, "hcp_ho": 1.90, "hcp_ao": 2.00,
     "ou_line": "3.5", "ou_o": 1.99, "ou_u": 1.89},

    # === MD5: 6.17 ===
    {"home": "伊拉克", "away": "挪威", "date": "2024-06-17", "time": "06:00",
     "oh": 13.5, "od": 6.80, "oa": 1.20,
     "hcp_line": 2.0, "hcp_ho": 1.85, "hcp_ao": 2.05,
     "ou_line": "3", "ou_o": 1.93, "ou_u": 1.95},

    {"home": "奥地利", "away": "约旦", "date": "2024-06-17", "time": "12:00",
     "oh": 1.33, "od": 5.30, "oa": 8.70,
     "hcp_line": -1.5, "hcp_ho": 1.99, "hcp_ao": 1.91,
     "ou_line": "2.5/3", "ou_o": 1.85, "ou_u": 2.03},

    {"home": "法国", "away": "塞内加尔", "date": "2024-06-17", "time": "03:00",
     "oh": 1.45, "od": 4.40, "oa": 6.70,
     "hcp_line": -1.0, "hcp_ho": 1.82, "hcp_ao": 2.08,
     "ou_line": "2.5", "ou_o": 1.99, "ou_u": 1.89},

    {"home": "阿根廷", "away": "阿尔及利亚", "date": "2024-06-17", "time": "09:00",
     "oh": 1.40, "od": 4.70, "oa": 7.90,
     "hcp_line": -1.25, "hcp_ho": 1.94, "hcp_ao": 1.96,
     "ou_line": "2.5", "ou_o": 1.97, "ou_u": 1.91},

    # === MD6: 6.18 ===
    {"home": "乌兹别克斯坦", "away": "哥伦比亚", "date": "2024-06-18", "time": "10:00",
     "oh": 8.40, "od": 4.70, "oa": 1.38,
     "hcp_line": 1.25, "hcp_ho": 1.99, "hcp_ao": 1.91,
     "ou_line": "2.5", "ou_o": 2.01, "ou_u": 1.87},

    {"home": "加纳", "away": "巴拿马", "date": "2024-06-18", "time": "07:00",
     "oh": 2.19, "od": 3.30, "oa": 3.35,
     "hcp_line": 0.0, "hcp_ho": 1.89, "hcp_ao": 2.01,
     "ou_line": "2/2.5", "ou_o": 2.04, "ou_u": 1.84},

    {"home": "英格兰", "away": "克罗地亚", "date": "2024-06-18", "time": "04:00",
     "oh": 1.73, "od": 3.65, "oa": 4.95,
     "hcp_line": -0.75, "hcp_ho": 1.98, "hcp_ao": 1.92,
     "ou_line": "2/2.5", "ou_o": 1.92, "ou_u": 1.96},

    {"home": "葡萄牙", "away": "民主刚果", "date": "2024-06-18", "time": "01:00",
     "oh": 1.28, "od": 5.60, "oa": 10.0,
     "hcp_line": -1.5, "hcp_ho": 1.87, "hcp_ao": 2.03,
     "ou_line": "2.5/3", "ou_o": 1.92, "ou_u": 1.96},

    # === MD7: 6.19 ===
    {"home": "加拿大", "away": "卡塔尔", "date": "2024-06-19", "time": "06:00",
     "oh": 1.31, "od": 5.20, "oa": 9.80,
     "hcp_line": -1.5, "hcp_ho": 1.99, "hcp_ao": 1.91,
     "ou_line": "2.5", "ou_o": 1.85, "ou_u": 2.03},

    {"home": "墨西哥", "away": "韩国", "date": "2024-06-19", "time": "09:00",
     "oh": 2.03, "od": 3.25, "oa": 3.95,
     "hcp_line": -0.5, "hcp_ho": 2.03, "hcp_ao": 1.87,
     "ou_line": "2", "ou_o": 1.85, "ou_u": 2.03},

    {"home": "捷克", "away": "南非", "date": "2024-06-19", "time": "00:00",
     "oh": 1.82, "od": 3.60, "oa": 4.35,
     "hcp_line": -0.75, "hcp_ho": 2.08, "hcp_ao": 1.82,
     "ou_line": "2/2.5", "ou_o": 1.83, "ou_u": 2.05},

    {"home": "瑞士", "away": "波黑", "date": "2024-06-19", "time": "03:00",
     "oh": 1.58, "od": 4.05, "oa": 5.70,
     "hcp_line": -1.0, "hcp_ho": 2.06, "hcp_ao": 1.84,
     "ou_line": "2/2.5", "ou_o": 1.83, "ou_u": 2.05},

    # === MD8: 6.20 ===
    {"home": "土耳其", "away": "巴拉圭", "date": "2024-06-20", "time": "11:00",
     "oh": 2.03, "od": 3.15, "oa": 3.60,
     "hcp_line": -0.5, "hcp_ho": 2.03, "hcp_ao": 1.81,
     "ou_line": "2/2.5", "ou_o": 2.03, "ou_u": 1.79},

    {"home": "巴西", "away": "海地", "date": "2024-06-20", "time": "08:30",
     "oh": 1.06, "od": 10.5, "oa": 17.5,
     "hcp_line": -2.75, "hcp_ho": 1.88, "hcp_ao": 1.96,
     "ou_line": "3.5/4", "ou_o": 1.90, "ou_u": 1.92},

    {"home": "美国", "away": "澳大利亚", "date": "2024-06-20", "time": "03:00",
     "oh": 1.55, "od": 3.95, "oa": 5.30,
     "hcp_line": -1.0, "hcp_ho": 2.02, "hcp_ao": 1.82,
     "ou_line": "2.5", "ou_o": 1.98, "ou_u": 1.84},

    {"home": "苏格兰", "away": "摩洛哥", "date": "2024-06-20", "time": "06:00",
     "oh": 3.70, "od": 3.15, "oa": 2.00,
     "hcp_line": 0.5, "hcp_ho": 1.84, "hcp_ao": 2.00,
     "ou_line": "2/2.5", "ou_o": 2.02, "ou_u": 1.80},

    # === MD9: 6.21 ===
    {"home": "厄瓜多尔", "away": "库拉索", "date": "2024-06-21", "time": "08:00",
     "oh": 1.19, "od": 6.10, "oa": 12.5,
     "hcp_line": -1.75, "hcp_ho": 1.84, "hcp_ao": 2.00,
     "ou_line": "2.5/3", "ou_o": 1.87, "ou_u": 1.95},

    {"home": "德国", "away": "科特迪瓦", "date": "2024-06-21", "time": "04:00",
     "oh": 1.53, "od": 4.15, "oa": 5.20,
     "hcp_line": -1.0, "hcp_ho": 1.96, "hcp_ao": 1.88,
     "ou_line": "2.5/3", "ou_o": 2.01, "ou_u": 1.81},

    {"home": "突尼斯", "away": "日本", "date": "2024-06-21", "time": "12:00",
     "oh": 4.90, "od": 3.45, "oa": 1.69,
     "hcp_line": 0.75, "hcp_ho": 1.90, "hcp_ao": 1.94,
     "ou_line": "2/2.5", "ou_o": 1.96, "ou_u": 1.86},

    {"home": "荷兰", "away": "瑞典", "date": "2024-06-21", "time": "01:00",
     "oh": 1.63, "od": 3.90, "oa": 4.70,
     "hcp_line": -0.75, "hcp_ho": 1.83, "hcp_ao": 2.01,
     "ou_line": "2.5", "ou_o": 1.90, "ou_u": 1.92},

    # === MD10: 6.22 ===
    {"home": "乌拉圭", "away": "佛得角共和国", "date": "2024-06-22", "time": "06:00",
     "oh": 1.44, "od": 4.25, "oa": 6.30,
     "hcp_line": -1.25, "hcp_ho": 2.07, "hcp_ao": 1.77,
     "ou_line": "2.5", "ou_o": 1.90, "ou_u": 1.92},

    {"home": "新西兰", "away": "埃及", "date": "2024-06-22", "time": "09:00",
     "oh": 4.55, "od": 3.35, "oa": 1.76,
     "hcp_line": 0.75, "hcp_ho": 1.81, "hcp_ao": 2.03,
     "ou_line": "2/2.5", "ou_o": 1.99, "ou_u": 1.83},

    {"home": "比利时", "away": "伊朗", "date": "2024-06-22", "time": "03:00",
     "oh": 1.39, "od": 4.50, "oa": 7.10,
     "hcp_line": -1.25, "hcp_ho": 1.95, "hcp_ao": 1.89,
     "ou_line": "2.5", "ou_o": 1.87, "ou_u": 1.95},

    {"home": "西班牙", "away": "沙特阿拉伯", "date": "2024-06-22", "time": "00:00",
     "oh": 1.08, "od": 8.80, "oa": 18.0,
     "hcp_line": -2.5, "hcp_ho": 2.01, "hcp_ao": 1.83,
     "ou_line": "3/3.5", "ou_o": 2.01, "ou_u": 1.81},

    # === MD11: 6.23 ===
    {"home": "挪威", "away": "塞内加尔", "date": "2024-06-23", "time": "08:00",
     "oh": 2.14, "od": 3.40, "oa": 3.10,
     "hcp_line": 0.0, "hcp_ho": 1.87, "hcp_ao": 1.97,
     "ou_line": "2.5", "ou_o": 1.97, "ou_u": 1.85},

    {"home": "法国", "away": "伊拉克", "date": "2024-06-23", "time": "05:00",
     "oh": 1.08, "od": 8.80, "oa": 20.0,
     "hcp_line": -2.5, "hcp_ho": 2.01, "hcp_ao": 1.83,
     "ou_line": "3/3.5", "ou_o": 2.02, "ou_u": 1.80},

    {"home": "约旦", "away": "阿尔及利亚", "date": "2024-06-23", "time": "11:00",
     "oh": 6.20, "od": 4.15, "oa": 1.46,
     "hcp_line": 1.0, "hcp_ho": 2.02, "hcp_ao": 1.82,
     "ou_line": "2.5", "ou_o": 1.97, "ou_u": 1.85},

    {"home": "阿根廷", "away": "奥地利", "date": "2024-06-23", "time": "01:00",
     "oh": 1.60, "od": 3.85, "oa": 5.00,
     "hcp_line": -0.75, "hcp_ho": 1.80, "hcp_ao": 2.04,
     "ou_line": "2.5", "ou_o": 2.00, "ou_u": 1.82},

    # === MD12: 6.24 ===
    {"home": "哥伦比亚", "away": "民主刚果", "date": "2024-06-24", "time": "10:00",
     "oh": 1.44, "od": 4.05, "oa": 6.90,
     "hcp_line": -1.0, "hcp_ho": 1.79, "hcp_ao": 2.05,
     "ou_line": "2/2.5", "ou_o": 1.93, "ou_u": 1.89},

    {"home": "巴拿马", "away": "克罗地亚", "date": "2024-06-24", "time": "07:00",
     "oh": 5.70, "od": 3.95, "oa": 1.52,
     "hcp_line": 1.0, "hcp_ho": 1.87, "hcp_ao": 1.97,
     "ou_line": "2/2.5", "ou_o": 1.79, "ou_u": 2.03},

    {"home": "英格兰", "away": "加纳", "date": "2024-06-24", "time": "04:00",
     "oh": 1.30, "od": 5.00, "oa": 8.30,
     "hcp_line": -1.5, "hcp_ho": 1.97, "hcp_ao": 1.87,
     "ou_line": "2.5/3", "ou_o": 1.90, "ou_u": 1.92},

    {"home": "葡萄牙", "away": "乌兹别克斯坦", "date": "2024-06-24", "time": "01:00",
     "oh": 1.22, "od": 5.90, "oa": 10.0,
     "hcp_line": -1.75, "hcp_ho": 1.90, "hcp_ao": 1.94,
     "ou_line": "3", "ou_o": 1.93, "ou_u": 1.89},

    # === MD13: 6.25 (last group stage) ===
    {"home": "南非", "away": "韩国", "date": "2024-06-25", "time": "09:00",
     "oh": 4.95, "od": 3.80, "oa": 1.61,
     "hcp_line": 0.75, "hcp_ho": 2.03, "hcp_ao": 1.81,
     "ou_line": "2.5", "ou_o": 2.03, "ou_u": 1.79},

    {"home": "捷克", "away": "墨西哥", "date": "2024-06-25", "time": "09:00",
     "oh": 4.25, "od": 3.35, "oa": 1.81,
     "hcp_line": 0.5, "hcp_ho": 2.03, "hcp_ao": 1.81,
     "ou_line": "2/2.5", "ou_o": 1.90, "ou_u": 1.92},

    {"home": "摩洛哥", "away": "海地", "date": "2024-06-25", "time": "06:00",
     "oh": 1.34, "od": 4.85, "oa": 7.50,
     "hcp_line": -1.25, "hcp_ho": 1.80, "hcp_ao": 2.04,
     "ou_line": "2.5/3", "ou_o": 1.93, "ou_u": 1.89},

    {"home": "波黑", "away": "卡塔尔", "date": "2024-06-25", "time": "03:00",
     "oh": 1.61, "od": 3.75, "oa": 5.00,
     "hcp_line": -0.75, "hcp_ho": 1.82, "hcp_ao": 2.02,
     "ou_line": "2/2.5", "ou_o": 1.83, "ou_u": 1.99},

    {"home": "瑞士", "away": "加拿大", "date": "2024-06-25", "time": "03:00",
     "oh": 2.12, "od": 3.25, "oa": 3.25,
     "hcp_line": 0.0, "hcp_ho": 1.84, "hcp_ao": 2.00,
     "ou_line": "2/2.5", "ou_o": 1.87, "ou_u": 1.95},

    {"home": "苏格兰", "away": "巴西", "date": "2024-06-25", "time": "06:00",
     "oh": 6.90, "od": 4.50, "oa": 1.40,
     "hcp_line": 1.25, "hcp_ho": 1.88, "hcp_ao": 1.96,
     "ou_line": "2.5", "ou_o": 1.82, "ou_u": 2.00},

    # === R16: 6.26 ===
    {"home": "厄瓜多尔", "away": "德国", "date": "2024-06-26", "time": "04:00",
     "oh": 4.45, "od": 3.55, "oa": 1.72,
     "hcp_line": 0.75, "hcp_ho": 1.88, "hcp_ao": 1.96,
     "ou_line": "2.5", "ou_o": 1.95, "ou_u": 1.87},

    {"home": "土耳其", "away": "美国", "date": "2024-06-26", "time": "10:00",
     "oh": 2.60, "od": 3.50, "oa": 2.41,
     "hcp_line": 0.0, "hcp_ho": 2.00, "hcp_ao": 1.84,
     "ou_line": "2.5", "ou_o": 1.86, "ou_u": 1.96},

    {"home": "巴拉圭", "away": "澳大利亚", "date": "2024-06-26", "time": "10:00",
     "oh": 2.09, "od": 3.20, "oa": 3.40,
     "hcp_line": 0.0, "hcp_ho": 1.81, "hcp_ao": 2.03,
     "ou_line": "2/2.5", "ou_o": 2.02, "ou_u": 1.80},

    {"home": "库拉索", "away": "科特迪瓦", "date": "2024-06-26", "time": "04:00",
     "oh": 11.0, "od": 5.80, "oa": 1.21,
     "hcp_line": 1.75, "hcp_ho": 1.91, "hcp_ao": 1.93,
     "ou_line": "2.5/3", "ou_o": 1.88, "ou_u": 1.94},

    {"home": "日本", "away": "瑞典", "date": "2024-06-26", "time": "07:00",
     "oh": 2.11, "od": 3.30, "oa": 3.25,
     "hcp_line": 0.0, "hcp_ho": 1.84, "hcp_ao": 2.00,
     "ou_line": "2/2.5", "ou_o": 1.83, "ou_u": 1.99},

    {"home": "突尼斯", "away": "荷兰", "date": "2024-06-26", "time": "07:00",
     "oh": 5.80, "od": 4.15, "oa": 1.49,
     "hcp_line": 1.0, "hcp_ho": 1.97, "hcp_ao": 1.87,
     "ou_line": "2.5", "ou_o": 1.90, "ou_u": 1.92},

    # === R16/QF: 6.27 ===
    {"home": "乌拉圭", "away": "西班牙", "date": "2024-06-27", "time": "08:00",
     "oh": 4.70, "od": 3.90, "oa": 1.63,
     "hcp_line": 0.75, "hcp_ho": 2.01, "hcp_ao": 1.83,
     "ou_line": "2.5", "ou_o": 1.90, "ou_u": 1.92},

    {"home": "佛得角共和国", "away": "沙特阿拉伯", "date": "2024-06-27", "time": "08:00",
     "oh": 2.47, "od": 3.35, "oa": 2.62,
     "hcp_line": 0.0, "hcp_ho": 1.86, "hcp_ao": 1.98,
     "ou_line": "2/2.5", "ou_o": 1.82, "ou_u": 2.00},

    {"home": "埃及", "away": "伊朗", "date": "2024-06-27", "time": "11:00",
     "oh": 2.16, "od": 3.00, "oa": 3.40,
     "hcp_line": 0.0, "hcp_ho": 1.86, "hcp_ao": 1.98,
     "ou_line": "2", "ou_o": 1.96, "ou_u": 1.86},

    {"home": "塞内加尔", "away": "伊拉克", "date": "2024-06-27", "time": "03:00",
     "oh": 1.40, "od": 4.40, "oa": 7.00,
     "hcp_line": -1.25, "hcp_ho": 1.98, "hcp_ao": 1.86,
     "ou_line": "2.5", "ou_o": 1.90, "ou_u": 1.92},

    {"home": "挪威", "away": "法国", "date": "2024-06-27", "time": "03:00",
     "oh": 4.05, "od": 3.55, "oa": 1.80,
     "hcp_line": 0.5, "hcp_ho": 2.04, "hcp_ao": 1.80,
     "ou_line": "2.5", "ou_o": 1.90, "ou_u": 1.92},

    {"home": "新西兰", "away": "比利时", "date": "2024-06-27", "time": "11:00",
     "oh": 9.00, "od": 5.20, "oa": 1.28,
     "hcp_line": 1.5, "hcp_ho": 1.95, "hcp_ao": 1.89,
     "ou_line": "2.5/3", "ou_o": 1.90, "ou_u": 1.92},

    # === QF: 6.28 ===
    {"home": "克罗地亚", "away": "加纳", "date": "2024-06-28", "time": "05:00",
     "oh": 1.62, "od": 3.75, "oa": 5.00,
     "hcp_line": -0.75, "hcp_ho": 1.83, "hcp_ao": 2.01,
     "ou_line": "2/2.5", "ou_o": 1.83, "ou_u": 1.99},

    {"home": "哥伦比亚", "away": "葡萄牙", "date": "2024-06-28", "time": "07:30",
     "oh": 3.25, "od": 3.30, "oa": 2.11,
     "hcp_line": 0.0, "hcp_ho": 2.01, "hcp_ao": 1.83,
     "ou_line": "2/2.5", "ou_o": 1.84, "ou_u": 1.98},

    {"home": "巴拿马", "away": "英格兰", "date": "2024-06-28", "time": "05:00",
     "oh": 9.10, "od": 5.40, "oa": 1.27,
     "hcp_line": 1.5, "hcp_ho": 2.00, "hcp_ao": 1.84,
     "ou_line": "2.5/3", "ou_o": 1.81, "ou_u": 2.01},

    {"home": "民主刚果", "away": "乌兹别克斯坦", "date": "2024-06-28", "time": "07:30",
     "oh": 2.27, "od": 3.25, "oa": 2.97,
     "hcp_line": 0.0, "hcp_ho": 2.01, "hcp_ao": 1.83,
     "ou_line": "2/2.5", "ou_o": 2.00, "ou_u": 1.82},

    {"home": "约旦", "away": "阿根廷", "date": "2024-06-28", "time": "10:00",
     "oh": 12.0, "od": 6.30, "oa": 1.18,
     "hcp_line": 2.0, "hcp_ho": 1.80, "hcp_ao": 2.04,
     "ou_line": "3", "ou_o": 1.97, "ou_u": 1.85},

    {"home": "阿尔及利亚", "away": "奥地利", "date": "2024-06-28", "time": "10:00",
     "oh": 3.30, "od": 3.25, "oa": 2.11,
     "hcp_line": 0.0, "hcp_ho": 2.01, "hcp_ao": 1.83,
     "ou_line": "2/2.5", "ou_o": 1.86, "ou_u": 1.96},

    # === LIVE (7.2): USA vs Bosnia in-play ===
    {"home": "美国", "away": "波黑", "date": "2024-07-02", "time": "LIVE 51'",
     "oh": 1.12, "od": 8.20, "oa": 24.0,
     "hcp_line": -0.75, "hcp_ho": 1.91, "hcp_ao": 1.99,
     "ou_line": "2.5/3", "ou_o": 2.05, "ou_u": 1.83,
     "_note": "LIVE in-play, score 1-0 at 51'"},
]

def depth_label(line):
    """Classify handicap depth."""
    abs_l = abs(line)
    if abs_l >= 1.5: return "deep"
    if abs_l >= 0.75: return "medium-deep"
    if abs_l >= 0.25: return "shallow"
    return "level"

def compute_implied_prob(oh, od, oa):
    """Demargin 1X2 to implicit probabilities."""
    inv = [1/oh, 1/od, 1/oa]
    margin = sum(inv) - 1
    return [i / (1 + margin) for i in inv]  # ph, pd, pa

def hcp_direction(r):
    """Determine which side is favored by handicap line+odds.
    Returns 'home' or 'away' (the side bookmaker expects to cover the spread).
    """
    line = r['hcp_line']
    ho = r['hcp_ho']
    ao = r['hcp_ao']
    # Lower odds = favorite side
    if ho < ao:
        return 'home'
    else:
        return 'away'

def x12_direction(r):
    """1X2 argmax direction."""
    oh, od, oa = r['oh'], r['od'], r['oa']
    probs = compute_implied_prob(oh, od, oa)
    idx = probs.index(max(probs))
    return ['home', 'draw', 'away'][idx]


def main():
    # Enrich records
    for r in RECORDS:
        r['hcp_depth'] = depth_label(r['hcp_line'])
        r['hcp_dir'] = hcp_direction(r)
        r['x12_dir'] = x12_direction(r)
        probs = compute_implied_prob(r['oh'], r['od'], r['oa'])
        r['p_h'], r['p_d'], r['p_a'] = probs
        # Overround
        inv_sum = 1/r['oh'] + 1/r['od'] + 1/r['oa']
        r['overround_pct'] = round((inv_sum - 1) * 100, 1)

    # Write JSON
    db = {
        "schema": "handicap_db_v1",
        "source": "世界杯_screenshots_72_images",
        "created": "2026-07-08T01:17:00",
        "method": "manual_multimodal_transcription_by_zhaotongchou",
        "total_records": len(RECORDS),
        "records": RECORDS
    }
    os.makedirs(DB_ROOT, exist_ok=True)
    with open(OUTPUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(db, f, ensure_ascii=False, indent=2)
    print("JSON written: %s (%d records)" % (OUTPUT_JSON, len(RECORDS)))

    # ── Analysis ──
    lines = []
    lines.append("# WC2026 让球盘数据库分析报告\n")
    lines.append("**生成时间**: 2026-07-08 | **来源**: 70张赔率截图 (手动多模态转录)\n")

    # 1. Depth distribution
    from collections import Counter
    depths = Counter(r['hcp_depth'] for r in RECORDS)
    lines.append("## 1. 盘口深度分布\n")
    lines.append("| 深度 | 数量 | 占比 |")
    lines.append("|------|------|------|")
    for d in ['level', 'shallow', 'medium-deep', 'deep']:
        n = depths.get(d, 0)
        pct = round(n/len(RECORDS)*100, 1)
        label = {'level': '平手(0)', 'shallow': '浅让(<0.75)', 'medium-deep': '中深(0.75-1.49)', 'deep': '深让(≥1.5)'}[d]
        lines.append("| %s | %d | %.1f%% |" % (label, n, pct))

    # 2. Direction consistency: HCP vs 1X2
    consistent = sum(1 for r in RECORDS
                    if (r['hcp_dir'] == 'home' and r['x12_dir'] == 'home') or
                       (r['hcp_dir'] == 'away' and r['x12_dir'] == 'away'))
    inconsistent = len(RECORDS) - consistent
    # Note: HCP can be draw-neutral while 1X2 picks draw; count those as semi-consistent
    hcp_draw_like = sum(1 for r in RECORDS if abs(r['hcp_line']) < 0.25)
    lines.append("\n## 2. 让球方向 vs 1X2 方向一致性\n")
    lines.append("- **完全一致** (同方向主胜/客胜): %d/%d (%.1f%%)" % (consistent, len(RECORDS), consistent/len(RECORDS)*100))
    lines.append("- **方向分歧**: %d/%d (%.1f%%)" % (inconsistent, len(RECORDS), inconsistent/len(RECORDS)*100))
    lines.append("- **平手盘口** (hcp≈0): %d 场 (让球无方向偏好)" % hcp_draw_like)

    # 3. Overround stats
    ovrds = [r['overround_pct'] for r in RECORDS]
    lines.append("\n## 3. 抽水率统计\n")
    lines.append("- 平均抽水: %.1f%%" % (sum(ovrds)/len(ovrds)))
    lines.append("- 最低抽水: %.1f%%" % min(ovrds))
    lines.append("- 最高抽水: %.1f%%" % max(ovrds))

    # 4. Implied draw probability distribution
    p_draws = [r['p_d']*100 for r in RECORDS]
    lines.append("\n## 4. 隐含平局概率分布\n")
    lines.append("- P(平局) 均值: %.1f%%" % (sum(p_draws)/len(p_draws)))
    lines.append("- P(平局) ≥26%% (alert阈值): %d/%d 场" %
                 (sum(1 for p in p_draws if p >= 26), len(RECORDS)))
    lines.append("- P(平局) ≥30%%: %d/%d 场" %
                 (sum(1 for p in p_draws if p >= 30), len(RECORDS)))

    # 5. Deep handicap favorites (|line|≥1.5) list
    deep_favs = [(r['home'], r['away'], r['hcp_line'], r['hcp_dir'])
                  for r in RECORDS if r['hcp_depth'] == 'deep']
    lines.append("\n## 5. 深让盘口 (|线|≥1.5) 汇总 (%d场)\n" % len(deep_favs))
    lines.append("| 主队 | 客队 | 盘口 | 让球方向 |")
    lines.append("|------|------|------|----------|")
    for h, a, ln, dr in deep_favs:
        side = "主让" if ln < 0 else "客让"
        lines.append("| %s | %s | %.2f(%s) | %s |" % (h, a, ln, side, dr))

    # 6. Level handicap matches (hcp=0)
    level_matches = [r for r in RECORDS if r['hcp_depth'] == 'level']
    lines.append("\n## 6. 平手盘口匹配 (hcp=0, %d场)\n" % len(level_matches))
    lines.append("| 主队 | 客队 | 日期 | 1X2方向 |")
    lines.append("|------|------|------|----------|")
    for r in level_matches:
        dir_map = {'home': '主胜', 'draw': '平局', 'away': '客胜'}
        lines.append("| %s | %s | %s | %s(%.1f%%) |" %
                     (r['home'], r['away'], r['date'],
                      dir_map[r['x12_dir']], r[{'home':'p_h','draw':'p_d','away':'p_a'}[r['x12_dir']]]*100))

    # 7. Key insight: TaoGe 四维验证
    deep_home_wins = sum(1 for r in RECORDS
                        if r['hcp_depth'] == 'deep' and r['hcp_line'] < 0)  # home gives deep
    deep_away_wins = sum(1 for r in RECORDS
                        if r['hcp_depth'] == 'deep' and r['hcp_line'] > 0)  # away gives deep
    lines.append("\n## 7. 四维铁律 (TaoGe) 验证\n")
    lines.append("- **深让主胜**: %d 场 (主让≥1.5球)" % deep_home_wins)
    lines.append("- **深让客胜**: %d 场 (客让≥1.5球, 即主受深让→客大热)" % deep_away_wins)
    lines.append("- **规则**: 深让→\"胜+平\", 浅让→\"胜+平\", 永不让负")

    # Write report
    report = "\n".join(lines)
    with open(OUTPUT_REPORT, 'w', encoding='utf-8') as f:
        f.write(report)
    print("Report written: %s" % OUTPUT_REPORT)
    print("\n--- Report Preview ---")
    print(report[:2000])


if __name__ == '__main__':
    main()
