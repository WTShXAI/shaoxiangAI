"""Database Manager Mixin — 主类"""
import sqlite3
import os
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
from contextlib import contextmanager

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'football_data.db')


class DatabaseManager:
    """DatabaseManager Mixin — 主类"""
"""
哨响AI - SQLite数据库管理模块 (已拆分-Mixin)
============================================
DatabaseManager 通过 Mixin 继承获得全部方法。
Mixin 位于 database/db/ 子包。
拆分: 2026-06-28 (Go File 拆分)
"""
import sqlite3, os, json, logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
from contextlib import contextmanager
from database.db import *

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'football_data.db')


class DatabaseManager(CoreMixin, SchemaMixin, CrudMatchMixin, CrudEntityMixin, CrudPredictionMixin, AnalyticsMixin):
    """SQLite数据库管理器 (Mixin组成)"""
    pass
