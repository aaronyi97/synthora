"""
CritiqueLogger — content_critique 错题本持久化服务 (v1.0)

订阅 QueryCompleted 事件，将 question_critique_summary 写入
SQLite 数据库（data/analytics.db），实现跨查询错误模式积累。

设计约束：
  - SQLite WAL 模式，asyncio.Queue + 单写协程，避免锁竞争
  - 问题原文 SHA-256 哈希化，不存明文（隐私保护）
  - 只存有实质内容的 critique（has_issues=True 或 summary 非空）
  - MVP：1张表，后续按需扩展
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agoracle.domain.events import QueryCompleted

logger = logging.getLogger(__name__)

_DB_PATH = Path(os.getenv("ANALYTICS_DB_PATH", "data/analytics.db"))

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS critique_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    question_hash TEXT NOT NULL,
    critique_summary TEXT,
    has_issues INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_critique_mode ON critique_findings(mode);
CREATE INDEX IF NOT EXISTS idx_critique_ts ON critique_findings(created_at);
"""

_CREATE_FAILURES_SQL = """
CREATE TABLE IF NOT EXISTS model_call_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id TEXT,
    model_id TEXT NOT NULL,
    role TEXT NOT NULL,
    error_type TEXT NOT NULL,
    error_message TEXT,
    latency_ms INTEGER,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_failure_model ON model_call_failures(model_id);
CREATE INDEX IF NOT EXISTS idx_failure_ts ON model_call_failures(created_at);
"""


def _init_db(db_path: Path) -> sqlite3.Connection:
    """初始化数据库，创建表（如不存在）。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    for stmt in _CREATE_TABLE_SQL.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    for stmt in _CREATE_FAILURES_SQL.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    conn.commit()
    return conn


def _hash_question(question: str) -> str:
    return hashlib.sha256(question.encode()).hexdigest()[:16]


class CritiqueLogger:
    """
    EventBus 订阅者：QueryCompleted → 错题本落盘。

    Usage:
        logger_svc = CritiqueLogger()
        await logger_svc.start()
        event_bus.subscribe(QueryCompleted, logger_svc.on_query_completed)
    """

    def __init__(self, db_path: Path = _DB_PATH) -> None:
        self._db_path = db_path
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._conn: sqlite3.Connection | None = None
        self._writer_task: asyncio.Task | None = None

    async def start(self) -> None:
        """启动后台写入协程（在 app lifespan 中调用）。"""
        self._conn = await asyncio.to_thread(_init_db, self._db_path)
        self._writer_task = asyncio.create_task(self._writer_loop())
        logger.info(f"[CritiqueLogger] 已启动，数据库: {self._db_path}")

    async def stop(self) -> None:
        """优雅关闭（在 app shutdown 中调用）。"""
        if self._writer_task:
            self._writer_task.cancel()
        if self._conn:
            self._conn.close()

    async def on_query_completed(self, event: "QueryCompleted") -> None:
        """EventBus handler — 将有效 critique 写入队列。"""
        summary = event.question_critique_summary
        if not summary:
            return
        try:
            self._queue.put_nowait({
                "type": "critique",
                "query_id": event.query_id,
                "mode": event.mode,
                "question_hash": _hash_question(event.question),
                "critique_summary": summary[:2000],
                "has_issues": 1,
                "created_at": datetime.now().isoformat(),
            })
        except asyncio.QueueFull:
            logger.warning("[CritiqueLogger] 队列满，丢弃一条 critique 记录")

    async def log_failure(
        self,
        query_id: str,
        model_id: str,
        role: str,
        error_type: str,
        error_message: str,
        latency_ms: int,
    ) -> None:
        """记录模型调用失败（由 FailureMonitor 调用）。"""
        try:
            self._queue.put_nowait({
                "type": "failure",
                "query_id": query_id,
                "model_id": model_id,
                "role": role,
                "error_type": error_type,
                "error_message": error_message[:500],
                "latency_ms": latency_ms,
                "created_at": datetime.now().isoformat(),
            })
        except asyncio.QueueFull:
            logger.warning("[CritiqueLogger] 队列满，丢弃一条 failure 记录")

    async def _writer_loop(self) -> None:
        """单写协程：从队列取记录批量写入 SQLite。"""
        while True:
            try:
                record = await self._queue.get()
                batch = [record]
                while not self._queue.empty() and len(batch) < 20:
                    try:
                        batch.append(self._queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                await asyncio.to_thread(self._write_batch, batch)
                for _ in batch:
                    self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[CritiqueLogger] 写入失败: {e}", exc_info=True)

    def _write_batch(self, batch: list[dict]) -> None:
        """同步写入（在线程池中执行）。"""
        if not self._conn:
            return
        try:
            with self._conn:
                for r in batch:
                    if r["type"] == "critique":
                        self._conn.execute(
                            "INSERT INTO critique_findings "
                            "(query_id, mode, question_hash, critique_summary, has_issues, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (r["query_id"], r["mode"], r["question_hash"],
                             r["critique_summary"], r["has_issues"], r["created_at"]),
                        )
                    elif r["type"] == "failure":
                        self._conn.execute(
                            "INSERT INTO model_call_failures "
                            "(query_id, model_id, role, error_type, error_message, latency_ms, created_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (r["query_id"], r["model_id"], r["role"],
                             r["error_type"], r["error_message"],
                             r["latency_ms"], r["created_at"]),
                        )
        except Exception as e:
            logger.error(f"[CritiqueLogger] SQLite 写入异常: {e}", exc_info=True)
