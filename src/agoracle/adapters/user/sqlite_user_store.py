"""
SQLite User Store — lightweight user management for small-scale deployment.

Features:
  - Username + bcrypt password hash
  - Unique API key per user (used as Bearer token)
  - Admin flag for future RBAC
  - Query history tracking per user
  - Async-safe via aiosqlite
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from datetime import datetime

import bcrypt
from pathlib import Path
from typing import Optional

import aiosqlite

logger = logging.getLogger(__name__)

_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT '',
    api_key TEXT UNIQUE NOT NULL,
    api_key_hash TEXT UNIQUE,
    is_admin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL,
    phone TEXT UNIQUE,
    phone_verified INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS verification_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    phone TEXT NOT NULL,
    code TEXT NOT NULL,
    purpose TEXT NOT NULL DEFAULT 'register',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_vc_phone ON verification_codes(phone);
CREATE INDEX IF NOT EXISTS idx_vc_expires ON verification_codes(expires_at);

CREATE INDEX IF NOT EXISTS idx_users_api_key ON users(api_key);
CREATE INDEX IF NOT EXISTS idx_users_api_key_hash ON users(api_key_hash);
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);

CREATE TABLE IF NOT EXISTS query_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    query_id TEXT NOT NULL,
    session_id TEXT,
    question TEXT NOT NULL,
    mode TEXT NOT NULL DEFAULT '',
    final_answer TEXT NOT NULL DEFAULT '',
    confidence REAL NOT NULL DEFAULT 0,
    contributor_count INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd REAL NOT NULL DEFAULT 0,
    user_marked_usable INTEGER,
    created_at TEXT NOT NULL,
    quality_gate TEXT NOT NULL DEFAULT '',
    best_single_answer TEXT NOT NULL DEFAULT '',
    has_divergence INTEGER NOT NULL DEFAULT 0,
    divergence_summary TEXT NOT NULL DEFAULT '',
    key_insights TEXT NOT NULL DEFAULT '[]',
    divergence_points TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_history_user_id ON query_history(user_id);
CREATE INDEX IF NOT EXISTS idx_history_created_at ON query_history(created_at);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    last_active_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_expires_at ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS login_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    client_ip TEXT NOT NULL,
    failed_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_login_failures_username ON login_failures(username);
CREATE INDEX IF NOT EXISTS idx_login_failures_ip ON login_failures(client_ip);
"""


def _hash_password(password: str) -> str:
    """Hash password with bcrypt (cost factor 12)."""
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def _verify_password(password: str, stored_hash: str) -> bool:
    """Verify password. Supports bcrypt and legacy SHA-256 for migration."""
    if stored_hash.startswith("$2b$") or stored_hash.startswith("$2a$"):
        # bcrypt hash
        return bcrypt.checkpw(password.encode(), stored_hash.encode())
    # Legacy SHA-256:salt format — verify then caller should re-hash
    parts = stored_hash.split(":", 1)
    if len(parts) != 2:
        return False
    salt, expected = parts
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return hmac.compare_digest(h, expected)


def _needs_rehash(stored_hash: str) -> bool:
    """Check if stored hash uses legacy algorithm and needs upgrade."""
    return not (stored_hash.startswith("$2b$") or stored_hash.startswith("$2a$"))


# Session TTL constants
_SESSION_ABSOLUTE_TTL = 7 * 24 * 3600   # 7 days hard limit
_SESSION_IDLE_TTL = 24 * 3600           # 24h idle timeout
_LOGIN_FAIL_LIMIT = 5
_LOGIN_LOCKOUT_SECONDS = 900            # 15 minutes

# Server-side HMAC secret for api_key hashing.
# Only development/test environments may use the deterministic fallback.
# All other environments (production, staging, etc.) MUST set API_KEY_HMAC_SECRET.
_API_KEY_HMAC_SECRET_RAW = os.environ.get("API_KEY_HMAC_SECRET", "")
_ENV = os.environ.get("ENV", "development").lower()
_ALLOW_FALLBACK = _ENV in ("development", "test")

