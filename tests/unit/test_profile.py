"""Unit tests for JSON profile store."""

from __future__ import annotations

import pytest

from agoracle.adapters.profile.json_profile import JsonProfileStore
from agoracle.domain.types import UserProfile


class TestJsonProfileStore:
    """Tests for the JSON profile store."""

    @pytest.mark.asyncio
    async def test_create_default_profile(self, tmp_path):
        """Creates a default profile if none exists."""
        store = JsonProfileStore(tmp_path / "profile.json")
        profile = await store.load()
        assert profile.preferred_language == "zh-CN"
        assert profile.preferred_depth == "detailed"
        assert (tmp_path / "profile.json").exists()

    @pytest.mark.asyncio
    async def test_save_and_load(self, tmp_path):
        """Save then load preserves all fields."""
        store = JsonProfileStore(tmp_path / "profile.json")
        profile = UserProfile(
            preferred_language="en",
            preferred_depth="concise",
            explicit_rules=["Rule 1", "Rule 2"],
            topic_expertise={"python": 0.9, "rust": 0.5},
            comfort_zone_topics=["python"],
            growth_zone_topics=["rust", "haskell"],
        )
        await store.save(profile)

        loaded = await store.load()
        assert loaded.preferred_language == "en"
        assert loaded.preferred_depth == "concise"
        assert loaded.explicit_rules == ["Rule 1", "Rule 2"]
        assert loaded.topic_expertise == {"python": 0.9, "rust": 0.5}
        assert loaded.comfort_zone_topics == ["python"]
        assert loaded.growth_zone_topics == ["rust", "haskell"]

    @pytest.mark.asyncio
    async def test_get_summary_new_user(self, tmp_path):
        """Summary for new user shows default info."""
        store = JsonProfileStore(tmp_path / "profile.json")
        summary = await store.get_summary()
        assert "zh-CN" in summary
        assert "detailed" in summary

    @pytest.mark.asyncio
    async def test_get_summary_with_expertise(self, tmp_path):
        """Summary includes top expertise topics."""
        store = JsonProfileStore(tmp_path / "profile.json")
        profile = UserProfile(
            topic_expertise={"python": 0.9, "rust": 0.7, "go": 0.5},
        )
        await store.save(profile)
        summary = await store.get_summary()
        assert "python" in summary

    @pytest.mark.asyncio
    async def test_get_summary_supports_english_labels(self, tmp_path):
        """Summary labels switch to English when language is overridden."""
        store = JsonProfileStore(tmp_path / "profile.json")
        profile = UserProfile(
            preferred_language="en",
            explicit_rules=["Use concrete examples"],
            topic_expertise={"python": 0.9},
            topic_frequency={"python": 4},
            topic_depth_map={"python": 4},
            recent_turns=[{"topic": "python", "summary": "Discussed async patterns"}],
        )
        await store.save(profile)
        summary = await store.get_summary(language="en-US")
        assert "Language preference" in summary
        assert "Frequently explored" in summary
        assert "Recently discussed" in summary

    @pytest.mark.asyncio
    async def test_cognitive_profile_roundtrip(self, tmp_path):
        """CognitiveProfile fields survive serialization."""
        store = JsonProfileStore(tmp_path / "profile.json")
        profile = UserProfile(
            cognitive_quadrant_dist={"known_known": 5, "known_unknown": 3, "unknown_known": 2, "unknown_unknown": 10},
            socratic_completion_rate=0.75,
            average_reasoning_quality=0.6,
            last_challenge_date="2026-02-12",
        )
        await store.save(profile)

        loaded = await store.load()
        assert loaded.cognitive_quadrant_dist["unknown_unknown"] == 10
        assert loaded.socratic_completion_rate == 0.75
        assert loaded.average_reasoning_quality == 0.6
        assert loaded.last_challenge_date == "2026-02-12"
