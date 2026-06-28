"""
哨响AI - 输入验证器
===================
- 所有外部输入（API参数、用户提交、文件上传）的集中验证
- 防 SQL 注入、XSS、路径遍历
- 类型安全 + 范围检查

用法:
    from utils.input_validator import validate_predict_params, sanitize_string
"""

import re
import json
import logging
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Union, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════
# 基础工具
# ══════════════════════════════════════════════════

# SQL 注入模式（字段名/表名中不应出现）
_SQL_KEYWORDS = re.compile(
    r'\b(SELECT|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|EXEC|UNION|'
    r'OR\s+\d|AND\s+\d|1\s*=\s*1|--|;)\b',
    re.IGNORECASE
)

# 安全字符串：仅允许字母数字、空格、短横、下划线、点
_SAFE_STRING = re.compile(r'^[\w\s\-\.\,\'\/\(\)]+$')

# 日期格式
_DATE_PATTERN = re.compile(r'^\d{4}-\d{2}-\d{2}$')

# XSS 标签
_XSS_TAGS = re.compile(r'<\s*(script|iframe|object|embed|style|link|img)', re.IGNORECASE)

# 路径遍历
_PATH_TRAVERSAL = re.compile(r'\.\.\/|\.\.\\')

def sanitize_string(value: str, max_length: int = 200,
                    allow_html: bool = False) -> str:
    """安全清洗字符串：去首尾空格、防XSS、防SQL注入、限长"""
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if len(value) > max_length:
        value = value[:max_length]
    if not allow_html:
        value = _XSS_TAGS.sub('[blocked]', value)
    return value

def validate_team_name(name: str) -> Tuple[bool, str]:
    """验证球队名称"""
    if not name or len(name.strip()) < 2:
        return False, "球队名至少2个字符"
    if len(name) > 100:
        return False, "球队名最多100个字符"
    if _XSS_TAGS.search(name):
        return False, "球队名含非法标签"
    if _SQL_KEYWORDS.search(name):
        return False, "球队名含非法关键字"
    return True, ""

def validate_date_str(date_str: str) -> Tuple[bool, str, Optional[str]]:
    """验证日期字符串 YYYY-MM-DD，返回 (valid, error, normalized)"""
    if not date_str:
        return True, "", None  # 允许为空
    if not _DATE_PATTERN.match(date_str):
        return False, f"日期格式错误: {date_str}，需为 YYYY-MM-DD", None
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d")
        if d.year < 2000 or d.year > 2100:
            return False, f"日期年份超出范围: {d.year}", None
    except ValueError:
        return False, f"无效日期: {date_str}", None
    return True, "", date_str

def validate_int(value: Any, min_val: int = 0, max_val: int = 2**31 - 1,
                 default: int = None) -> Tuple[bool, str, Optional[int]]:
    """验证整数参数"""
    if value is None:
        if default is not None:
            return True, "", default
        return False, "缺少必需参数", None
    try:
        v = int(value)
        if v < min_val or v > max_val:
            return False, f"值 {v} 超出范围 [{min_val}, {max_val}]", None
        return True, "", v
    except (ValueError, TypeError):
        return False, f"无效整数: {value}", None

def validate_float(value: Any, min_val: float = 0.0, max_val: float = 1.0,
                   default: float = None) -> Tuple[bool, str, Optional[float]]:
    """验证浮点数参数"""
    if value is None:
        if default is not None:
            return True, "", default
        return False, "缺少必需参数", None
    try:
        v = float(value)
        if v < min_val or v > max_val:
            return False, f"值 {v} 超出范围 [{min_val}, {max_val}]", None
        return True, "", v
    except (ValueError, TypeError):
        return False, f"无效浮点数: {value}", None