if not _ALLOW_FALLBACK:
    if not _API_KEY_HMAC_SECRET_RAW:
        raise RuntimeError(
            f"FATAL: API_KEY_HMAC_SECRET must be set in {_ENV} environment. "
            "Generate with: python3 -c \"import secrets; print(secrets.token_hex(32))\""
        )
    if len(_API_KEY_HMAC_SECRET_RAW) < 32:
        raise RuntimeError(
            f"FATAL: API_KEY_HMAC_SECRET too short in {_ENV} environment (minimum 32 characters). "
            "Generate with: python3 -c \"import secrets; print(secrets.token_hex(32))\""
        )
elif not _API_KEY_HMAC_SECRET_RAW:
    logger.warning("API_KEY_HMAC_SECRET not set — using insecure dev fallback. Never use in production/staging.")

_API_KEY_HMAC_SECRET = (_API_KEY_HMAC_SECRET_RAW or "synthora-api-key-hmac-dev-only").encode()


def _hash_api_key(api_key: str) -> str:
    """HMAC-SHA256 hash of api_key for storage. Deterministic for lookup."""
    return hmac.new(_API_KEY_HMAC_SECRET, api_key.encode(), hashlib.sha256).hexdigest()


def _generate_api_key() -> str:
    """Generate a unique API key."""
    return f"sk-{secrets.token_hex(24)}"


def _generate_session_id() -> str:
    """Generate a cryptographically secure session ID (opaque, not equal to api_key)."""
    return f"sess-{secrets.token_hex(32)}"


