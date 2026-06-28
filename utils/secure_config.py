"""
哨响AI - 安全配置管理
=====================
- 密钥生命周期管理：加载 / 验证 / 轮换提醒 / 日志脱敏
- 最小权限检查
- 环境变量注入

用法:
    config = SecureConfig()
    api_key = config.get("FOOTBALL_DATA_API_KEY")
    config.validate_all()
"""

import os
import re
import logging
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Set
from pathlib import Path

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════
# 密钥定义
# ══════════════════════════════════════════════════

class KeyDefinition:
    """API 密钥/机密定义"""

    def __init__(self, env_var: str, key_type: str,
                 required: bool = True, min_length: int = 8,
                 description: str = "", rotation_days: int = 90):
        self.env_var = env_var
        self.key_type = key_type       # api_key | secret | token | password
        self.required = required
        self.min_length = min_length
        self.description = description
        self.rotation_days = rotation_days  # 建议轮换间隔（天）

    @property
    def value(self) -> Optional[str]:
        return os.getenv(self.env_var, "").strip() or None

# ══════════════════════════════════════════════════
# 安全配置管理器
# ══════════════════════════════════════════════════

class SecureConfig:
    """
    集中密钥管理

    从 .env 文件加载，验证格式和完整性，
    脱敏输出（日志中不泄露完整密钥）。
    """

    def __init__(self, env_file: str = None, project_root: str = None):
        if project_root is None:
            project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        self.project_root = project_root
        self.env_file = env_file or os.path.join(project_root, ".env")

        # 加载 .env
        self._load_env_file()

        # 定义所有密钥/配置
        self._keys: List[KeyDefinition] = [
            KeyDefinition(
                "FOOTBALL_DATA_API_KEY", "api_key",
                required=True, min_length=16,
                description="Football-Data.org API v4 密钥",
                rotation_days=180,
            ),
            KeyDefinition(
                "FLASK_SECRET_KEY", "secret",
                required=True, min_length=32,
                description="Flask session 签名密钥",
                rotation_days=90,
            ),
            KeyDefinition(
                "API_AUTH_TOKEN", "token",
                required=False, min_length=16,
                description="外部 API 认证 Token",
                rotation_days=90,
            ),
            KeyDefinition(
                "THE_ODDS_API_KEY", "api_key",
                required=False, min_length=8,
                description="The Odds API 密钥",
                rotation_days=180,
            ),
            KeyDefinition(
                "RAPIDAPI_KEY", "api_key",
                required=False, min_length=8,
                description="RapidAPI (API-Football) 密钥",
                rotation_days=180,
            ),
            KeyDefinition(
                "REDIS_URL", "password",
                required=False, min_length=1,
                description="Redis 连接 URL（含密码）",
                rotation_days=180,
            ),
        ]

        # 加载时间
        self._loaded_at = datetime.now(timezone.utc)

    def _load_env_file(self):
        """手动加载 .env 到 os.environ（兼容 python-dotenv 缺失）"""
        if not os.path.isfile(self.env_file):
            logger.info(f"[SecConfig] .env 文件不存在: {self.env_file}")
            return

        with open(self.env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                # 仅设置未配置的变量（不覆盖已有环境变量）
                if key and not os.getenv(key):
                    os.environ[key] = value

        logger.info(f"[SecConfig] 已加载 .env: {self.env_file}")

    # ══════════════════════════════════════════════════
    # 密钥访问
    # ══════════════════════════════════════════════════

    def get(self, key_name: str) -> Optional[str]:
        """安全获取密钥值"""
        for kd in self._keys:
            if kd.env_var == key_name:
                return kd.value
        return os.getenv(key_name, "").strip() or None

    def mask(self, key_name: str) -> str:
        """
        脱敏输出

        Example: "abc123...xyz" (仅显示前6后3字符)
        """
        value = self.get(key_name)
        if not value:
            return "<未配置>"
        if len(value) <= 12:
            return "*" * len(value)
        return f"{value[:6]}...{value[-3:]}"

    # ══════════════════════════════════════════════════
    # 验证
    # ══════════════════════════════════════════════════

    def validate_all(self) -> Tuple[bool, List[str]]:
        """
        验证所有密钥配置

        Returns:
            (all_valid, warnings_or_errors)
        """
        issues = []

        for kd in self._keys:
            value = kd.value

            # 必需检查
            if kd.required and not value:
                issues.append(
                    f"[ERROR] 必需密钥未配置: {kd.env_var} ({kd.description})"
                )
                continue

            if not value:
                issues.append(
                    f"[WARN] 可选密钥未配置: {kd.env_var} ({kd.description})"
                )
                continue

            # 长度检查
            if len(value) < kd.min_length:
                issues.append(
                    f"[ERROR] {kd.env_var} 长度不足: {len(value)} < "
                    f"{kd.min_length} ({kd.description})"
                )

            # 格式检查
            if kd.key_type == "api_key" and not re.match(r'^[a-zA-Z0-9_-]+$', value):
                issues.append(
                    f"[WARN] {kd.env_var} 含特殊字符，可能无效 ({kd.description})"
                )

            # 常见占位符检查
            if value.lower() in ("your_key_here", "changeme", "placeholder",
                                  "xxx", "your_api_key"):
                issues.append(
                    f"[ERROR] {kd.env_var} 仍为占位符值 ({kd.description})"
                )

        all_valid = not any(i.startswith("[ERROR]") for i in issues)
        return all_valid, issues

    def get_rotation_status(self) -> Dict[str, Dict]:
        """获取密钥轮换状态"""
        status = {}
        for kd in self._keys:
            if not kd.value:
                status[kd.env_var] = {
                    "configured": False,
                    "rotation_recommended": "N/A",
                }
            else:
                # 简化估算：如果密钥已配置超过 rotation_days，建议轮换
                days_since_load = (datetime.now(timezone.utc) - self._loaded_at).days
                # 实际上我们无法知道密钥何时创建，这里用加载时间近似
                status[kd.env_var] = {
                    "configured": True,
                    "masked": self.mask(kd.env_var),
                    "rotation_interval_days": kd.rotation_days,
                    "note": f"建议每 {kd.rotation_days} 天轮换",
                }
        return status

    # ══════════════════════════════════════════════════
    # 最小权限检查
    # ══════════════════════════════════════════════════

    @staticmethod
    def check_file_permissions(filepath: str) -> Dict:
        """
        检查文件权限是否安全

        Windows: 检查是否可被其它用户写入
        Linux/Mac: 检查 .env 是否是 600 权限
        """
        import stat as _stat

        result = {
            "path": filepath,
            "exists": False,
            "secure": False,
            "issues": [],
        }

        if not os.path.isfile(filepath):
            result["issues"].append("文件不存在")
            return result

        result["exists"] = True

        if os.name == "nt":
            # Windows: 检查是否只读
            if os.access(filepath, os.W_OK):
                result["issues"].append(
                    "[WARN] Windows: .env 文件可写，建议设为只读"
                )
            else:
                result["secure"] = True
        else:
            # Unix: 检查是否只有 owner 可读写
            try:
                mode = os.stat(filepath).st_mode
                if mode & (_stat.S_IROTH | _stat.S_IWOTH | _stat.S_IXOTH):
                    result["issues"].append("文件对其他用户可见")
                elif mode & (_stat.S_IRGRP | _stat.S_IWGRP):
                    result["issues"].append("文件对同组用户可见")
                else:
                    result["secure"] = True
            except (Exception, KeyError, IndexError) as e:
                result["issues"].append(f"权限检查失败: {e}")

        return result

    @staticmethod
    def check_db_permissions(db_path: str) -> Dict:
        """检查数据库文件权限"""
        if not os.path.isfile(db_path):
            return {"path": db_path, "exists": False, "issues": ["数据库文件不存在"]}

        issues = []
        # 检查数据库是否在安全目录
        dangerous_paths = ["/tmp", "/var/tmp", "C:\\Windows\\Temp",
                           os.path.expanduser("~\\AppData\\Local\\Temp")]
        for dp in dangerous_paths:
            if db_path.startswith(dp):
                issues.append(f"数据库位于临时目录: {dp}")

        return {
            "path": db_path,
            "exists": True,
            "size_mb": round(os.path.getsize(db_path) / (1024 * 1024), 2),
            "issues": issues,
            "secure": len(issues) == 0,
        }

    def full_security_audit(self) -> Dict:
        """完整安全审计"""
        valid, issues = self.validate_all()

        return {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "env_file": self.env_file,
            "key_validation": {
                "all_valid": valid,
                "issues": issues,
                "keys_configured": sum(
                    1 for kd in self._keys if kd.value
                ),
                "keys_required": sum(1 for kd in self._keys if kd.required),
            },
            "key_rotation": self.get_rotation_status(),
            "file_permissions": self.check_file_permissions(self.env_file),
            "recommendations": self._generate_recommendations(issues),
        }

    @staticmethod
    def _generate_recommendations(issues: List[str]) -> List[str]:
        """根据问题生成建议"""
        recs = []
        if any("未配置" in i for i in issues):
            recs.append("配置所有必需密钥后重启服务")
        if any("占位符" in i for i in issues):
            recs.append("替换占位符值为真实密钥")
        if any("长度不足" in i for i in issues):
            recs.append("生成足够强度的密钥（建议 openssl rand -hex 32）")
        return recs
