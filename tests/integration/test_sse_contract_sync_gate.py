from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
QUERY_FILE = ROOT / "src/agoracle/api/routes/query.py"
MISC_FILE = ROOT / "src/agoracle/api/routes/misc.py"
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


def test_ask_terminal_events_are_synced_between_backend_and_frontend() -> None:
    query_text = _read(QUERY_FILE)
    client_text = _read(CLIENT_FILE)

    ask_backend = _slice(
        query_text,
        '@router.post("/ask/stream")',
        "return EventSourceResponse(event_generator())",
    )
    ask_frontend = _slice(
        client_text,
        "async askStream(",
        "// ── Roundtable v2.2.2 ──",
    )

    required = {"complete", "error"}
    backend = _backend_events(ask_backend)
    frontend = _frontend_cases(ask_frontend)

    assert required.issubset(backend)
    assert required.issubset(frontend)


def test_roundtable_terminal_events_are_synced_between_backend_and_frontend() -> None:
    misc_text = _read(MISC_FILE)
    client_text = _read(CLIENT_FILE)

    rt_backend = _slice(
        misc_text,
        '@router.post(\n    "/roundtable/start",',
        '@router.post("/roundtable/{session_id}/choice"',
    )
    rt_frontend = _slice(
        client_text,
        "async roundtableStream(",
        "async history(",
    )

    required = {"roundtable_complete", "roundtable_error", "auto_draft"}
    backend = _backend_events(rt_backend)
    frontend = _frontend_cases(rt_frontend)

    assert required.issubset(backend)
    assert required.issubset(frontend)


def test_socratic_terminal_events_are_synced_between_backend_and_frontend() -> None:
    socratic_text = _read(SOCRATIC_FILE)
    client_text = _read(CLIENT_FILE)

    soc_backend = _slice(
        socratic_text,
        '@router.post("/socratic/start/stream")',
        '@router.post("/socratic/respond"',
    )
    soc_frontend = _slice(
        client_text,
        "async socraticStartStream(",
        "async socraticRespond(",
    )

    required = {"socratic_ready", "socratic_error"}
    backend = _backend_events(soc_backend)
    frontend = _frontend_cases(soc_frontend)

    assert required.issubset(backend)
    assert required.issubset(frontend)


def test_ask_fallback_anchor_exists_for_missing_terminal() -> None:
    client_text = _read(CLIENT_FILE)
    ask_frontend = _slice(
        client_text,
        "async askStream(",
        "// ── Roundtable v2.2.2 ──",
    )

    assert "if (!gotComplete && !signal?.aborted)" in ask_frontend
    assert "SSE ended without complete event, falling back to /ask" in ask_frontend
    assert "const fallback = await this.ask(req);" in ask_frontend


def test_roundtable_terminal_tracking_and_disconnect_anchor_exist() -> None:
    client_text = _read(CLIENT_FILE)
    rt_frontend = _slice(
        client_text,
        "async roundtableStream(",
        "async history(",
    )

    assert "let receivedTerminal = false;" in rt_frontend
    assert rt_frontend.count("receivedTerminal = true;") >= 3
    assert "if (!receivedTerminal && !signal?.aborted)" in rt_frontend
    assert 'callbacks.onError?.(i18n.t("api.client.errors.roundtable.connectionInterrupted"));' in rt_frontend


def test_terminal_payload_key_fields_exist_on_backend_side() -> None:
    query_text = _read(QUERY_FILE)
    misc_text = _read(MISC_FILE)

    ask_backend = _slice(
        query_text,
        '@router.post("/ask/stream")',
        "return EventSourceResponse(event_generator())",
    )
    rt_backend = _slice(
        misc_text,
        '@router.post(\n    "/roundtable/start",',
        '@router.post("/roundtable/{session_id}/choice"',
    )

    assert '"final_answer": r.final_answer or ""' in ask_backend
    assert 'yield {"event": "error", "data": json.dumps({"error": event.error})}' in ask_backend
    assert '"decision_packet": {' in rt_backend
    assert '"error": event.error' in rt_backend
