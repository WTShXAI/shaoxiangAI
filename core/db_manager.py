"""SQLite 连接管理器: WAL + 单写者 + 有界连接池 + 坏页自检 (REQ-02, 事故② 根治).

事故背景 (docs/system_design.md §1.1 事故②):
    旧 ``conn()`` **每次连接**都执行 ``PRAGMA journal_mode=WAL``。``journal_mode`` 是
    持久化设置(写在数据库文件头), 设置动作需要**排他写锁** → 全量轮持锁时其他连接
    在 PRAGMA 上 busy 超时 (实测 1244 次 ``database is locked``) → 未捕获路径崩溃 → 坏页。

本模块的四道护栏:
    1. **PRAGMA 仅初始化一次**: ``journal_mode=WAL`` 只在 ``init_db()`` 执行, 且先读
       当前值, 已是 WAL 就**完全不写** → 零写锁争抢。``synchronous`` / ``busy_timeout``
       是**连接级**设置 (不落文件头、不取锁), 每条连接都设, 安全。
    2. **单写者 (Single-Writer)**: 全进程唯一 writer 连接 + ``threading.RLock`` 串行化,
       杜绝并发写抢锁。读走独立 reader 连接 (WAL 支持多读不阻塞写)。
    3. **有界连接池**: reader 连接 LIFO 复用, 上限 ``pool_size``; checkout 时做健康检查,
       坏连接直接丢弃重建。
    4. **坏页自检 + 备份/恢复**: ``check_integrity()`` / ``backup()`` / ``restore()``,
       配合 ``scripts/backup_gq.py`` 形成 SOP (REQ-12)。

线程模型:
    所有连接以 ``check_same_thread=False`` 创建, 跨线程安全由本模块的锁 + 池保证。
    writer 的**所有**使用都必须经 ``writer()`` / ``execute_write()`` (自动加锁)。
"""

from __future__ import annotations

import os
import queue
import shutil
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from core.config import Config, get_config
from core.safe_log import safe_str

__all__ = [
    "DBManagerError",
    "PoolExhaustedError",
    "IntegrityError",
    "GQConnectionManager",
    "get_manager",
    "reset_manager",
]

#: reader 池默认容量。
DEFAULT_POOL_SIZE = 5

#: 从池中 checkout 连接的默认等待秒数 (超时视为池耗尽, 快速失败而非无限阻塞)。
DEFAULT_CHECKOUT_TIMEOUT = 10.0


class DBManagerError(Exception):
    """DB 管理器基础异常。"""


class PoolExhaustedError(DBManagerError):
    """reader 池耗尽 (等待超时)。快速失败, 避免拖住事件循环。"""


class IntegrityError(DBManagerError):
    """完整性校验失败 (坏页)。"""


