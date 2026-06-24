from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOCRATIC_FILE = ROOT / "src/agoracle/api/routes/socratic.py"
CLIENT_FILE = ROOT / "web/src/api/client.ts"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _slice(text: str, start_anchor: str, end_anchor: str) -> str:
    start = text.find(start_anchor)
    assert start >= 0, f"start anchor not found: {start_anchor}"
    end = text.find(end_anchor, start + len(start_anchor))
    assert end >= 0, f"end anchor not found: {end_anchor}"
    return text[start:end]


def _backend_events(section: str) -> set[str]:
    return set(re.findall(r'yield\s+\{\s*"event":\s*"([^"]+)"', section, re.S))


def _frontend_cases(section: str) -> set[str]:
    return set(re.findall(r'case\s+"([^"]+)"\s*:', section))


def test_socratic_terminal_events_are_synced_between_backend_and_frontend() -> None:
    socratic_text = _read(SOCRATIC_FILE)
    client_text = _read(CLIENT_FILE)

    backend = _slice(
        socratic_text,
        '@router.post("/socratic/start/stream")',
        '@router.post("/socratic/respond"',
    )
    frontend = _slice(
        client_text,
        "async socraticStartStream(",
        "async socraticRespond(",
    )

    required = {"socratic_ready", "socratic_error"}
    assert required.issubset(_backend_events(backend))
    assert required.issubset(_frontend_cases(frontend))


def test_socratic_heartbeat_is_explicitly_handled_on_frontend() -> None:
    socratic_text = _read(SOCRATIC_FILE)
    client_text = _read(CLIENT_FILE)

    backend = _slice(
        socratic_text,
        '@router.post("/socratic/start/stream")',
        '@router.post("/socratic/respond"',
    )
    frontend = _slice(
        client_text,
        "async socraticStartStream(",
        "async socraticRespond(",
    )

    assert 'yield {"event": "heartbeat", "data": ""}' in backend
    assert 'case "heartbeat":' in frontend


def test_socratic_non_terminal_events_are_synced_between_backend_and_frontend() -> None:
    socratic_text = _read(SOCRATIC_FILE)
    client_text = _read(CLIENT_FILE)

    backend = _slice(
        socratic_text,
        '@router.post("/socratic/start/stream")',
        '@router.post("/socratic/respond"',
    )
    frontend = _slice(
        client_text,
        "async socraticStartStream(",
        "async socraticRespond(",
    )

    required = {"socratic_stage", "socratic_contributor", "socratic_divergence"}
    assert required.issubset(_backend_events(backend))
    assert required.issubset(_frontend_cases(frontend))
