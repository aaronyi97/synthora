"""
Request audit logging middleware — structured JSONL security trail.

Logs every API request with: timestamp, client IP, method, path,
status code, latency, and user-agent. Written to data/logs/audit.jsonl.

This provides the security audit trail required for:
  - Incident investigation (who accessed what, when)
  - Abuse detection (unusual patterns)
  - Compliance reporting (access logs)

v2.6.7: Uses a single background consumer thread + queue instead of
thread-per-request, preventing thread explosion under high QPS.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import re
import threading
import time
from pathlib import Path
from typing import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger(__name__)

_DEFAULT_AUDIT_LOG_PATH = "data/logs/audit.jsonl"


class _AuditLogWriter:
    """Single-threaded consumer that drains a queue and writes to JSONL.

    One daemon thread per log path. Survives individual write failures
    and logs errors instead of silently dropping entries.
    """

    def __init__(self, log_path: Path) -> None:
        self._log_path = log_path
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=10000)
        self._thread = threading.Thread(
            target=self._consumer_loop, daemon=True, name="audit-log-writer"
        )
        self._thread.start()
        atexit.register(self.flush)

    def enqueue(self, line: str) -> None:
        """Enqueue a log line. Drops silently if queue is full (backpressure)."""
        try:
            self._queue.put_nowait(line)
        except queue.Full:
            logger.warning("Audit log queue full, dropping entry")

    def flush(self) -> None:
        """Drain remaining entries on shutdown."""
        self._queue.put(None)  # sentinel
        self._thread.join(timeout=5)

    def _consumer_loop(self) -> None:
        """Background thread: batch-drain queue and append to file."""
        while True:
            try:
                item = self._queue.get(timeout=1)
            except queue.Empty:
                continue
            if item is None:
                # Drain remaining items before exit
                self._drain_remaining()
                break
            self._write_line(item)

    def _drain_remaining(self) -> None:
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
                if item is not None:
                    self._write_line(item)
            except queue.Empty:
                break

    def _write_line(self, line: str) -> None:
        try:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            logger.error(f"Audit log write failed: {e}")


# Singleton writers per log path (process-wide)
_writers: dict[str, _AuditLogWriter] = {}
_writers_lock = threading.Lock()  # concurrency-ok: singleton guard in single-worker uvicorn; writes use queue-based consumer thread


def _get_writer(log_path: Path) -> _AuditLogWriter:
    key = str(log_path)
    if key not in _writers:
        with _writers_lock:
            if key not in _writers:  # double-checked locking
                _writers[key] = _AuditLogWriter(log_path)
    return _writers[key]


class AuditLogMiddleware(BaseHTTPMiddleware):
    """Log every API request as structured JSONL for security audit."""

    def __init__(self, app, enabled: bool = True, log_path: str = "") -> None:
        super().__init__(app)
        self._enabled = enabled and os.getenv("AUDIT_LOG_ENABLED", "1") != "0"
        self._log_path = Path(
            log_path or os.getenv("AUDIT_LOG_PATH", _DEFAULT_AUDIT_LOG_PATH)
        )
        if self._enabled:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            logger.info(f"Audit logging enabled: {self._log_path}")

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not self._enabled:
            return await call_next(request)

        start = time.monotonic()
        cf_ip = request.headers.get("CF-Connecting-IP")
        client_ip = cf_ip.strip() if cf_ip else (request.client.host if request.client else "unknown")
        method = request.method
        path = request.url.path

        # Execute request
        response = await call_next(request)

        latency_ms = int((time.monotonic() - start) * 1000)
        status = response.status_code

        entry = {
            "ts": time.time(),
            "ip": client_ip,
            "method": method,
            "path": path,
            "status": status,
            "latency_ms": latency_ms,
            "ua": re.sub(r"[\x00-\x1f\x7f]", "", request.headers.get("user-agent", ""))[:200],
        }

        # Add auth status and user identity for API paths (SEC-018)
        if path.startswith("/api"):
            has_auth = "Authorization" in request.headers
            entry["auth"] = has_auth
            # user_id is set by UserAuthMiddleware after authentication
            uid = getattr(request.state, "user_id", None)
            if uid:
                entry["user_id"] = uid

        # Non-blocking write via background consumer thread
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        _get_writer(self._log_path).enqueue(line)

        return response
