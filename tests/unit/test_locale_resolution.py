from __future__ import annotations

from types import SimpleNamespace

from agoracle.api.deps import normalize_locale, parse_accept_language, resolve_language


def test_normalize_locale_maps_supported_variants():
    assert normalize_locale("en-GB") == "en-US"
    assert normalize_locale("en") == "en-US"
    assert normalize_locale("zh-TW") == "zh-CN"
    assert normalize_locale("fr-FR") == "zh-CN"


def test_parse_accept_language_uses_first_tag():
    assert parse_accept_language("en-GB,en;q=0.9,zh;q=0.8") == "en-GB"
    assert parse_accept_language(None) is None


def test_resolve_language_priority_chain():
    profile = SimpleNamespace(preferred_language="en")
    assert resolve_language("zh-CN", "en-US,en;q=0.9", profile) == "zh-CN"
    assert resolve_language(None, "en-GB,en;q=0.9", profile) == "en-US"
    assert resolve_language(None, None, profile) == "en-US"
    assert resolve_language(None, None, None) == "zh-CN"
