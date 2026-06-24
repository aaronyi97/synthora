"""
Unit tests for QuotaService SQLite backend (Q3-rootfix).

Covers:
- Basic record_usage + check_quota flow
- Cross-process safety: two separate QuotaService instances pointing to same DB
  behave correctly (simulates two gunicorn workers)
- set_user_total_credits / get_user_total_credits
- delete_user removes all records
- get_all_usage and get_user_history
- Quota exceeded returns correct error dict
- Admin (user_id<=0) always passes quota
- check_governance no threading.Lock in quota.py
"""

from __future__ import annotations

import os
import tempfile
from unittest.mock import MagicMock

import pytest

from agoracle.services.quota import QuotaService


def _make_quota(data_dir: str, enabled: bool = True) -> QuotaService:
    cfg = MagicMock()
    cfg.enabled = enabled
    cfg.light = 100
    cfg.deep = 10
    cfg.research = 5
    cfg.socratic = 20
    return QuotaService(cfg, data_dir=data_dir)


@pytest.fixture()
def data_dir(tmp_path):
    return str(tmp_path)


class TestQuotaBasic:
    def test_record_and_get_usage(self, data_dir):
        q = _make_quota(data_dir)
        q.record_usage(1, "light")
        q.record_usage(1, "light")
        q.record_usage(1, "deep")
        usage = q.get_usage(1)
        assert usage["light"] == 2
        assert usage["deep"] == 1
        assert usage["research"] == 0

    def test_lifetime_usage(self, data_dir):
        q = _make_quota(data_dir)
        q.record_usage(2, "research")
        q.record_usage(2, "light")
        lu = q.get_lifetime_usage(2)
        assert lu["research"] == 1
        assert lu["light"] == 1

    def test_lifetime_credits_used(self, data_dir):
        q = _make_quota(data_dir)
        q.record_usage(3, "deep")
        used = q.get_lifetime_credits_used(3)
        assert used == 60

    def test_default_total_credits(self, data_dir):
        q = _make_quota(data_dir)
        assert q.get_user_total_credits(99) == 500

    def test_set_and_get_total_credits(self, data_dir):
        q = _make_quota(data_dir)
        q.set_user_total_credits(10, 1000)
        assert q.get_user_total_credits(10) == 1000

    def test_check_quota_ok(self, data_dir):
        q = _make_quota(data_dir)
        result = q.check_quota(1, "light")
        assert result is None

    def test_check_quota_exceeded(self, data_dir):
        q = _make_quota(data_dir)
        q.set_user_total_credits(5, 0)
        result = q.check_quota(5, "light")
        assert result is not None
        assert result["error"] == "quota_exceeded"
        assert result["credits_remaining"] == 0

    def test_admin_always_passes(self, data_dir):
        q = _make_quota(data_dir)
        q.set_user_total_credits(0, 0)
        assert q.check_quota(0, "research") is None
        assert q.check_quota(-1, "deep") is None

    def test_quota_disabled(self, data_dir):
        q = _make_quota(data_dir, enabled=False)
        q.set_user_total_credits(7, 0)
        assert q.check_quota(7, "deep") is None

    def test_delete_user(self, data_dir):
        q = _make_quota(data_dir)
        q.record_usage(4, "light")
        q.set_user_total_credits(4, 999)
        q.delete_user(4)
        assert q.get_usage(4)["light"] == 0
        assert q.get_user_total_credits(4) == 500

    def test_get_all_usage(self, data_dir):
        q = _make_quota(data_dir)
        q.record_usage(11, "light")
        q.record_usage(12, "deep")
        all_usage = q.get_all_usage()
        assert "11" in all_usage
        assert "12" in all_usage
        assert all_usage["11"]["total"] == 1

    def test_get_user_history(self, data_dir):
        q = _make_quota(data_dir)
        q.record_usage(20, "light")
        history = q.get_user_history(20, days=7)
        assert len(history) >= 1
        today_data = list(history.values())[0]
        assert today_data.get("light", 0) >= 1

    def test_get_limits(self, data_dir):
        q = _make_quota(data_dir)
        limits = q.get_limits()
        assert "light" in limits
        assert "deep" in limits


class TestQuotaMultiWorkerSafety:
    """Simulate two gunicorn workers sharing the same quota.db."""

    def test_two_instances_same_db_consistent(self, data_dir):
        """Both worker instances read consistent state after write via either."""
        worker1 = _make_quota(data_dir)
        worker2 = _make_quota(data_dir)

        worker1.record_usage(50, "light")
        worker1.record_usage(50, "light")

        usage_w2 = worker2.get_usage(50)
        assert usage_w2["light"] == 2

    def test_concurrent_increments_no_lost_updates(self, data_dir):
        """Multiple sequential writes from two instances accumulate correctly."""
        w1 = _make_quota(data_dir)
        w2 = _make_quota(data_dir)

        for _ in range(5):
            w1.record_usage(60, "light")
        for _ in range(5):
            w2.record_usage(60, "light")

        final = _make_quota(data_dir).get_usage(60)
        assert final["light"] == 10

    def test_credits_visible_across_instances(self, data_dir):
        """Credits set by one worker are visible to another."""
        w1 = _make_quota(data_dir)
        w2 = _make_quota(data_dir)
        w1.set_user_total_credits(70, 200)
        assert w2.get_user_total_credits(70) == 200

    def test_quota_exceeded_consistent_across_instances(self, data_dir):
        """Quota exceeded state is visible to a second instance."""
        w1 = _make_quota(data_dir)
        w2 = _make_quota(data_dir)
        w1.set_user_total_credits(80, 1)
        w1.record_usage(80, "deep")
        result = w2.check_quota(80, "light")
        assert result is not None
        assert result["error"] == "quota_exceeded"


class TestQuotaNoThreadingLock:
    """Regression: threading.Lock must not be imported or used in quota.py code."""

    def test_no_threading_import_in_source(self):
        import agoracle.services.quota as quota_module
        import inspect
        source = inspect.getsource(quota_module)
        code_lines = [
            line for line in source.splitlines()
            if not line.strip().startswith("#") and not line.strip().startswith('"""') and '"""' not in line
        ]
        code_only = "\n".join(code_lines)
        assert "import threading" not in code_only, (
            "threading imported in quota.py — Q3-rootfix regression"
        )
        assert "threading.Lock()" not in code_only, (
            "threading.Lock() used in quota.py — Q3-rootfix regression: "
            "must use SQLite WAL instead"
        )
