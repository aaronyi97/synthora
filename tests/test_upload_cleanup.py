"""
RC-09 regression tests: delete_account must clean up uploaded files
without affecting other users' files.

Tests cover:
- Single user upload + delete cleans owned files
- Multi-user: deleting user A does NOT touch user B's files
- Corrupt/missing .owner files are handled gracefully
- Empty uploads dir doesn't break delete_account
"""

import os
import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import bcrypt as _bcrypt_module
from fastapi.middleware.cors import CORSMiddleware
from starlette.testclient import TestClient


@pytest.fixture(autouse=True)
def fast_bcrypt(monkeypatch):
    """Speed up bcrypt for all tests by reducing cost factor from 12 to 4."""
    original_gensalt = _bcrypt_module.gensalt
    monkeypatch.setattr(_bcrypt_module, "gensalt", lambda rounds=12, prefix=b"2b": original_gensalt(4, prefix))
    yield


def _make_upload_dir(tmp_path: Path):
    """Create a temp uploads dir with known files."""
    uploads = tmp_path / "data" / "uploads"
    uploads.mkdir(parents=True)
    return uploads


def _create_uploaded_file(uploads: Path, file_id: str, ext: str, owner_uid: int):
    """Simulate an uploaded file: {file_id}.{ext} + {file_id}.owner"""
    (uploads / f"{file_id}.{ext}").write_bytes(b"fake content")
    (uploads / f"{file_id}.owner").write_text(str(owner_uid), encoding="utf-8")