class GQConnectionManager:
    """events.db 连接管理器 (class diagram: GQConnectionManager)。

    Example:
        >>> mgr = GQConnectionManager(db_path="/tmp/t.db")
        >>> mgr.init_db()
        >>> mgr.execute_write("CREATE TABLE IF NOT EXISTS t(a INTEGER)")
        0
        >>> mgr.execute_write("INSERT INTO t(a) VALUES(?)", (1,))
        1
        >>> mgr.query("SELECT a FROM t")[0]["a"]
        1
        >>> mgr.check_integrity()
        True
        >>> mgr.close_all()
    """

    def __init__(
        self,
        db_path: Optional[str] = None,
        busy_timeout_ms: Optional[int] = None,
        pool_size: int = DEFAULT_POOL_SIZE,
        config: Optional[Config] = None,
        logger: Optional[Any] = None,
        checkout_timeout: float = DEFAULT_CHECKOUT_TIMEOUT,
        row_factory: Any = sqlite3.Row,
    ) -> None:
        """初始化管理器 (**不建立连接**, 首次使用或 ``init_db()`` 时才连)。

        Args:
            db_path: 数据库路径; ``None`` 时取 ``Config.gq_db_path``。
            busy_timeout_ms: sqlite busy_timeout; ``None`` 时取 ``Config.busy_timeout_ms``。
            pool_size: reader 池上限 (>=1)。
            config: 配置对象; ``None`` 取全局单例。
            logger: 具备 ``.info/.warning/.error(msg, **kw)`` 的 logger (可选)。
            checkout_timeout: 池 checkout 等待秒数。
            row_factory: reader 连接的 row_factory, 默认 ``sqlite3.Row``。
        """
        cfg = config if config is not None else get_config()
        self._config: Config = cfg
        self._db_path: str = str(db_path or cfg.gq_db_path)
        self._busy_timeout_ms: int = int(
            busy_timeout_ms if busy_timeout_ms is not None else cfg.busy_timeout_ms
        )
        if self._busy_timeout_ms < 0:
            self._busy_timeout_ms = 0
        self._pool_size: int = max(1, int(pool_size))
        self._checkout_timeout: float = max(0.1, float(checkout_timeout))
        self._row_factory = row_factory
        self._logger = logger

        # 单写者: 唯一 writer 连接 + 可重入锁 (同线程嵌套写事务安全)
        self._writer: Optional[sqlite3.Connection] = None
        self._writer_lock = threading.RLock()
        # 嵌套写事务深度 (thread-local); 只有最外层负责 commit/rollback。
        # 用显式计数而非 RLock._is_owned() 私有 API, 避免解释器实现差异。
        self._writer_depth = threading.local()

        # 有界 reader 池
        self._pool: "queue.LifoQueue[sqlite3.Connection]" = queue.LifoQueue(maxsize=self._pool_size)
        self._pool_sem = threading.BoundedSemaphore(self._pool_size)
        self._pool_lock = threading.Lock()
        self._created_readers = 0
        self._leased_readers = 0

        # init 幂等标记
        self._init_lock = threading.RLock()
        self._initialized = False
        self._journal_mode: str = "unknown"
        self._closed = False

        # 观测计数
        self._stats: Dict[str, int] = {
            "writes": 0,
            "reads": 0,
            "write_errors": 0,
            "read_errors": 0,
            "pool_hits": 0,
            "pool_misses": 0,
            "reconnects": 0,
        }

    # ------------------------------------------------------------------
    # 属性
    # ------------------------------------------------------------------
    @property
    def db_path(self) -> str:
        """数据库文件路径。"""
        return self._db_path

    @property
    def busy_timeout_ms(self) -> int:
        """sqlite busy_timeout (毫秒)。"""
        return self._busy_timeout_ms

    @property
    def journal_mode(self) -> str:
        """最近一次探测到的 journal_mode (``init_db()`` 后有效)。"""
        return self._journal_mode

    @property
    def initialized(self) -> bool:
        """``init_db()`` 是否已成功执行。"""
        return self._initialized

    # ------------------------------------------------------------------
    # 日志
    # ------------------------------------------------------------------
    def _log(self, level: str, msg: str, **fields: Any) -> None:
        """内部日志 (无 logger 时静默, 绝不抛)。"""
        if self._logger is None:
            return
        try:
            method = getattr(self._logger, level, None)
            if callable(method):
                method(msg, **fields)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 连接创建
    # ------------------------------------------------------------------
    def _connect(self, readonly: bool = False) -> sqlite3.Connection:
        """建立一条新连接并施加**连接级** PRAGMA。

        连接级 PRAGMA (``busy_timeout`` / ``synchronous``) 不落文件头、**不取写锁**,
        因此可以安全地对每条连接执行 —— 这与"禁止每次连接设 journal_mode"并不矛盾。

        Args:
            readonly: 是否以只读 URI 模式打开 (用于 integrity 校验等旁路)。

        Returns:
            已配置的连接。

        Raises:
            DBManagerError: 连接失败。
        """
        target_dir = os.path.dirname(os.path.abspath(self._db_path))
        if target_dir:
            try:
                os.makedirs(target_dir, exist_ok=True)
            except Exception as exc:
                raise DBManagerError(
                    f"cannot create db directory {target_dir}: {safe_str(exc, 300)}"
                ) from exc
        timeout_sec = max(self._busy_timeout_ms, 0) / 1000.0
        try:
            if readonly:
                uri = f"file:{Path(self._db_path).as_posix()}?mode=ro"
                conn = sqlite3.connect(uri, timeout=timeout_sec, uri=True, check_same_thread=False)
            else:
                conn = sqlite3.connect(
                    self._db_path, timeout=timeout_sec, check_same_thread=False
                )
        except sqlite3.Error as exc:
            raise DBManagerError(
                f"connect failed for {self._db_path}: {safe_str(exc, 300)}"
            ) from exc

        # 连接级设置: 失败静默 (不能因为调优 PRAGMA 失败而让整条连接不可用)
        for pragma in (
            f"PRAGMA busy_timeout={self._busy_timeout_ms}",
            "PRAGMA synchronous=NORMAL",
        ):
            try:
                conn.execute(pragma)
            except sqlite3.Error:
                pass
        if self._row_factory is not None:
            try:
                conn.row_factory = self._row_factory
            except Exception:
                pass
        return conn

    # ------------------------------------------------------------------
    # init_db (幂等)
    # ------------------------------------------------------------------
    def init_db(self, force: bool = False, verify_integrity: bool = False) -> None:
        """一次性初始化持久化 PRAGMA (幂等)。

        执行内容:
            * 读取当前 ``journal_mode``; **只有不是 WAL 时才写** ``journal_mode=WAL``
              (避免无谓抢写锁 —— 坏页根因);
            * 设置连接级 ``synchronous=NORMAL`` / ``busy_timeout``;
            * 可选启动自检 ``integrity_check``。

        重复调用直接返回 (除 ``force=True``)。任何 PRAGMA 失败仅记日志, 不抛,
        以免基础设施初始化把整个服务拖死。

        Args:
            force: 忽略幂等标记, 强制重跑。
            verify_integrity: 初始化后是否顺带跑一次完整性校验。

        Raises:
            DBManagerError: 连接彻底建立不起来。
            IntegrityError: ``verify_integrity=True`` 且校验失败。
        """
        with self._init_lock:
            if self._initialized and not force:
                return
            conn = self._connect()
            try:
                current = "unknown"
                try:
                    row = conn.execute("PRAGMA journal_mode").fetchone()
                    if row is not None:
                        current = str(row[0]).lower()
                except sqlite3.Error as exc:
                    self._log("warning", "read journal_mode failed", detail=safe_str(exc, 300))
                if current != "wal":
                    try:
                        row = conn.execute("PRAGMA journal_mode=WAL").fetchone()
                        current = str(row[0]).lower() if row is not None else current
                        self._log("info", "journal_mode set to WAL", db_path=self._db_path)
                    except sqlite3.Error as exc:
                        # 常见于并发全量轮持锁; 保持原 journal_mode 继续跑, 不崩
                        self._log(
                            "warning",
                            "set journal_mode=WAL failed (kept current mode)",
                            detail=safe_str(exc, 300),
                            current_mode=current,
                        )
                self._journal_mode = current
                try:
                    conn.commit()
                except sqlite3.Error:
                    pass
            finally:
                self._safe_close(conn)
            self._initialized = True

        if verify_integrity and not self.check_integrity():
            raise IntegrityError(f"integrity_check failed for {self._db_path}")

    def _ensure_initialized(self) -> None:
        """惰性保证 ``init_db()`` 已执行。"""
        if not self._initialized:
            self.init_db()

    # ------------------------------------------------------------------
    # reader 池
    # ------------------------------------------------------------------
    @staticmethod
    def _is_alive(conn: sqlite3.Connection) -> bool:
        """健康检查: 能跑 ``SELECT 1`` 即视为可用。"""
        try:
            conn.execute("SELECT 1").fetchone()
            return True
        except Exception:
            return False

    @staticmethod
    def _safe_close(conn: Optional[sqlite3.Connection]) -> None:
        """关闭连接, 吞异常。"""
        if conn is None:
            return
        try:
            conn.close()
        except Exception:
            pass

    def get_reader(self) -> sqlite3.Connection:
        """从有界池中取一条 reader 连接 (WAL 下读不阻塞写)。

        调用方**必须**在用完后调用 ``release_reader()``; 推荐直接用 ``reader()``
        上下文管理器自动归还。

        Returns:
            可用的 sqlite3 连接。

        Raises:
            DBManagerError: 管理器已关闭 / 连接建立失败。
            PoolExhaustedError: 等待超时 (池耗尽)。
        """
        if self._closed:
            raise DBManagerError("connection manager is closed")
        self._ensure_initialized()
        if not self._pool_sem.acquire(timeout=self._checkout_timeout):
            raise PoolExhaustedError(
                f"reader pool exhausted (size={self._pool_size}, "
                f"waited {self._checkout_timeout}s)"
            )
        conn: Optional[sqlite3.Connection] = None
        try:
            while True:
                try:
                    candidate = self._pool.get_nowait()
                except queue.Empty:
                    break
                if self._is_alive(candidate):
                    conn = candidate
                    with self._pool_lock:
                        self._stats["pool_hits"] += 1
                    break
                self._safe_close(candidate)
                with self._pool_lock:
                    self._created_readers = max(0, self._created_readers - 1)
                    self._stats["reconnects"] += 1
            if conn is None:
                conn = self._connect()
                with self._pool_lock:
                    self._created_readers += 1
                    self._stats["pool_misses"] += 1
            with self._pool_lock:
                self._leased_readers += 1
            return conn
        except Exception:
            self._pool_sem.release()  # 未成功借出必须还回配额, 否则池会逐步"漏干"
            raise

    def release_reader(self, conn: Optional[sqlite3.Connection]) -> None:
        """归还 reader 连接 (坏连接直接丢弃)。绝不抛出。"""
        if conn is None:
            return
        try:
            try:
                conn.rollback()  # 丢弃未提交的读事务, 防止长事务钉住 WAL
            except Exception:
                pass
            if self._closed or not self._is_alive(conn):
                self._safe_close(conn)
                with self._pool_lock:
                    self._created_readers = max(0, self._created_readers - 1)
            else:
                try:
                    self._pool.put_nowait(conn)
                except queue.Full:
                    self._safe_close(conn)
                    with self._pool_lock:
                        self._created_readers = max(0, self._created_readers - 1)
        finally:
            with self._pool_lock:
                self._leased_readers = max(0, self._leased_readers - 1)
            try:
                self._pool_sem.release()
            except ValueError:
                pass  # BoundedSemaphore 过量 release 保护, 不允许抛出

    @contextmanager
    def reader(self) -> Iterator[sqlite3.Connection]:
        """reader 连接上下文管理器 (自动归还)。

        Yields:
            reader 连接。
        """
        conn = self.get_reader()
        try:
            yield conn
        finally:
            self.release_reader(conn)

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[Any]:
        """执行只读查询并返回全部行。

        Args:
            sql: SQL 语句。
            params: 绑定参数。

        Returns:
            行列表 (默认 ``sqlite3.Row``)。
        """
        with self.reader() as conn:
            try:
                rows = conn.execute(sql, tuple(params)).fetchall()
                with self._pool_lock:
                    self._stats["reads"] += 1
                return list(rows)
            except Exception:
                with self._pool_lock:
                    self._stats["read_errors"] += 1
                raise

    # ------------------------------------------------------------------
    # 单写者
    # ------------------------------------------------------------------
    def get_writer(self) -> sqlite3.Connection:
        """获取全进程**唯一** writer 连接 (单写者约束)。

        ⚠ 直接拿到裸连接后请自行用 ``writer()`` 或 ``execute_write()`` 加锁;
        本方法只保证"连接唯一", 串行化由 ``_writer_lock`` 负责。

        Returns:
            唯一 writer 连接。
        """
        if self._closed:
            raise DBManagerError("connection manager is closed")
        self._ensure_initialized()
        with self._writer_lock:
            if self._writer is not None and not self._is_alive(self._writer):
                self._safe_close(self._writer)
                self._writer = None
                with self._pool_lock:
                    self._stats["reconnects"] += 1
            if self._writer is None:
                self._writer = self._connect()
            return self._writer

    @contextmanager
    def writer(self) -> Iterator[sqlite3.Connection]:
        """写事务上下文管理器: 加写锁 → 执行 → commit / 异常 rollback。

        单写者串行化的**唯一正确入口**。嵌套调用安全 (RLock), 内层不重复提交。

        Yields:
            writer 连接。
        """
        self._writer_lock.acquire()
        previous_depth = int(getattr(self._writer_depth, "value", 0) or 0)
        self._writer_depth.value = previous_depth + 1
        is_outermost = previous_depth == 0
        try:
            conn = self.get_writer()
            try:
                yield conn
                if is_outermost:
                    conn.commit()
                with self._pool_lock:
                    self._stats["writes"] += 1
            except Exception:
                with self._pool_lock:
                    self._stats["write_errors"] += 1
                if is_outermost:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                raise
        finally:
            self._writer_depth.value = previous_depth
            self._writer_lock.release()

    def execute_write(self, sql: str, params: Sequence[Any] = ()) -> int:
        """执行单条写语句 (自动加锁 + 提交)。

        Args:
            sql: SQL 语句。
            params: 绑定参数。

        Returns:
            受影响行数 (``cursor.rowcount``, DDL 返回 0)。
        """
        with self.writer() as conn:
            cursor = conn.execute(sql, tuple(params))
            return int(cursor.rowcount if cursor.rowcount is not None and cursor.rowcount > 0 else 0)

    def executemany_write(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> int:
        """批量写 (自动加锁 + 单事务提交)。

        Args:
            sql: SQL 语句。
            seq_of_params: 参数序列。

        Returns:
            受影响行数。
        """
        rows = [tuple(p) for p in seq_of_params]
        with self.writer() as conn:
            cursor = conn.executemany(sql, rows)
            return int(cursor.rowcount if cursor.rowcount is not None and cursor.rowcount > 0 else 0)

    # ------------------------------------------------------------------
    # 坏页自检 / checkpoint
    # ------------------------------------------------------------------
    def check_integrity(self, quick: bool = False, max_errors: int = 10) -> bool:
        """执行完整性校验 (坏页自检)。

        跑 ``PRAGMA integrity_check``(或 ``quick_check``) + ``PRAGMA foreign_key_check``。
        **绝不抛出**: 任何异常都视为"校验不通过"返回 ``False``, 由调用方决定是否恢复。

        Args:
            quick: 用 ``quick_check`` 代替 ``integrity_check`` (大库快得多)。
            max_errors: 最多报告的错误数 (传给 PRAGMA 的参数)。

        Returns:
            健康返回 True, 否则 False。
        """
        pragma = "quick_check" if quick else "integrity_check"
        conn: Optional[sqlite3.Connection] = None
        try:
            conn = self._connect()
            rows = conn.execute(f"PRAGMA {pragma}({int(max_errors)})").fetchall()
            verdicts = [str(r[0]).strip().lower() for r in rows if r is not None and len(r) > 0]
            if not verdicts or verdicts != ["ok"]:
                self._log(
                    "error",
                    "integrity check failed",
                    db_path=self._db_path,
                    pragma=pragma,
                    detail=safe_str(verdicts[:max_errors], 1000),
                )
                return False
            try:
                fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
                if fk_rows:
                    self._log(
                        "error",
                        "foreign_key_check reported violations",
                        db_path=self._db_path,
                        violations=len(fk_rows),
                    )
                    return False
            except sqlite3.Error as exc:
                # foreign_key_check 在部分 schema 上可能不可用, 不作为坏页判据
                self._log("warning", "foreign_key_check skipped", detail=safe_str(exc, 300))
            return True
        except Exception as exc:
            self._log(
                "error",
                "integrity check raised",
                db_path=self._db_path,
                detail=safe_str(exc, 500),
            )
            return False
        finally:
            self._safe_close(conn)

    def checkpoint(self, mode: str = "TRUNCATE") -> bool:
        """执行 WAL checkpoint, 把 ``-wal`` 内容落回主库。

        Args:
            mode: ``PASSIVE`` / ``FULL`` / ``RESTART`` / ``TRUNCATE``。

        Returns:
            是否成功 (失败仅记日志返回 False, 不抛)。
        """
        normalized = str(mode or "TRUNCATE").strip().upper()
        if normalized not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
            normalized = "TRUNCATE"
        try:
            with self.writer() as conn:
                row = conn.execute(f"PRAGMA wal_checkpoint({normalized})").fetchone()
            busy = int(row[0]) if row is not None and len(row) > 0 else 0
            if busy != 0:
                self._log(
                    "warning",
                    "wal_checkpoint busy (some frames not checkpointed)",
                    mode=normalized,
                    db_path=self._db_path,
                )
                return False
            return True
        except Exception as exc:
            self._log(
                "warning",
                "wal_checkpoint failed",
                mode=normalized,
                detail=safe_str(exc, 500),
            )
            return False

    # ------------------------------------------------------------------
    # 备份 / 恢复
    # ------------------------------------------------------------------
    def backup(self, dest: str, verify: bool = True) -> str:
        """在线备份数据库 (sqlite backup API, 不阻塞读)。

        流程: ``wal_checkpoint(TRUNCATE)`` → 备份到同目录临时文件 → 校验临时文件
        完整性 → 原子 ``os.replace`` 到目标路径。中途失败会清理临时文件。

        Args:
            dest: 目标文件路径。
            verify: 是否校验备份文件完整性。

        Returns:
            备份文件的绝对路径。

        Raises:
            DBManagerError: 备份失败。
            IntegrityError: 备份文件校验失败。
        """
        self._ensure_initialized()
        dest_path = Path(dest).expanduser()
        dest_dir = dest_path.parent
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            raise DBManagerError(
                f"cannot create backup directory {dest_dir}: {safe_str(exc, 300)}"
            ) from exc

        self.checkpoint("TRUNCATE")  # 尽力而为: 失败也继续 (backup API 自身会带上 WAL 内容)

        tmp_fd, tmp_name = tempfile.mkstemp(prefix=dest_path.name + ".", suffix=".tmp", dir=str(dest_dir))
        os.close(tmp_fd)
        try:
            os.unlink(tmp_name)  # sqlite backup 需要目标不存在或为空库文件
        except OSError:
            pass

        src: Optional[sqlite3.Connection] = None
        dst: Optional[sqlite3.Connection] = None
        try:
            src = self._connect()
            dst = sqlite3.connect(tmp_name, timeout=max(self._busy_timeout_ms, 0) / 1000.0)
            src.backup(dst)
            try:
                dst.commit()
            except sqlite3.Error:
                pass
        except Exception as exc:
            self._safe_close(dst)
            self._safe_close(src)
            _unlink_quiet(tmp_name)
            raise DBManagerError(f"backup failed: {safe_str(exc, 500)}") from exc
        else:
            self._safe_close(dst)
            self._safe_close(src)

        if verify and not _verify_file_integrity(tmp_name, self._busy_timeout_ms):
            _unlink_quiet(tmp_name)
            raise IntegrityError(f"backup verification failed for {dest_path}")

        try:
            os.replace(tmp_name, str(dest_path))
        except Exception as exc:
            _unlink_quiet(tmp_name)
            raise DBManagerError(
                f"cannot move backup into place ({dest_path}): {safe_str(exc, 300)}"
            ) from exc

        self._log("info", "backup completed", db_path=self._db_path, dest=str(dest_path))
        return str(dest_path.resolve())

    def restore(self, src: str, verify: bool = True) -> None:
        """从备份文件恢复 (坏页自愈, REQ-12)。

        流程: 校验源文件 → 关闭所有连接 → 备份当前坏库为 ``*.corrupt-<ts>`` (尽力而为)
        → 拷贝源文件覆盖主库 → 清理 ``-wal``/``-shm`` 残留 → 重新 ``init_db()``。

        Args:
            src: 备份文件路径。
            verify: 是否先校验源文件完整性。

        Raises:
            DBManagerError: 源文件不存在或拷贝失败。
            IntegrityError: 源文件校验失败。
        """
        src_path = Path(src).expanduser()
        if not src_path.is_file():
            raise DBManagerError(f"restore source not found: {src_path}")
        if verify and not _verify_file_integrity(str(src_path), self._busy_timeout_ms):
            raise IntegrityError(f"restore source failed integrity check: {src_path}")

        self.close_all()

        target = Path(self._db_path)
        if target.is_file():
            quarantine = f"{self._db_path}.corrupt-{int(time.time())}"
            try:
                shutil.move(str(target), quarantine)
                self._log("warning", "quarantined current db before restore", path=quarantine)
            except Exception as exc:
                self._log(
                    "warning",
                    "cannot quarantine current db (will overwrite)",
                    detail=safe_str(exc, 300),
                )

        for sidecar in (f"{self._db_path}-wal", f"{self._db_path}-shm"):
            _unlink_quiet(sidecar)

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src_path), str(target))
        except Exception as exc:
            raise DBManagerError(f"restore copy failed: {safe_str(exc, 500)}") from exc

        self._closed = False
        self._initialized = False
        self.init_db(force=True)
        self._log("info", "restore completed", db_path=self._db_path, source=str(src_path))

    # ------------------------------------------------------------------
    # 生命周期 / 观测
    # ------------------------------------------------------------------
    def close_all(self) -> None:
        """关闭 writer 与池内所有 reader 连接 (幂等, 绝不抛)。"""
        self._closed = True
        with self._writer_lock:
            self._safe_close(self._writer)
            self._writer = None
        while True:
            try:
                conn = self._pool.get_nowait()
            except queue.Empty:
                break
            self._safe_close(conn)
        with self._pool_lock:
            self._created_readers = 0

    def stats(self) -> Dict[str, Any]:
        """返回观测指标快照 (供 REQ-15 面板/health 使用)。"""
        with self._pool_lock:
            snapshot = dict(self._stats)
            snapshot.update(
                {
                    "db_path": self._db_path,
                    "journal_mode": self._journal_mode,
                    "pool_size": self._pool_size,
                    "readers_created": self._created_readers,
                    "readers_leased": self._leased_readers,
                    "writer_open": self._writer is not None,
                    "initialized": self._initialized,
                    "closed": self._closed,
                    "busy_timeout_ms": self._busy_timeout_ms,
                }
            )
        return snapshot

    def __enter__(self) -> "GQConnectionManager":
        """支持 ``with GQConnectionManager(...) as mgr:``。"""
        self.init_db()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """退出时关闭全部连接。"""
        self.close_all()

    def __repr__(self) -> str:
        return (
            f"GQConnectionManager(db_path={self._db_path!r}, "
            f"pool_size={self._pool_size}, journal_mode={self._journal_mode!r})"
        )


