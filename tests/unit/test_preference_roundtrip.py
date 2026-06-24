from __future__ import annotations

from agoracle.adapters.profile.json_profile import JsonProfileStore
from agoracle.domain.types import UserProfile


def test_serialize_includes_preference_profile_fields():
    data = JsonProfileStore._serialize(UserProfile())

    assert "preferred_language" in data
    assert "preferred_depth" in data
    assert "explicit_rules" in data
    assert "mode_preference" in data


def test_serialize_deserialize_roundtrip_preserves_default_preference_fields():
    profile = UserProfile(
        explicit_rules=["use examples"],
        mode_preference={"deep": 0.8},
    )

    restored = JsonProfileStore._deserialize(JsonProfileStore._serialize(profile))

    assert restored.preferred_language == profile.preferred_language
    assert restored.preferred_depth == profile.preferred_depth
    assert restored.explicit_rules == profile.explicit_rules
    assert restored.mode_preference == profile.mode_preference


def test_serialize_deserialize_roundtrip_preserves_non_default_preferences():
    profile = UserProfile(
        preferred_language="en",
        preferred_depth="concise",
        explicit_rules=["rule1"],
        mode_preference={"research": 0.9},
    )

    restored = JsonProfileStore._deserialize(JsonProfileStore._serialize(profile))

    assert restored.preferred_language == "en"
    assert restored.preferred_depth == "concise"
    assert restored.explicit_rules == ["rule1"]
    assert restored.mode_preference == {"research": 0.9}
