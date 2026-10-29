"""Bounded, asynchronous pose diagnostics; no images, faces, or credentials."""
from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Thread
from typing import Any


class _FileHandler(RotatingFileHandler):
    def handleError(self, record):
        # Let the worker report disk errors instead of silently losing traces.
        raise


class DiagnosticLog:
    def __init__(self, path: str, max_bytes: int = 20_000_000, backups: int = 2):
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._handler = _FileHandler(target, maxBytes=max_bytes,
                                    backupCount=backups, encoding="utf-8")
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self._queue: Queue[dict[str, Any]] = Queue(maxsize=256)
        self._stop = Event()
        self.dropped = 0
        self.error: str | None = None
        self._thread = Thread(target=self._write, name="fall-diagnostic-log", daemon=True)
        self._thread.start()

    def record(self, record: dict[str, Any]) -> None:
        # The AI thread never waits for storage; newest-frame processing wins.
        try:
            self._queue.put_nowait(record)
        except Full:
            self.dropped += 1

    def _write(self) -> None:
        try:
            while not self._stop.is_set() or not self._queue.empty():
                try:
                    record = self._queue.get(timeout=0.1)
                except Empty:
                    continue
                try:
                    line = json.dumps(record, ensure_ascii=False, allow_nan=False,
                                      separators=(",", ":"))
                    self._handler.emit(logging.LogRecord("edgefall", logging.INFO,
                                                        "", 0, line, (), None))
                    self.error = None
                except Exception as exc:
                    self.error = str(exc)
                    self.dropped += 1
                finally:
                    self._queue.task_done()
        finally:
            self._handler.close()

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)