class SQLiteUserStore:
    """SQLite-backed user store with query history."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._db_path))
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        # v2.9: busy_timeout — wait up to 5s for write locks before raising SQLITE_BUSY.
        # Prevents spurious write failures when multiple gunicorn workers commit concurrently.
        await self._db.execute("PRAGMA busy_timeout=5000")

        # Migration: add api_key_hash column BEFORE _CREATE_TABLES runs,
        # because _CREATE_TABLES includes CREATE INDEX on api_key_hash.
        # On existing DBs the column is missing → index creation fails.
        # NOTE: SQLite ALTER TABLE ADD COLUMN cannot have UNIQUE constraint,
        # so we add the column plain and rely on the index for uniqueness.
        try:
            await self._db.execute("ALTER TABLE users ADD COLUMN api_key_hash TEXT")
            await self._db.commit()
            logger.info("Migration: added api_key_hash column to users table")
        except Exception as e:
            err = str(e).lower()
            if "duplicate column" not in err and "already exists" not in err and "no such table" not in err:
                logger.error(f"Migration FAILED (api_key_hash): {e}")
                raise

        # Backfill: hash existing plaintext api_keys for rows missing api_key_hash.
        try:
            cursor = await self._db.execute(
                "SELECT id, api_key FROM users WHERE api_key_hash IS NULL AND api_key IS NOT NULL AND api_key != ''"
            )
            legacy_rows = await cursor.fetchall()
            if legacy_rows:
                for row_id, plain_key in legacy_rows:
                    hashed = _hash_api_key(plain_key)
                    await self._db.execute(
                        "UPDATE users SET api_key_hash = ?, api_key = ? WHERE id = ?",
                        (hashed, hashed, row_id),
                    )
                await self._db.commit()
                logger.info(f"Migration: backfilled api_key_hash for {len(legacy_rows)} legacy rows")
        except Exception as e:
            err = str(e).lower()
            if "no such table" not in err:
                logger.error(f"Migration FAILED (api_key_hash backfill): {e}")
                raise

        try:
            await self._db.execute("ALTER TABLE query_history ADD COLUMN user_marked_usable INTEGER")
            await self._db.commit()
            logger.info("Migration: added user_marked_usable column to query_history")
        except Exception as e:
            err = str(e).lower()
            if "duplicate column" not in err and "already exists" not in err and "no such table" not in err:
                logger.error(f"Migration FAILED (user_marked_usable): {e}")
                raise

        for col_sql, col_name in [
            ("ALTER TABLE query_history ADD COLUMN quality_gate TEXT NOT NULL DEFAULT ''", "quality_gate"),
            ("ALTER TABLE query_history ADD COLUMN best_single_answer TEXT NOT NULL DEFAULT ''", "best_single_answer"),
        ]:
            try:
                await self._db.execute(col_sql)
                await self._db.commit()
                logger.info(f"Migration: added {col_name} column to query_history")
            except Exception as e:
                err = str(e).lower()
                if "duplicate column" not in err and "already exists" not in err and "no such table" not in err:
                    logger.error(f"Migration FAILED ({col_name}): {e}")
                    raise

        for col_sql, col_name in [
            ("ALTER TABLE users ADD COLUMN phone TEXT", "phone"),
            ("ALTER TABLE users ADD COLUMN phone_verified INTEGER NOT NULL DEFAULT 0", "phone_verified"),
        ]:
            try:
                await self._db.execute(col_sql)
                await self._db.commit()
                logger.info(f"Migration: added {col_name} column to users table")
            except Exception as e:
                err = str(e).lower()
                if "duplicate column" not in err and "already exists" not in err and "no such table" not in err:
                    logger.error(f"Migration FAILED ({col_name}): {e}")
                    raise

        for col_sql, col_name in [
            ("ALTER TABLE query_history ADD COLUMN has_divergence INTEGER NOT NULL DEFAULT 0", "has_divergence"),
            ("ALTER TABLE query_history ADD COLUMN divergence_summary TEXT NOT NULL DEFAULT ''", "divergence_summary"),
            ("ALTER TABLE query_history ADD COLUMN key_insights TEXT NOT NULL DEFAULT '[]'", "key_insights"),
            ("ALTER TABLE query_history ADD COLUMN divergence_points TEXT NOT NULL DEFAULT '[]'", "divergence_points"),
            ("ALTER TABLE query_history ADD COLUMN session_id TEXT", "session_id"),
        ]:
            try:
                await self._db.execute(col_sql)
                await self._db.commit()
                logger.info(f"Migration: added {col_name} column to query_history")
            except Exception as e:
                err = str(e).lower()
                if "duplicate column" not in err and "already exists" not in err and "no such table" not in err:
                    logger.error(f"Migration FAILED ({col_name}): {e}")
                    raise

        await self._db.executescript(_CREATE_TABLES)
        await self._db.commit()
        logger.info(f"UserStore initialized: {self._db_path}")

    def _ensure_db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("UserStore not initialized")
        return self._db

    # ── Session Management (SEC-003) ───────────────────────

    async def create_session(self, user_id: int) -> str:
        """Create a new session for the given user. Returns opaque session_id."""
        db = self._ensure_db()
        session_id = _generate_session_id()
        now = time.time()
        expires_at = now + _SESSION_ABSOLUTE_TTL
        await db.execute(
            "INSERT INTO sessions (session_id, user_id, created_at, last_active_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, user_id, now, now, expires_at),
        )
        await db.commit()
        # Opportunistic cleanup of expired sessions (1% chance per request)
        if secrets.randbelow(100) == 0:
            await self._cleanup_sessions(db, now)
        return session_id

    async def get_by_session_id(self, session_id: str) -> Optional[dict]:
        """Look up user by session_id. Returns None if expired or not found."""
        db = self._ensure_db()
        now = time.time()
        cursor = await db.execute(
            """SELECT s.user_id, s.last_active_at, s.expires_at,
                      u.username, u.display_name, u.is_admin
               FROM sessions s JOIN users u ON s.user_id = u.id
               WHERE s.session_id = ?""",
            (session_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return None

        user_id, last_active, expires_at, username, display_name, is_admin = row

        # Check hard expiry
        if now > expires_at:
            await db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            await db.commit()
            return None

        # Check idle timeout
        if now - last_active > _SESSION_IDLE_TTL:
            await db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
            await db.commit()
            return None

        # Throttle last_active updates to once per 5 minutes
        if now - last_active > 300:
            await db.execute(
                "UPDATE sessions SET last_active_at = ? WHERE session_id = ?",
                (now, session_id),
            )
            await db.commit()

        return {
            "id": user_id,
            "username": username,
            "display_name": display_name,
            "is_admin": bool(is_admin),
        }

    async def revoke_session(self, session_id: str) -> None:
        """Revoke a session (logout)."""
        db = self._ensure_db()
        await db.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        await db.commit()

    async def revoke_all_sessions(self, user_id: int) -> None:
        """Revoke all sessions for a user (e.g. password change)."""
        db = self._ensure_db()
        await db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        await db.commit()

    async def _cleanup_sessions(self, db: aiosqlite.Connection, now: float) -> None:
        """Delete expired sessions."""
        try:
            await db.execute(
                "DELETE FROM sessions WHERE expires_at < ? OR last_active_at < ?",
                (now, now - _SESSION_IDLE_TTL),
            )
            await db.commit()
        except Exception as e:
            logger.debug(f"Session cleanup error: {e}")

    # ── Login Failure Tracking (SEC-005) ────────────────────

    async def record_login_failure(self, username: str, client_ip: str) -> None:
        """Record a failed login attempt."""
        db = self._ensure_db()
        await db.execute(
            "INSERT INTO login_failures (username, client_ip, failed_at) VALUES (?, ?, ?)",
            (username, client_ip, time.time()),
        )
        await db.commit()

    async def clear_login_failures(self, username: str, client_ip: str) -> None:
        """Clear login failures after successful login."""
        db = self._ensure_db()
        await db.execute(
            "DELETE FROM login_failures WHERE username = ? AND client_ip = ?",
            (username, client_ip),
        )
        await db.commit()

    async def check_login_locked(self, username: str, client_ip: str) -> Optional[int]:
        """Check if username or IP is locked out. Returns remaining seconds or None."""
        db = self._ensure_db()
        now = time.time()
        window_start = now - _LOGIN_LOCKOUT_SECONDS
        # Check both per-username and per-IP
        for col, val in (("username", username), ("client_ip", client_ip)):
            cursor = await db.execute(
                f"SELECT COUNT(*), MIN(failed_at) FROM login_failures WHERE {col} = ? AND failed_at > ?",
                (val, window_start),
            )
            row = await cursor.fetchone()
            if row and row[0] >= _LOGIN_FAIL_LIMIT:
                earliest = row[1]
                remaining = int(_LOGIN_LOCKOUT_SECONDS - (now - earliest))
                if remaining > 0:
                    return remaining
                # Window expired — clean up
                await db.execute(
                    f"DELETE FROM login_failures WHERE {col} = ? AND failed_at <= ?",
                    (val, now - _LOGIN_LOCKOUT_SECONDS),
                )
                await db.commit()
        return None

    # ── Verification Codes (SMS) ─────────────────────────

    async def save_verification_code(
        self, phone: str, code: str, purpose: str = "register", ttl_seconds: int = 300
    ) -> None:
        """Persist a fresh verification code, invalidating any prior unused codes for same phone+purpose."""
        from datetime import timedelta
        db = self._ensure_db()
        now = datetime.now()
        expires_at = now + timedelta(seconds=ttl_seconds)
        # Invalidate older unused codes for this phone+purpose
        await db.execute(
            "UPDATE verification_codes SET used = 1 WHERE phone = ? AND purpose = ? AND used = 0",
            (phone, purpose),
        )
        await db.execute(
            """INSERT INTO verification_codes (phone, code, purpose, created_at, expires_at, used, attempts)
               VALUES (?, ?, ?, ?, ?, 0, 0)""",
            (phone, code, purpose, now.isoformat(), expires_at.isoformat()),
        )
        await db.commit()

    async def verify_code(self, phone: str, code: str, purpose: str = "register") -> bool:
        """Verify a code. Returns True on match. Marks code used or increments attempts."""
        db = self._ensure_db()
        now = datetime.now().isoformat()
        cursor = await db.execute(
            """SELECT id, code, attempts FROM verification_codes
               WHERE phone = ? AND purpose = ? AND used = 0 AND expires_at > ?
               ORDER BY id DESC LIMIT 1""",
            (phone, purpose, now),
        )
        row = await cursor.fetchone()  # (id, stored_code, attempts)
        if not row:
            return False
        vc_id, stored_code, attempts = row
        if attempts >= 5:
            # Too many wrong guesses — invalidate
            await db.execute("UPDATE verification_codes SET used = 1 WHERE id = ?", (vc_id,))
            await db.commit()
            return False
        if not hmac.compare_digest(stored_code, code):
            await db.execute(
                "UPDATE verification_codes SET attempts = attempts + 1 WHERE id = ?", (vc_id,)
            )
            await db.commit()
            return False
        # Correct — mark used
        await db.execute("UPDATE verification_codes SET used = 1 WHERE id = ?", (vc_id,))
        await db.commit()
        return True

    async def check_send_rate(
        self, phone: str, purpose: str = "register",
        per_minute_limit: int = 1, per_day_limit: int = 10
    ) -> Optional[str]:
        """Return an error string if send rate exceeded, else None."""
        from datetime import timedelta
        db = self._ensure_db()
        now = datetime.now()
        one_minute_ago = (now - timedelta(seconds=60)).isoformat()
        one_day_ago = (now - timedelta(days=1)).isoformat()
        cursor = await db.execute(
            "SELECT COUNT(*) FROM verification_codes WHERE phone = ? AND purpose = ? AND created_at > ?",
            (phone, purpose, one_minute_ago),
        )
        if (await cursor.fetchone())[0] >= per_minute_limit:
            return "发送过于频繁，请 60 秒后再试"
        cursor = await db.execute(
            "SELECT COUNT(*) FROM verification_codes WHERE phone = ? AND purpose = ? AND created_at > ?",
            (phone, purpose, one_day_ago),
        )
        if (await cursor.fetchone())[0] >= per_day_limit:
            return "今日发送次数已达上限，请明天再试"
        return None

    # ── User CRUD ──────────────────────────────────────────

    async def register(
        self, username: str, password: str, display_name: str = "", is_admin: bool = False
    ) -> dict:
        """Register a new user. Returns user dict with api_key."""
        db = self._ensure_db()
        now = datetime.now().isoformat()
        api_key = _generate_api_key()
        pw_hash = _hash_password(password)

        api_key_hashed = _hash_api_key(api_key)
        try:
            cursor = await db.execute(
                """INSERT INTO users (username, password_hash, display_name, api_key, api_key_hash, is_admin, created_at, last_active_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    username,
                    pw_hash,
                    display_name or username,
                    api_key_hashed,
                    api_key_hashed,
                    int(is_admin),
                    now,
                    now,
                ),
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            raise ValueError(f"Username '{username}' already exists")

        user_id = cursor.lastrowid

        return {
            "id": user_id,
            "username": username,
            "display_name": display_name or username,
            "api_key": api_key,
            "is_admin": is_admin,
        }

    async def register_with_phone(
        self, phone: str, password: str, display_name: str = "", is_admin: bool = False
    ) -> dict:
        """Register a user identified by phone number (phone_verified=1 immediately after code check)."""
        db = self._ensure_db()
        now = datetime.now().isoformat()
        api_key = _generate_api_key()
        pw_hash = _hash_password(password)
        api_key_hashed = _hash_api_key(api_key)
        # Use phone as username (strip + normalise)
        username = f"u{phone}"
        _display = display_name or f"用户{phone[-4:]}"
        try:
            cursor = await db.execute(
                """INSERT INTO users
                   (username, password_hash, display_name, api_key, api_key_hash,
                    is_admin, created_at, last_active_at, phone, phone_verified)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (
                    username,
                    pw_hash,
                    _display,
                    api_key_hashed,
                    api_key_hashed,
                    int(is_admin),
                    now,
                    now,
                    phone,
                ),
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            raise ValueError(f"Phone '{phone}' already registered")
        return {
            "id": cursor.lastrowid,
            "username": username,
            "display_name": _display,
            "api_key": api_key,
            "is_admin": is_admin,
            "phone": phone,
        }

    async def login(self, username: str, password: str) -> Optional[dict]:
        """Verify credentials and return a user dict, or None."""
        db = self._ensure_db()
        cursor = await db.execute(
            "SELECT id, username, password_hash, display_name, is_admin FROM users WHERE username = ?",
            (username,),
        )
        row = await cursor.fetchone()
        if not row:
            return None

        if not _verify_password(password, row[2]):
            return None

        # Gradual migration: re-hash with bcrypt if using legacy algorithm
        if _needs_rehash(row[2]):
            new_hash = _hash_password(password)
            await db.execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (new_hash, row[0]),
            )
            logger.info(f"Migrated password hash for user '{username}' to bcrypt")

        # Update last_active
        await db.execute(
            "UPDATE users SET last_active_at = ? WHERE id = ?",
            (datetime.now().isoformat(), row[0]),
        )
        await db.commit()

        return {
            "id": row[0],
            "username": row[1],
            "display_name": row[3],
            "api_key": None,
            "is_admin": bool(row[4]),
        }

    async def login_by_phone(self, phone: str, password: str) -> Optional[dict]:
        """Login using phone number + password."""
        db = self._ensure_db()
        cursor = await db.execute(
            "SELECT id, username, password_hash, display_name, is_admin FROM users WHERE phone = ?",
            (phone,),
        )
        row = await cursor.fetchone()
        if not row:
            return None
        if not _verify_password(password, row[2]):
            return None
        if _needs_rehash(row[2]):
            new_hash = _hash_password(password)
            await db.execute("UPDATE users SET password_hash = ? WHERE id = ?", (new_hash, row[0]))
        await db.execute(
            "UPDATE users SET last_active_at = ? WHERE id = ?",
            (datetime.now().isoformat(), row[0]),
        )
        await db.commit()
        return {
            "id": row[0], "username": row[1],
            "display_name": row[3], "api_key": None, "is_admin": bool(row[4]),
        }

    async def get_by_api_key(self, api_key: str) -> Optional[dict]:
        """Look up user by API key using deterministic hash-based storage."""
        db = self._ensure_db()
        key_hash = _hash_api_key(api_key)
        cursor = await db.execute(
            "SELECT id, username, display_name, is_admin, last_active_at FROM users WHERE api_key_hash = ?",
            (key_hash,),
        )
        row = await cursor.fetchone()
        if not row:
            return None

        # Throttle last_active updates to once per 5 minutes to reduce writes
        now = datetime.now()
        try:
            last = datetime.fromisoformat(row[4]) if row[4] else None
            if not last or (now - last).total_seconds() > 300:
                await db.execute(
                    "UPDATE users SET last_active_at = ? WHERE id = ?",
                    (now.isoformat(), row[0]),
                )
                await db.commit()
        except Exception as e:
            logger.debug(f"last_active_at update failed: {e}")  # #24: never silent

        return {
            "id": row[0],
            "username": row[1],
            "display_name": row[2],
            "api_key": None,
            "is_admin": bool(row[3]),
        }

    async def has_api_key(self, user_id: int) -> bool:
        db = self._ensure_db()
        cursor = await db.execute(
            "SELECT api_key FROM users WHERE id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return bool(row and row[0])

    async def list_users(self) -> list[dict]:
        """List all users (admin only)."""
        db = self._ensure_db()
        cursor = await db.execute(
            "SELECT id, username, display_name, is_admin, created_at, last_active_at FROM users ORDER BY created_at"
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": r[0], "username": r[1], "display_name": r[2],
                "is_admin": bool(r[3]), "created_at": r[4], "last_active_at": r[5],
            }
            for r in rows
        ]

    async def update_password(self, username: str, new_password: str) -> bool:
        """Update a user's password. Returns True if user found and updated.
        SEC-L1-04: Also revokes all sessions (NIST SP 800-63B §7.1)."""
        db = self._ensure_db()
        new_hash = _hash_password(new_password)
        cursor = await db.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (new_hash, username),
        )
        await db.commit()
        if cursor.rowcount > 0:
            uid_cursor = await db.execute("SELECT id FROM users WHERE username = ?", (username,))
            uid_row = await uid_cursor.fetchone()
            if uid_row:
                await self.revoke_all_sessions(uid_row[0])
        return cursor.rowcount > 0

    async def reset_api_key(self, user_id: int) -> str:
        """Generate and set a new API key for a user."""
        db = self._ensure_db()
        new_key = _generate_api_key()
        new_hash = _hash_api_key(new_key)
        await db.execute(
            "UPDATE users SET api_key = ?, api_key_hash = ? WHERE id = ?",
            (new_hash, new_hash, user_id),
        )
        await db.commit()
        return new_key

    # ── Query History ──────────────────────────────────────

    async def save_query(
        self,
        user_id: int,
        query_id: str,
        session_id: str | None,
        question: str,
        mode: str,
        final_answer: str,
        confidence: float,
        contributor_count: int,
        latency_ms: int,
        estimated_cost_usd: float,
        user_marked_usable: int | None = None,
        quality_gate: str = "",
        best_single_answer: str = "",
        has_divergence: bool = False,
        divergence_summary: str = "",
        key_insights: list | None = None,
        divergence_points: list | None = None,
    ) -> None:
        """Save a query to user's history."""
        import json as _json
        db = self._ensure_db()
        now = datetime.now().isoformat()
        await db.execute(
            """INSERT INTO query_history
               (user_id, query_id, session_id, question, mode, final_answer, confidence,
                contributor_count, latency_ms, estimated_cost_usd, user_marked_usable,
                created_at, quality_gate, best_single_answer,
                has_divergence, divergence_summary, key_insights, divergence_points)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, query_id, session_id, question, mode, final_answer, confidence,
             contributor_count, latency_ms, estimated_cost_usd, user_marked_usable,
             now, quality_gate, best_single_answer,
             int(has_divergence),
             divergence_summary,
             _json.dumps(key_insights or [], ensure_ascii=False),
             _json.dumps(divergence_points or [], ensure_ascii=False)),
        )
        await db.commit()

    async def get_history(self, user_id: int, limit: int = 50, offset: int = 0) -> list[dict]:
        """Get user's query history, newest first."""
        import json as _json
        db = self._ensure_db()
        cursor = await db.execute(
            """SELECT query_id, session_id, question, mode, final_answer, confidence,
                      contributor_count, latency_ms, estimated_cost_usd,
                      user_marked_usable, created_at, quality_gate, best_single_answer,
                      has_divergence, divergence_summary, key_insights, divergence_points
               FROM query_history WHERE user_id = ?
               ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (user_id, limit, offset),
        )
        rows = await cursor.fetchall()

        def _safe_json(val, fallback):
            try:
                return _json.loads(val) if val else fallback
            except Exception:
                return fallback

        return [
            {
                "query_id": r[0], "session_id": r[1], "question": r[2], "mode": r[3],
                "final_answer": r[4], "confidence": r[5],
                "contributor_count": r[6], "latency_ms": r[7],
                "estimated_cost_usd": r[8], "user_marked_usable": r[9],
                "created_at": r[10],
                "quality_gate": r[11] or "",
                "best_single_answer": r[12] or "",
                "has_divergence": bool(r[13]),
                "divergence_summary": r[14] or "",
                "key_insights": _safe_json(r[15], []),
                "divergence_points": _safe_json(r[16], []),
            }
            for r in rows
        ]

    async def get_history_count(self, user_id: int) -> int:
        """Count user's total queries."""
        db = self._ensure_db()
        cursor = await db.execute(
            "SELECT COUNT(*) FROM query_history WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_cost_by_date(self, date_str: str) -> dict:
        """Aggregate real per-query cost from query_history for a given date (YYYY-MM-DD).

        v4.30: Used by /admin/cost-report to replace hard-coded P50 estimates.
        Returns per-mode actual cost sums and per-user cost totals.
        Falls back gracefully if no records exist for the date.
        """
        db = self._ensure_db()
        # Per-mode: SUM(estimated_cost_usd) + COUNT(*) grouped by mode
        cursor = await db.execute(
            """SELECT mode,
                      COUNT(*) AS query_count,
                      SUM(estimated_cost_usd) AS total_cost
               FROM query_history
               WHERE created_at >= ? AND created_at < date(?, '+1 day')
               GROUP BY mode""",
            (date_str, date_str),
        )
        mode_rows = await cursor.fetchall()

        mode_counts: dict[str, int] = {}
        mode_costs: dict[str, float] = {}
        for row in mode_rows:
            mode_name = (row[0] or "unknown").lower()
            mode_counts[mode_name] = row[1]
            mode_costs[mode_name] = round(row[2] or 0.0, 6)

        # Per-user: SUM(estimated_cost_usd) grouped by user_id
        cursor2 = await db.execute(
            """SELECT user_id,
                      COUNT(*) AS query_count,
                      SUM(estimated_cost_usd) AS total_cost
               FROM query_history
               WHERE created_at >= ? AND created_at < date(?, '+1 day')
               GROUP BY user_id
               ORDER BY total_cost DESC
               LIMIT 10""",
            (date_str, date_str),
        )
        user_rows = await cursor2.fetchall()
        user_costs = [
            {"user_id": r[0], "query_count": r[1], "total_cost_usd": round(r[2] or 0.0, 6)}
            for r in user_rows
        ]

        return {
            "mode_counts": mode_counts,
            "mode_costs_usd": mode_costs,
            "top_users_by_cost": user_costs,
        }

    # ── FM-05: Query by ID (v5.2: IDOR-safe single-query fetch) ──

    async def get_query_by_id(self, user_id: int, query_id: str) -> dict | None:
        """Get a single query by ID, scoped to user (IDOR-safe).

        Returns None if query doesn't exist or belongs to a different user.
        """
        import json as _json
        db = self._ensure_db()
        cursor = await db.execute(
            """SELECT query_id, session_id, question, mode, final_answer, confidence,
                      contributor_count, latency_ms, estimated_cost_usd,
                      user_marked_usable, created_at, quality_gate, best_single_answer,
                      has_divergence, divergence_summary, key_insights, divergence_points
               FROM query_history WHERE user_id = ? AND query_id = ?""",
            (user_id, query_id),
        )
        r = await cursor.fetchone()
        if not r:
            return None

        def _safe_json(val, fallback):
            try:
                return _json.loads(val) if val else fallback
            except Exception:
                return fallback

        return {
            "query_id": r[0],
            "session_id": r[1],
            "question": r[2],
            "mode": r[3],
            "final_answer": r[4] or "",
            "confidence": r[5] or 0.0,
            "contributor_count": r[6] or 0,
            "latency_ms": r[7] or 0,
            "estimated_cost_usd": r[8] or 0.0,
            "user_marked_usable": bool(r[9]),
            "created_at": r[10],
            "quality_gate": r[11] or "",
            "best_single_answer": r[12] or "",
            "has_divergence": bool(r[13]),
            "divergence_summary": r[14] or "",
            "key_insights": _safe_json(r[15], []),
            "divergence_points": _safe_json(r[16], []),
        }

    # ── FM-04: Account Deletion (原则#22 用户主权) ──────────

    async def verify_password_by_id(self, user_id: int, password: str) -> bool:
        """Step-up auth: verify password for an already-authenticated user (RC-10).

        Used before irreversible operations (account deletion) to ensure the
        request is not a CSRF/session-hijack attack.
        """
        db = self._ensure_db()
        cursor = await db.execute(
            "SELECT password_hash FROM users WHERE id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return False
        return _verify_password(password, row[0])

    async def delete_user(self, user_id: int) -> None:
        """Permanently delete user and all associated data (原则#22).

        Removes: users record, query history, sessions, login failures,
        verification codes. Irreversible.
        """
        db = self._ensure_db()
        await db.execute("DELETE FROM query_history WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        await db.execute("DELETE FROM login_failures WHERE username = (SELECT username FROM users WHERE id = ?)", (user_id,))
        await db.execute(
            "DELETE FROM verification_codes WHERE phone = (SELECT phone FROM users WHERE id = ?)",
            (user_id,),
        )
        await db.execute("DELETE FROM users WHERE id = ?", (user_id,))
        await db.commit()
        logger.info(f"User {user_id} and all data permanently deleted (原则#22)")

    # ── Lifecycle ──────────────────────────────────────────

    async def health_check(self) -> bool:
        if not self._db:
            return False
        try:
            cursor = await self._db.execute("SELECT 1")
            row = await cursor.fetchone()
            return row is not None
        except Exception:
            return False

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None
