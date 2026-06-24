from __future__ import annotations

from agoracle.services.prompt_loader import PromptLoader


def test_prompt_loader_falls_back_to_zh_cn_then_root(tmp_path):
    prompts_dir = tmp_path / "prompts"
    (prompts_dir / "zh-CN").mkdir(parents=True)
    (prompts_dir / "zh-CN" / "judge_light.md").write_text("zh prompt", encoding="utf-8")
    (prompts_dir / "contributor.md").write_text("root prompt", encoding="utf-8")

    loader = PromptLoader(prompts_dir)

    assert loader.load("judge_light", language="en-US") == "zh prompt"
    assert loader.load("contributor", language="en-US") == "root prompt"


def test_prompt_loader_cache_key_includes_language(tmp_path):
    prompts_dir = tmp_path / "prompts"
    (prompts_dir / "zh-CN").mkdir(parents=True)
    (prompts_dir / "en-US").mkdir(parents=True)
    (prompts_dir / "zh-CN" / "safety_rules.md").write_text("中文规则", encoding="utf-8")
    (prompts_dir / "en-US" / "safety_rules.md").write_text("English rules", encoding="utf-8")

    loader = PromptLoader(prompts_dir)

    assert loader.load("safety_rules", language="zh-CN") == "中文规则"
    assert loader.load("safety_rules", language="en-US") == "English rules"
