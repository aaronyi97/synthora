"""
Usage Quota Service (v2.7.5 → v4.10 → Q3-rootfix) — per-user lifetime credit tracking.

v4.10: Changed from daily-reset per-mode limits to a single lifetime credit pool.
Each user starts with 500 credits (never resets). Credit costs per query:
  light=1, deep=60, research=100, socratic=15

Q3-rootfix: Migrated persistence from JSON file + threading.Lock to SQLite WAL.
SQLite BEGIN IMMEDIATE transactions provide cross-process atomicity, making this
safe under gunicorn workers>1 without per-process state divergence.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Generator

if TYPE_CHECKING:
    from agoracle.config.schema import QuotaConfig

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    user_id  TEXT NOT NULL,
    date     TEXT NOT NULL,
    mode     TEXT NOT NULL,
    count    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, date, mode)
);
CREATE TABLE IF NOT EXISTS credits (
    user_id TEXT PRIMARY KEY,
    total   INTEGER NOT NULL DEFAULT 500
);
"""

_CREDIT_COST: dict[str, int] = {"light": 1, "deep": 60, "research": 100, "socratic": 15, "roundtable": 60}


class QuotaService:
    """Per-user lifetime quota tracking backed by SQLite WAL (multi-worker safe)."""

    def __init__(self, quota_config: "QuotaConfig", data_dir: str = "data") -> None:
        self._config = quota_config
        db_path = Path(data_dir) / "quota.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db_path = str(db_path)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=8000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _tx(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager: BEGIN IMMEDIATE transaction, auto-commit or rollback."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _user_key(self, user_id: int) -> str:
        return str(user_id)

    def get_usage(self, user_id: int) -> dict[str, int]:
        """Get today's usage counts for a user. Returns {mode: count}."""
        today = self._today()
        uk = self._user_key(user_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT mode, count FROM usage WHERE user_id=? AND date=?",
                (uk, today),
            ).fetchall()
        day_data = {row[0]: row[1] for row in rows}
        return {
            "light": day_data.get("light", 0),
            "deep": day_data.get("deep", 0),
            "research": day_data.get("research", 0),
            "socratic": day_data.get("socratic", 0),
            "roundtable": day_data.get("roundtable", 0),
        }

    def get_lifetime_usage(self, user_id: int) -> dict[str, int]:
        """Get all-time usage counts for a user across all dates. Returns {mode: count}."""
        uk = self._user_key(user_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT mode, SUM(count) FROM usage WHERE user_id=? GROUP BY mode",
                (uk,),
            ).fetchall()
        totals: dict[str, int] = {"light": 0, "deep": 0, "research": 0, "socratic": 0, "roundtable": 0}
        for mode, cnt in rows:
            if mode in totals:
                totals[mode] = cnt or 0
        return totals

    def get_lifetime_credits_used(self, user_id: int) -> int:
        """Return total credits consumed by a user across all time."""
        usage = self.get_lifetime_usage(user_id)
        return sum(usage.get(m, 0) * _CREDIT_COST.get(m, 0) for m in _CREDIT_COST)

    def get_user_total_credits(self, user_id: int) -> int:
        """Return the total credit allocation for a user (default 500 if not set)."""
        uk = self._user_key(user_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT total FROM credits WHERE user_id=?", (uk,)
            ).fetchone()
        return row[0] if row else 500

    def set_user_total_credits(self, user_id: int, total: int) -> None:
        """Set a custom total credit allocation for a user."""
        uk = self._user_key(user_id)
        with self._tx() as conn:
            conn.execute(
                "INSERT INTO credits (user_id, total) VALUES (?, ?)"
                " ON CONFLICT(user_id) DO UPDATE SET total=excluded.total",
                (uk, total),
            )

    def get_limits(self) -> dict[str, int]:
        """Get configured daily limits."""
        return {
            "light": self._config.light,
            "deep": self._config.deep,
            "research": self._config.research,
            "socratic": self._config.socratic,
            "roundtable": self._config.roundtable,
        }

    def check_quota(self, user_id: int, mode: str) -> dict | None:
        """
        Check if user has remaining lifetime credits for the given mode.

        v4.10: Enforces a single lifetime credit pool (500 credits, never resets).
        Returns None if quota is OK.
        Returns error dict if credits would be exhausted.
        """
        if not self._config.enabled:
            return None

        # Admin (user_id=0 or unauth) — no quota enforcement
        if user_id <= 0:
            return None

        mode_lower = mode.lower()
        cost = _CREDIT_COST.get(mode_lower, 0)
        total_credits = self.get_user_total_credits(user_id)
        credits_used = self.get_lifetime_credits_used(user_id)
        credits_remaining = max(0, total_credits - credits_used)

        if credits_remaining < cost:
            return {
                "error": "quota_exceeded",
                "mode": mode_lower,
                "total_credits": total_credits,
                "credits_used": credits_used,
                "credits_remaining": credits_remaining,
                "cost": cost,
                "message": (
                    f"你的积分余额不足（剩余 {credits_remaining} 积分，"
                    f"{mode_lower.capitalize()} 模式需要 {cost} 积分）。"
                    f"试用积分已用完，感谢体验 Synthora。"
                ),
            }

        return None

    def record_usage(self, user_id: int, mode: str) -> None:
        """Record one usage for the given user and mode.

        SQLite BEGIN IMMEDIATE ensures cross-process atomic increment.
        Async dispatch via run_in_executor to avoid blocking the event loop.
        """
        if user_id <= 0:
            return

        def _write() -> None:
            today = self._today()
            uk = self._user_key(user_id)
            mode_lower = mode.lower()
            with self._tx() as conn:
                conn.execute(
                    "INSERT INTO usage (user_id, date, mode, count) VALUES (?, ?, ?, 1)"
                    " ON CONFLICT(user_id, date, mode) DO UPDATE SET count=count+1",
                    (uk, today, mode_lower),
                )

        def _safe_write() -> None:
            try:
                _write()
            except Exception as e:
                logger.error(f"Failed to record usage for user_id={user_id}, mode={mode}: {e}")

        try:
            loop = asyncio.get_running_loop()
            loop.run_in_executor(None, _safe_write)
        except RuntimeError:
            _safe_write()

    def _cleanup_old_records(self) -> None:
        """Remove usage records older than 30 days (maintenance, not on hot path)."""
        cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        with self._tx() as conn:
            conn.execute("DELETE FROM usage WHERE date < ?", (cutoff,))

    def get_all_usage(self, date: str | None = None) -> dict:
        """
        Get usage for all users for a given date (default: today).
        Used by admin endpoint.
        """
        target_date = date or self._today()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT user_id, mode, count FROM usage WHERE date=?",
                (target_date,),
            ).fetchall()
        result: dict[str, dict] = {}
        for user_id, mode, count in rows:
            if user_id not in result:
                result[user_id] = {}
            result[user_id][mode] = count
        for uid in result:
            result[uid]["total"] = sum(
                v for k, v in result[uid].items() if k != "total"
            )
        return result

    def get_user_history(self, user_id: int, days: int = 7) -> dict:
        """Get usage history for a specific user over the last N days."""
        uk = self._user_key(user_id)
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT date, mode, count FROM usage WHERE user_id=?"
                " ORDER BY date DESC",
                (uk,),
            ).fetchall()
        history: dict[str, dict] = {}
        for date, mode, count in rows:
            if len(history) >= days and date not in history:
                continue
            if date not in history:
                history[date] = {}
            history[date][mode] = count
        return dict(list(history.items())[:days])

    def delete_user(self, user_id: int) -> None:
        """Remove all quota data for a user (FM-04 账户删除)."""
        uk = self._user_key(user_id)
        with self._tx() as conn:
            conn.execute("DELETE FROM usage WHERE user_id=?", (uk,))
            conn.execute("DELETE FROM credits WHERE user_id=?", (uk,))