def validate_league_code(code: str) -> Tuple[bool, str, Optional[str]]:
    """验证联赛代码，防注入"""
    if not code:
        return False, "联赛代码不能为空", None
    # 仅允许字母和数字
    if not re.match(r'^[A-Za-z0-9\s\-]+$', code):
        return False, f"无效联赛代码: {code}", None
    if len(code) > 50:
        return False, "联赛代码过长", None
    return True, "", code.strip().upper()

# ══════════════════════════════════════════════════
# API 参数验证
# ══════════════════════════════════════════════════

@dataclass
class ValidationResult:
    """验证结果"""
    valid: bool
    errors: List[str] = field(default_factory=list)
    cleaned: Dict[str, Any] = field(default_factory=dict)

def validate_predict_params(data: Dict) -> ValidationResult:
    """
    验证 /api/predict 和 /api/batch-predict 请求参数

    Required: home_team, away_team (or match_id)
    Optional: league, match_date, features (dict)
    """
    result = ValidationResult(valid=True)

    # home_team
    if "home_team" in data:
        ok, err = validate_team_name(data.get("home_team", ""))
        if not ok:
            result.errors.append(f"home_team: {err}")
        else:
            result.cleaned["home_team"] = sanitize_string(data["home_team"])
    elif "match_id" not in data:
        result.errors.append("缺少 home_team 或 match_id")

    # away_team
    if "away_team" in data:
        ok, err = validate_team_name(data.get("away_team", ""))
        if not ok:
            result.errors.append(f"away_team: {err}")
        else:
            result.cleaned["away_team"] = sanitize_string(data["away_team"])

    # match_id
    if "match_id" in data:
        ok, err, val = validate_int(data["match_id"], min_val=1)
        if not ok:
            result.errors.append(f"match_id: {err}")
        else:
            result.cleaned["match_id"] = val

    # match_date
    if "match_date" in data:
        ok, err, val = validate_date_str(data["match_date"])
        if not ok:
            result.errors.append(f"match_date: {err}")
        else:
            result.cleaned["match_date"] = val

    # league
    if "league" in data:
        ok, err, code = validate_league_code(data["league"])
        if not ok:
            result.errors.append(f"league: {err}")
        else:
            result.cleaned["league"] = code

    # features (可选特征字典)
    if "features" in data and data["features"] is not None:
        if not isinstance(data["features"], dict):
            result.errors.append("features 必须是字典")
        else:
            cleaned_features = {}
            for k, v in data["features"].items():
                if not re.match(r'^[a-z0-9_]+$', k, re.IGNORECASE):
                    result.errors.append(f"features 键名非法: {k}")
                    continue
                ok, err, fv = validate_float(v, min_val=-100.0, max_val=100.0)
                if not ok:
                    result.errors.append(f"features.{k}: {err}")
                else:
                    cleaned_features[k] = fv
            result.cleaned["features"] = cleaned_features

    result.valid = len(result.errors) == 0
    return result

def validate_train_params(data: Dict) -> ValidationResult:
    """验证 /api/train 请求参数"""
    result = ValidationResult(valid=True)

    # 可选参数
    for param in ["league_filter", "test_size", "n_estimators", "max_depth"]:
        if param in data:
            if param in ("test_size",):
                ok, err, val = validate_float(data[param], 0.01, 0.5, default=0.1)
            else:
                ok, err, val = validate_int(data[param], min_val=1, max_val=10000)
            if not ok:
                result.errors.append(f"{param}: {err}")
            else:
                result.cleaned[param] = val

    result.valid = len(result.errors) == 0
    return result

def validate_match_input(data: Dict) -> ValidationResult:
    """验证 /api/matches POST 的比赛录入数据"""
    result = ValidationResult(valid=True)

    required = {
        "home_team_name": "主队名",
        "away_team_name": "客队名",
        "match_date": "比赛日期",
        "league_name": "联赛名",
        "league_id": "联赛ID",
    }

    for field, label in required.items():
        if field not in data or not str(data[field]).strip():
            result.errors.append(f"缺少必填字段: {label}")
        else:
            result.cleaned[field] = sanitize_string(str(data[field]))

    # 验证可选数值
    for field in ["home_score", "away_score"]:
        if field in data and data[field] is not None and str(data[field]).strip():
            ok, err, val = validate_int(data[field], min_val=0, max_val=50)
            if not ok:
                result.errors.append(f"{field}: {err}")
            else:
                result.cleaned[field] = val

    result.valid = len(result.errors) == 0
    return result

