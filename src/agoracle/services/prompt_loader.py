"""
Prompt Loader — loads and renders prompt templates from prompts/ directory.

Prompts are Markdown files with {placeholder} variables for injection.
Templates are cached in memory after first load.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_KNOWN_PLACEHOLDERS_RE = re.compile(
    r"\{(?:profile_section|rag_section|session_section|context_section|web_search_instruction)\}"
)


class PromptLoader:
    """Load and render prompt templates."""

    def __init__(self, prompts_dir: str | Path) -> None:
        self.prompts_dir = Path(prompts_dir)
        self._cache: dict[str, str] = {}

        if not self.prompts_dir.exists():
            logger.warning(f"Prompts directory not found: {self.prompts_dir}")

    @staticmethod
    def _normalize_language(language: str | None) -> str:
        if not language:
            return "zh-CN"
        value = language.strip().replace("_", "-").lower()
        if value.startswith("en"):
            return "en-US"
        if value.startswith("zh"):
            return "zh-CN"
        return "zh-CN"

    def _cache_key(self, name: str, language: str) -> str:
        return f"{language}:{name}"

    def _candidate_paths(self, name: str, language: str) -> list[Path]:
        normalized = self._normalize_language(language)
        candidates = [
            self.prompts_dir / normalized / f"{name}.md",
            self.prompts_dir / "zh-CN" / f"{name}.md",
            self.prompts_dir / f"{name}.md",
        ]
        deduped: list[Path] = []
        seen: set[Path] = set()
        for path in candidates:
            if path not in seen:
                deduped.append(path)
                seen.add(path)
        return deduped

    def load(self, name: str, language: str = "zh-CN") -> str:
        """
        Load a prompt template by name (without .md extension).

        Returns the raw template string with {placeholders} intact.
        Cached after first load.
        """
        normalized = self._normalize_language(language)
        cache_key = self._cache_key(name, normalized)
        if cache_key in self._cache:
            return self._cache[cache_key]

        for path in self._candidate_paths(name, normalized):
            if not path.exists():
                continue
            content = path.read_text(encoding="utf-8")
            self._cache[cache_key] = content
            return content

        logger.error(
            "Prompt template not found: %s",
            ", ".join(str(path) for path in self._candidate_paths(name, normalized)),
        )
        return ""

    def render(self, name: str, language: str = "zh-CN", **kwargs: str) -> str:
        """
        Load a prompt and fill in placeholders.

        Placeholders use Python format syntax: {profile_section}, {rag_section}, etc.
        Missing placeholders are replaced with empty string.
        """
        template = self.load(name, language=language)
        if not template:
            return ""

        # Replace known placeholders; leave unknown ones empty
        for key, value in kwargs.items():
            template = template.replace(f"{{{key}}}", value)

        # Clean up only known template placeholders (not arbitrary {word} in user content)
        template = _KNOWN_PLACEHOLDERS_RE.sub("", template)

        return template.strip()

    def clear_cache(self) -> None:
        """Clear the prompt template cache."""
        self._cache.clear()

    @property
    def available_prompts(self) -> list[str]:
        """List available prompt template names."""
        if not self.prompts_dir.exists():
            return []
        prompts = {p.stem for p in self.prompts_dir.glob("*.md")}
        prompts.update(
            p.stem
            for lang_dir in self.prompts_dir.iterdir()
            if lang_dir.is_dir()
            for p in lang_dir.glob("*.md")
        )
        return sorted(prompts)
