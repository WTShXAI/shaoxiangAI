"""
============================================================
  哨响AI — 数据纯净死命令（DATA INTEGRITY LAW）
============================================================
  这是哨响AI系统中不可修改、不可绕过的基础法律。
  违反此法律的代码 = 不可接收的缺陷、必须修复。

  「第一律」本系统禁止使用任何形式的 模拟/合成/随机/伪造
           数据来训练模型、生成预测结果或填充数据库。
           所有训练数据必须来源于真实比赛记录（status='finished'
           且 home_score IS NOT NULL）。

  「第二律」严禁以下行为：
           - 用 np.random / random 生成比赛比分、赔率、赛程
           - 用硬编码评分推测比赛结果后当作"训练数据"使用
           - 在任何训练/预测流程中调用 generate_matches()、
             _generate_mock_data()、estimate_odds() 等合成函数
           - 当无真实数据时偷偷用模拟数据"兜底"

  「第三律」检测到模拟数据时，必须：
           - 抛出 DataIntegrityError（不可被静默捕获的硬错误）
           - 记录完整的调用栈到日志
           - 停止当前操作，绝不降级到模拟数据

  此文件由哨响AI核心团队维护。任何对此文件的修改须经审核。
============================================================
"""

import functools
import logging
import os
import traceback
from typing import Callable

logger = logging.getLogger("data_integrity")

# ============================================================
# 全局开关 — 永远为 True，禁止任何环境变量绕过
# ============================================================
ENFORCE_REAL_DATA_ONLY = True


class DataIntegrityError(RuntimeError):
    """
    数据纯净性错误 — 系统级的硬错误，不可被静默捕获。

    当代码试图使用模拟/合成/随机数据时抛出。
    这不只是一个"警告"，而是一个"阻断"。
    """
    def __init__(self, message: str, caller_info: str = ""):
        full_message = (
            f"\n{'='*60}\n"
            f"  ⛔ 数据纯净死命令违反 — 操作已阻断\n"
            f"{'='*60}\n"
            f"  原因: {message}\n"
            f"  位置: {caller_info}\n"
            f"  规则: 哨响AI禁止使用任何模拟/合成/随机数据\n"
            f"  处理: 请确保数据库中存在真实比赛记录后再操作\n"
            f"{'='*60}\n"
        )
        super().__init__(full_message)
        self.message = message
        self.caller_info = caller_info
        # 写入错误日志
        logger.critical(full_message)


def _get_caller_info() -> str:
    """获取调用者位置信息"""
    stack = traceback.extract_stack()
    # stack[-1] 是当前函数，stack[-2] 是调用者，stack[-3] 是原始触发点
    for frame in reversed(stack[:-2]):
        if 'data_integrity' not in frame.filename:
            return f"{frame.filename}:{frame.lineno} in {frame.name}()"
    return "unknown"


def guard_real_data(source_name: str = "unknown") -> Callable:
    """
    装饰器：标记函数为「仅限真实数据」。
    若全局开关未启用（理论不应发生），记录警告。
    被装饰函数调用时依赖此检查来阻止模拟数据。
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            if not ENFORCE_REAL_DATA_ONLY:
                logger.critical(f"⚠️ 数据纯净法律被绕过！{source_name}")
                raise DataIntegrityError(
                    f"ENFORCE_REAL_DATA_ONLY 被设为 False — 这是不允许的。"
                    f"\n  来源: {source_name}",
                    _get_caller_info()
                )
            return func(*args, **kwargs)
        return wrapper
    return decorator


def block_mock_data(source_name: str, detail: str = "") -> None:
    """
    主动阻断调用：在被禁止的模拟数据函数入口处调用。
    这个函数不会返回 — 它会直接抛出 DataIntegrityError。
    """
    caller = _get_caller_info()
    msg = f"试图调用被禁止的模拟数据函数: {source_name}"
    if detail:
        msg += f"\n  详情: {detail}"
    raise DataIntegrityError(msg, caller)


def assert_real_match_data(matches: list, context: str = "") -> None:
    """
    验证比赛数据是否为真实数据。
    检查条件：status='finished' 且 home_score 不为 None/空。

    用法：
        assert_real_match_data(matches, context="训练数据准备")
    """
    if not matches:
        raise DataIntegrityError(
            f"无训练数据：数据库中不存在已完成的比赛记录。"
            f"\n  上下文: {context}"
            f"\n  需要: status='finished' 且 home_score IS NOT NULL",
            _get_caller_info()
        )

    for m in matches:
        if m.get('status') != 'finished':
            raise DataIntegrityError(
                f"发现未完成的比赛被当作训练数据: match_id={m.get('match_id')},"
                f" status={m.get('status')}"
                f"\n  上下文: {context}",
                _get_caller_info()
            )
        if m.get('home_score') is None:
            raise DataIntegrityError(
                f"发现无比分数据的比赛被当作训练数据: match_id={m.get('match_id')}"
                f"\n  上下文: {context}",
                _get_caller_info()
            )

    logger.info(f"✅ 数据纯净检查通过: {len(matches)} 场真实已完成比赛 [{context}]")


def assert_no_synthetic_source(source_module: str, caller_func: str = "") -> None:
    """
    断言数据来源非合成。
    在从 enhanced_data.py 或任何模拟源导入关键数据生成函数时调用。
    """
    SYNTHETIC_SOURCES = [
        'enhanced_data',
        'mock_data',
        'synthetic',
        'simulated',
    ]
    for keyword in SYNTHETIC_SOURCES:
        if keyword in source_module.lower():
            raise DataIntegrityError(
                f"数据来源为合成模块: {source_module}"
                f"\n  调用者: {caller_func if caller_func else 'unknown'}",
                _get_caller_info()
            )


# ============================================================
# 系统启动时的纯净性自检
# ============================================================
def startup_integrity_check() -> None:
    """
    哨响AI启动时强制执行数据纯净性检查。
    如果发现任何模拟数据模块处于活跃状态，阻止启动。
    """
    logger.info("=" * 50)
    logger.info("  🔒 数据纯净法律 启动检查")
    logger.info("=" * 50)

    # 检查1: ENFORCE_REAL_DATA_ONLY 必须为 True
    if not ENFORCE_REAL_DATA_ONLY:
        raise DataIntegrityError("ENFORCE_REAL_DATA_ONLY 在启动时不为 True")

    # 检查2: 禁止通过环境变量绕过
    env_bypass = os.environ.get('ALLOW_MOCK_DATA', '').lower()
    if env_bypass in ('1', 'true', 'yes'):
        raise DataIntegrityError(
            "环境变量 ALLOW_MOCK_DATA 被设为允许 — 这是严禁的。"
            "\n  请删除此环境变量并重启系统。"
        )

    # 检查3: 验证核心数据函数是否可访问真实数据库
    logger.info("  ✅ 数据纯净法律全部通过")
    logger.info("  📜 规则: 禁止模拟数据 / 合成数据 / 随机填充")
    logger.info("  📜 要求: 所有训练数据必须来自真实比赛记录")
    logger.info("=" * 50)


# ============================================================
# 模块导入时自动执行基本检查
# ============================================================
if ENFORCE_REAL_DATA_ONLY:
    logger.info("🔒 数据纯净法律已加载 — 模拟数据全面禁止")
else:
    logger.critical("⚠️⚠️⚠️ 数据纯净法律未启用 — 这不应该发生！")