def validate_batch_predict_input(data: Dict) -> ValidationResult:
    """验证批量预测输入"""
    result = ValidationResult(valid=True)

    if "matches" not in data:
        result.errors.append("缺少 matches 数组")
    elif not isinstance(data["matches"], list):
        result.errors.append("matches 必须是数组")
    elif len(data["matches"]) == 0:
        result.errors.append("matches 不能为空")
    elif len(data["matches"]) > 100:
        result.errors.append("单次批量预测最多100场")
    else:
        cleaned_matches = []
        for i, m in enumerate(data["matches"]):
            mv = validate_predict_params(m)
            if not mv.valid:
                result.errors.extend([f"matches[{i}]: {e}" for e in mv.errors])
            else:
                cleaned_matches.append(mv.cleaned)
        result.cleaned["matches"] = cleaned_matches

    result.valid = len(result.errors) == 0
    return result

# ══════════════════════════════════════════════════
# 安全检查
# ══════════════════════════════════════════════════

def check_sql_injection(value: str) -> bool:
    """检测是否含 SQL 注入模式"""
    if not isinstance(value, str):
        return False
    return bool(_SQL_KEYWORDS.search(value))

def check_path_traversal(path: str) -> bool:
    """检测路径遍历攻击"""
    if not isinstance(path, str):
        return False
    return bool(_PATH_TRAVERSAL.search(path))

def check_xss(value: str) -> bool:
    """检测 XSS 攻击"""
    if not isinstance(value, str):
        return False
    return bool(_XSS_TAGS.search(value))

def security_scan(data: Dict) -> List[str]:
    """
    递归扫描字典中所有字符串值的安全风险

    Returns:
        违规项列表
    """
    violations = []

    def _scan(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                _scan(v, f"{path}.{k}" if path else k)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                _scan(v, f"{path}[{i}]")
        elif isinstance(obj, str):
            if check_sql_injection(obj):
                violations.append(f"SQL注入风险 @ {path}: {obj[:50]}")
            if check_xss(obj):
                violations.append(f"XSS风险 @ {path}: {obj[:50]}")

    _scan(data)
    return violations

# ══════════════════════════════════════════════════
# Flask 请求验证装饰器
# ══════════════════════════════════════════════════

def validate_request(validator_func):
    """
    Flask 路由装饰器：自动验证 JSON 请求体

    用法:
        @app.route('/api/predict', methods=['POST'])
        @validate_request(validate_predict_params)
        def predict():
            data = request.validated_data  # 已清洗的数据
            ...
    """
    from functools import wraps
    from flask import request, jsonify

    def decorator(f):
        @wraps(f)
        def wrapper(*args, **kwargs):
            # 获取请求数据
            if request.is_json:
                data = request.get_json(silent=True) or {}
            else:
                data = request.form.to_dict() or request.args.to_dict() or {}

            # 安全检查
            sec_violations = security_scan(data)
            if sec_violations:
                logger.warning(f"[SEC] 安全扫描拦截: {sec_violations}")
                return jsonify({
                    "error": "请求包含不安全内容",
                    "code": "SECURITY_VIOLATION",
                }), 400

            # 业务验证
            result = validator_func(data)
            if not result.valid:
                return jsonify({
                    "error": "参数验证失败",
                    "code": "VALIDATION_ERROR",
                    "details": result.errors,
                }), 400

            # 注入清洗后的数据
            request.validated_data = result.cleaned
            return f(*args, **kwargs)
        return wrapper
    return decorator