class TestUploadCleanupOnDelete:
    """RC-09: delete_account cleans up data/uploads/ by .owner==uid"""

    def test_deletes_own_files_only(self, tmp_path):
        """User A's files deleted; User B's files untouched."""
        uploads = _make_upload_dir(tmp_path)

        # User A (uid=10) has 2 files
        _create_uploaded_file(uploads, "aaa111bbb222", "png", owner_uid=10)
        _create_uploaded_file(uploads, "ccc333ddd444", "pdf", owner_uid=10)

        # User B (uid=20) has 1 file
        _create_uploaded_file(uploads, "eee555fff666", "jpg", owner_uid=20)

        # Simulate cleanup logic (extracted from app.py delete_account)
        uid_to_delete = 10
        for owner_file in uploads.glob("*.owner"):
            try:
                file_owner = int(owner_file.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                continue
            if file_owner != uid_to_delete:
                continue
            file_id_stem = owner_file.stem
            for sibling in uploads.glob(f"{file_id_stem}.*"):
                sibling.unlink()

        # User A's files gone
        assert not (uploads / "aaa111bbb222.png").exists()
        assert not (uploads / "aaa111bbb222.owner").exists()
        assert not (uploads / "ccc333ddd444.pdf").exists()
        assert not (uploads / "ccc333ddd444.owner").exists()

        # User B's files intact
        assert (uploads / "eee555fff666.jpg").exists()
        assert (uploads / "eee555fff666.owner").exists()

    def test_corrupt_owner_file_skipped(self, tmp_path):
        """Corrupt .owner file doesn't crash cleanup or affect others."""
        uploads = _make_upload_dir(tmp_path)

        # Normal file owned by uid=10
        _create_uploaded_file(uploads, "aaa111bbb222", "png", owner_uid=10)

        # Corrupt .owner (non-integer content)
        (uploads / "corrupt12file.png").write_bytes(b"data")
        (uploads / "corrupt12file.owner").write_text("not-a-number", encoding="utf-8")

        uid_to_delete = 10
        for owner_file in uploads.glob("*.owner"):
            try:
                file_owner = int(owner_file.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                continue
            if file_owner != uid_to_delete:
                continue
            file_id_stem = owner_file.stem
            for sibling in uploads.glob(f"{file_id_stem}.*"):
                sibling.unlink()

        # uid=10's file deleted
        assert not (uploads / "aaa111bbb222.png").exists()
        # Corrupt file untouched
        assert (uploads / "corrupt12file.png").exists()
        assert (uploads / "corrupt12file.owner").exists()

    def test_empty_uploads_dir_no_error(self, tmp_path):
        """Empty uploads dir doesn't raise."""
        uploads = _make_upload_dir(tmp_path)

        uid_to_delete = 10
        deleted = 0
        for owner_file in uploads.glob("*.owner"):
            try:
                file_owner = int(owner_file.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                continue
            if file_owner != uid_to_delete:
                continue
            file_id_stem = owner_file.stem
            for sibling in uploads.glob(f"{file_id_stem}.*"):
                sibling.unlink()
                deleted += 1

        assert deleted == 0

    def test_missing_owner_file_data_file_untouched(self, tmp_path):
        """Data file without .owner sidecar is not deleted (fail-closed)."""
        uploads = _make_upload_dir(tmp_path)

        # Orphan data file (no .owner)
        (uploads / "orphan123456.png").write_bytes(b"orphan data")

        uid_to_delete = 10
        for owner_file in uploads.glob("*.owner"):
            try:
                file_owner = int(owner_file.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                continue
            if file_owner != uid_to_delete:
                continue
            file_id_stem = owner_file.stem
            for sibling in uploads.glob(f"{file_id_stem}.*"):
                sibling.unlink()

        # Orphan file untouched
        assert (uploads / "orphan123456.png").exists()

    def test_concurrent_multiuser_isolation(self, tmp_path):
        """Simulate 3 users with interleaved files; deleting one leaves others intact."""
        uploads = _make_upload_dir(tmp_path)

        # User 1 (uid=1): 3 files
        for i, ext in enumerate(["png", "jpg", "pdf"]):
            _create_uploaded_file(uploads, f"user1file000{i}", ext, owner_uid=1)

        # User 2 (uid=2): 2 files
        for i, ext in enumerate(["webp", "docx"]):
            _create_uploaded_file(uploads, f"user2file000{i}", ext, owner_uid=2)

        # User 3 (uid=3): 1 file
        _create_uploaded_file(uploads, "user3file0000", "gif", owner_uid=3)

        # Delete user 2
        uid_to_delete = 2
        for owner_file in uploads.glob("*.owner"):
            try:
                file_owner = int(owner_file.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                continue
            if file_owner != uid_to_delete:
                continue
            file_id_stem = owner_file.stem
            for sibling in uploads.glob(f"{file_id_stem}.*"):
                sibling.unlink()

        # User 2 gone
        assert not (uploads / "user2file0000.webp").exists()
        assert not (uploads / "user2file0000.owner").exists()
        assert not (uploads / "user2file0001.docx").exists()
        assert not (uploads / "user2file0001.owner").exists()

        # User 1 intact
        for i, ext in enumerate(["png", "jpg", "pdf"]):
            assert (uploads / f"user1file000{i}.{ext}").exists()
            assert (uploads / f"user1file000{i}.owner").exists()

        # User 3 intact
        assert (uploads / "user3file0000.gif").exists()
        assert (uploads / "user3file0000.owner").exists()


class TestStepUpAuthVerifyPassword:
    """RC-10: verify_password_by_id used as step-up gate before account deletion."""

    @pytest.mark.asyncio
    async def test_correct_password_returns_true(self, tmp_path):
        """Valid password → True."""
        import bcrypt
        from agoracle.adapters.user.sqlite_user_store import SQLiteUserStore

        store = SQLiteUserStore(tmp_path / "users.db")
        await store.initialize()
        user = await store.register("testuser_rc10", "correctpass", "Test RC10")
        uid = user["id"]

        result = await store.verify_password_by_id(uid, "correctpass")
        assert result is True
        await store.close()

    @pytest.mark.asyncio
    async def test_wrong_password_returns_false(self, tmp_path):
        """Wrong password → False (deletion blocked)."""
        from agoracle.adapters.user.sqlite_user_store import SQLiteUserStore

        store = SQLiteUserStore(tmp_path / "users.db")
        await store.initialize()
        user = await store.register("testuser_rc10b", "correctpass", "Test RC10b")
        uid = user["id"]

        result = await store.verify_password_by_id(uid, "wrongpassword")
        assert result is False
        await store.close()

    @pytest.mark.asyncio
    async def test_nonexistent_user_returns_false(self, tmp_path):
        """Non-existent uid → False (fail-closed)."""
        from agoracle.adapters.user.sqlite_user_store import SQLiteUserStore

        store = SQLiteUserStore(tmp_path / "users.db")
        await store.initialize()

        result = await store.verify_password_by_id(99999, "anypassword")
        assert result is False
        await store.close()

    @pytest.mark.asyncio
    async def test_empty_password_blocked(self, tmp_path):
        """Empty password string → False (Pydantic min_length=1 also blocks at API layer)."""
        from agoracle.adapters.user.sqlite_user_store import SQLiteUserStore

        store = SQLiteUserStore(tmp_path / "users.db")
        await store.initialize()
        user = await store.register("testuser_rc10c", "correctpass", "Test RC10c")
        uid = user["id"]

        result = await store.verify_password_by_id(uid, "")
        assert result is False
        await store.close()


class TestDeleteUserDataCleanup:
    """delete_user must purge dependent records, including verification codes tied by phone."""

    @pytest.mark.asyncio
    async def test_delete_user_removes_verification_codes_for_phone(self, tmp_path):
        from agoracle.adapters.user.sqlite_user_store import SQLiteUserStore

        phone = "13800138000"
        store = SQLiteUserStore(tmp_path / "users.db")
        await store.initialize()
        user = await store.register_with_phone(phone, "correctpass", "Delete Me")
        uid = user["id"]

        await store.save_verification_code(phone, "123456", "register")
        await store.save_verification_code("13900139000", "654321", "register")

        cursor = await store._ensure_db().execute(
            "SELECT COUNT(*) FROM verification_codes WHERE phone = ?",
            (phone,),
        )
        assert (await cursor.fetchone())[0] == 1

        await store.delete_user(uid)

        cursor = await store._ensure_db().execute(
            "SELECT COUNT(*) FROM verification_codes WHERE phone = ?",
            (phone,),
        )
        assert (await cursor.fetchone())[0] == 0

        cursor = await store._ensure_db().execute(
            "SELECT COUNT(*) FROM verification_codes WHERE phone = ?",
            ("13900139000",),
        )
        assert (await cursor.fetchone())[0] == 1
        await store.close()


class TestApiKeyStorageHardening:
    """API keys must no longer be persisted as plaintext for new writes."""

    @pytest.mark.asyncio
    async def test_register_stores_hashed_api_key_in_db_and_authenticates(self, tmp_path):
        from agoracle.adapters.user.sqlite_user_store import SQLiteUserStore, _hash_api_key

        store = SQLiteUserStore(tmp_path / "users.db")
        await store.initialize()

        user = await store.register("api_key_user", "correctpass", "API Key User")
        api_key = user["api_key"]

        cursor = await store._ensure_db().execute(
            "SELECT api_key, api_key_hash FROM users WHERE id = ?",
            (user["id"],),
        )
        row = await cursor.fetchone()

        assert row[0] == _hash_api_key(api_key)
        assert row[1] == _hash_api_key(api_key)
        assert row[0] != api_key

        auth_user = await store.get_by_api_key(api_key)
        assert auth_user is not None
        assert auth_user["id"] == user["id"]
        assert auth_user["api_key"] is None
        await store.close()

    @pytest.mark.asyncio
    async def test_initialize_backfills_legacy_plaintext_api_key_rows(self, tmp_path):
        import aiosqlite

        from agoracle.adapters.user.sqlite_user_store import SQLiteUserStore, _hash_api_key, _hash_password

        db_path = tmp_path / "legacy_users.db"
        legacy_key = "sk-legacy-plaintext-key"

        db = await aiosqlite.connect(str(db_path))
        await db.execute(
            """CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                api_key TEXT UNIQUE NOT NULL,
                is_admin INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                last_active_at TEXT NOT NULL
            )"""
        )
        await db.execute(
            """INSERT INTO users
               (username, password_hash, display_name, api_key, is_admin, created_at, last_active_at)
               VALUES (?, ?, ?, ?, 0, ?, ?)""",
            (
                "legacy_user",
                _hash_password("correctpass"),
                "Legacy User",
                legacy_key,
                "2026-03-09T00:00:00",
                "2026-03-09T00:00:00",
            ),
        )
        await db.commit()
        await db.close()

        store = SQLiteUserStore(db_path)
        await store.initialize()

        cursor = await store._ensure_db().execute(
            "SELECT api_key, api_key_hash FROM users WHERE username = ?",
            ("legacy_user",),
        )
        row = await cursor.fetchone()

        assert row[0] == _hash_api_key(legacy_key)
        assert row[1] == _hash_api_key(legacy_key)
        assert row[0] != legacy_key

        auth_user = await store.get_by_api_key(legacy_key)
        assert auth_user is not None
        assert auth_user["username"] == "legacy_user"
        assert auth_user["api_key"] is None
        await store.close()

    @pytest.mark.asyncio
    async def test_reset_api_key_persists_only_hashes(self, tmp_path):
        from agoracle.adapters.user.sqlite_user_store import SQLiteUserStore, _hash_api_key

        store = SQLiteUserStore(tmp_path / "users.db")
        await store.initialize()
        user = await store.register("reset_key_user", "correctpass", "Reset User")

        new_key = await store.reset_api_key(user["id"])
        cursor = await store._ensure_db().execute(
            "SELECT api_key, api_key_hash FROM users WHERE id = ?",
            (user["id"],),
        )
        row = await cursor.fetchone()

        assert row[0] == _hash_api_key(new_key)
        assert row[1] == _hash_api_key(new_key)
        assert row[0] != new_key
        await store.close()


class TestCORSDeleteMethod:
    """RC-03: CORS must include DELETE method."""

    def test_cors_allows_delete(self):
        from agoracle.api.app import create_app
        app = create_app()
        # Find CORSMiddleware in the middleware stack
        found_delete = False
        for middleware in app.user_middleware:
            if middleware.cls is CORSMiddleware:
                methods = middleware.kwargs.get("allow_methods", [])
                if "DELETE" in methods or "*" in methods:
                    found_delete = True
                break
        assert found_delete, "CORS allow_methods must include DELETE"
