"""Atomic checkpoints and machine-readable progress events."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .models import RunEvent


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def atomic_write_json(path: str | Path, value: Any) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, default=str)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


def atomic_write_text(path: str | Path, value: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


class RunCheckpoint:
    def __init__(
        self,
        output_dir: str | Path,
        *,
        source_fingerprint: str,
        config: dict[str, Any],
        resume: bool = True,
        force: bool = False,
        run_id: str | None = None,
        event_sink: Callable[[RunEvent], None] | None = None,
    ) -> None:
        self.output_dir = Path(output_dir)
        self.root = self.output_dir / ".he-run"
        if force and self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.root / "run.json"
        self.events_path = self.root / "events.jsonl"
        self._lock = threading.Lock()
        self._event_sink = event_sink
        expected = fingerprint({"source": source_fingerprint, "config": config})
        existing = self.read_json(self.manifest_path)
        if existing and resume and not force:
            if existing.get("fingerprint") != expected:
                raise ValueError(
                    "Existing checkpoint does not match the input or configuration. "
                    "Use --force to start a new run."
                )
            self.manifest = existing
            self.run_id = str(existing["run_id"])
        else:
            self.run_id = run_id or uuid.uuid4().hex
            self.manifest = {
                "run_id": self.run_id,
                "fingerprint": expected,
                "source_fingerprint": source_fingerprint,
                "config": config,
                "status": "created",
                "stage": "created",
                "created_at": _now(),
                "updated_at": _now(),
            }
            atomic_write_json(self.manifest_path, self.manifest)

    @staticmethod
    def read_json(path: str | Path) -> dict[str, Any] | None:
        target = Path(path)
        if not target.exists():
            return None
        with target.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return value if isinstance(value, dict) else None

    def update(
        self, *, status: str | None = None, stage: str | None = None, **details: Any
    ) -> None:
        if status is not None:
            self.manifest["status"] = status
        if stage is not None:
            self.manifest["stage"] = stage
        self.manifest.update(details)
        self.manifest["updated_at"] = _now()
        atomic_write_json(self.manifest_path, self.manifest)

    def emit(
        self,
        stage: str,
        status: str,
        message: str,
        *,
        chunk_id: str | None = None,
        current: int | None = None,
        total: int | None = None,
        attempt: int | None = None,
        **details: Any,
    ) -> None:
        event = RunEvent(
            timestamp=_now(),
            run_id=self.run_id,
            stage=stage,
            status=status,
            message=message,
            chunk_id=chunk_id,
            current=current,
            total=total,
            attempt=attempt,
            details=details,
        )
        line = event.model_dump_json()
        with self._lock:
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()
        if self._event_sink is not None:
            self._event_sink(event)
        prefix = f"[{stage}]"
        progress = f"[{current}/{total}]" if current is not None and total else ""
        retry = f"[attempt {attempt}]" if attempt else ""
        print(f"{prefix}{progress}{retry} {message}", flush=True)

    def chunk_dir(self, chunk_id: str) -> Path:
        path = self.root / "chunks" / chunk_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def chunk_completed(self, chunk_id: str) -> bool:
        status = self.read_json(self.chunk_dir(chunk_id) / "status.json") or {}
        return (
            status.get("status") == "completed"
            and (self.chunk_dir(chunk_id) / "graph.json").exists()
        )

    @contextmanager
    def heartbeat(
        self,
        stage: str,
        message: str,
        *,
        interval: int = 30,
        chunk_id: str | None = None,
    ) -> Iterator[None]:
        stopped = threading.Event()

        def beat() -> None:
            started = time.monotonic()
            while not stopped.wait(max(1, interval)):
                self.emit(
                    stage,
                    "heartbeat",
                    message,
                    chunk_id=chunk_id,
                    elapsed_seconds=round(time.monotonic() - started),
                )

        thread = threading.Thread(target=beat, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stopped.set()
            thread.join(timeout=1)
