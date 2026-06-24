"""Synthora — Multi-model AI orchestration system."""

from __future__ import annotations

from pathlib import Path


def _read_version() -> str:
    """Single version source with robust fallback order.

    Prefer pyproject.toml in repo checkout (runtime truth for this project),
    then package metadata for installed environments.
    """
    try:
        # Python 3.11+
        import tomllib  # type: ignore[attr-defined]
    except Exception:
        # Python 3.9/3.10 fallback
        import tomli as tomllib  # type: ignore[no-redef]

    try:
        root = Path(__file__).resolve().parents[2]
        pyproject = root / "pyproject.toml"
        if pyproject.is_file():
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            value = data.get("project", {}).get("version", "")
            if isinstance(value, str) and value.strip():
                return value.strip()
    except Exception:
        pass

    try:
        from importlib.metadata import version as pkg_version

        return pkg_version("agoracle")
    except Exception:
        return "0.0.0-dev"


__version__ = _read_version()