# ---------------------------------------------------------------------------
# 模块级工具
# ---------------------------------------------------------------------------


def _unlink_quiet(path: str) -> None:
    """删除文件, 吞异常。"""
    try:
        if os.path.exists(path):
            os.unlink(path)
    except Exception:
        pass


def _verify_file_integrity(path: str, busy_timeout_ms: int = 30000) -> bool:
    """独立校验某个 sqlite 文件的完整性 (用于备份/恢复前后)。

    Args:
        path: sqlite 文件路径。
        busy_timeout_ms: busy_timeout。

    Returns:
        健康返回 True, 否则 False (绝不抛)。
    """
    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(path, timeout=max(busy_timeout_ms, 0) / 1000.0)
        rows = conn.execute("PRAGMA integrity_check(10)").fetchall()
        verdicts = [str(r[0]).strip().lower() for r in rows if r is not None and len(r) > 0]
        return verdicts == ["ok"]
    except Exception:
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


_managers: Dict[str, GQConnectionManager] = {}
_managers_lock = threading.Lock()


def get_manager(db_path: Optional[str] = None, **kwargs: Any) -> GQConnectionManager:
    """按路径获取共享的 ``GQConnectionManager`` 单例 (保证"全进程单写者")。

    Args:
        db_path: 数据库路径; ``None`` 时取 ``Config.gq_db_path``。
        **kwargs: 首次创建时传给构造函数的参数。

    Returns:
        共享管理器实例。
    """
    path = str(db_path or get_config().gq_db_path)
    key = os.path.abspath(path)
    with _managers_lock:
        manager = _managers.get(key)
        if manager is None:
            manager = GQConnectionManager(db_path=path, **kwargs)
            _managers[key] = manager
        return manager


def reset_manager(db_path: Optional[str] = None) -> None:
    """关闭并移除缓存的管理器 (测试用; ``db_path=None`` 表示全部)。"""
    with _managers_lock:
        if db_path is None:
            targets = list(_managers.items())
            _managers.clear()
        else:
            key = os.path.abspath(str(db_path))
            manager = _managers.pop(key, None)
            targets = [(key, manager)] if manager is not None else []
    for _key, manager in targets:
        if manager is not None:
            manager.close_all()
