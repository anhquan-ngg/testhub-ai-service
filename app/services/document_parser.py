"""Bounded document intake and killable child-process orchestration."""

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

from app.schemas.document import ExtractionResult, MAX_FILE_BYTES, MAX_RESULT_BYTES

logger = logging.getLogger(__name__)


class ExtractionError(Exception):
    """Sanitized extraction failure mapped at the API boundary."""

    def __init__(self, code: str, status: int = 422) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


class ThreadedProcess:
    """Child process waited in a thread when the event loop cannot spawn subprocesses."""

    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self._process = process

    @property
    def returncode(self) -> int | None:
        return self._process.poll()

    def kill(self) -> None:
        self._process.kill()

    async def wait(self) -> int:
        return await asyncio.to_thread(self._process.wait)


class DocumentParser:
    def __init__(self, *, timeout_seconds: float = 45, max_concurrency: int = 2) -> None:
        self.timeout_seconds = timeout_seconds
        self._slots = asyncio.Semaphore(max_concurrency)

    async def extract(
        self, stream: AsyncIterator[bytes], source_type: str, include_notes: bool,
    ) -> ExtractionResult:
        """Bound upload and parsing by one deadline, releasing capacity on cancellation."""
        try:
            await asyncio.wait_for(self._slots.acquire(), 0.1)
        except TimeoutError:
            raise ExtractionError("DOCUMENT_CAPACITY_REACHED", 503) from None
        try:
            return await asyncio.wait_for(
                self._extract(stream, source_type, include_notes), self.timeout_seconds,
            )
        except TimeoutError:
            raise ExtractionError("DOCUMENT_TIMEOUT", 504) from None
        except ExtractionError:
            raise
        except Exception as exc:
            logger.warning("document_processing_failed error_type=%s", type(exc).__name__)
            raise ExtractionError("DOCUMENT_PROCESS_FAILED", 503) from None
        finally:
            self._slots.release()

    async def _extract(
        self, stream: AsyncIterator[bytes], source_type: str, include_notes: bool,
    ) -> ExtractionResult:
        with tempfile.TemporaryDirectory(prefix="testhub-document-") as folder:
            source, output = Path(folder) / "source", Path(folder) / "result.json"
            size = 0
            with source.open("wb") as handle:
                async for block in stream:
                    size += len(block)
                    if size > MAX_FILE_BYTES:
                        raise ExtractionError("DOCUMENT_LIMIT_EXCEEDED", 413)
                    handle.write(block)
            if not size:
                raise ExtractionError("DOCUMENT_INVALID")
            args = (
                sys.executable, "-m", "app.services.document_parser_worker",
                str(source), str(output), source_type, str(include_notes).lower(),
            )
            options = {
                "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                "cwd": str(Path(__file__).resolve().parents[2]),
                "env": {"PYTHONUTF8": "1", **{
                    name: os.environ[name]
                    for name in ("SystemRoot", "WINDIR", "TEMP", "TMP")
                    if name in os.environ
                }},
            }
            process: asyncio.subprocess.Process | ThreadedProcess
            cancelled = False
            try:
                process = await asyncio.create_subprocess_exec(*args, **options)
            except NotImplementedError:
                # e.g. uvicorn --reload on Windows runs a SelectorEventLoop.
                logger.info("document_subprocess_thread_fallback")
                spawn = asyncio.create_task(asyncio.to_thread(subprocess.Popen, args, **options))
                # A cancelled await cannot stop Popen's thread. Retain ownership
                # until it returns so the existing cleanup can kill/reap the child.
                while True:
                    try:
                        process = ThreadedProcess(await asyncio.shield(spawn))
                        break
                    except asyncio.CancelledError:
                        cancelled = True
            try:
                if cancelled:
                    raise asyncio.CancelledError
                await process.wait()
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            if not output.exists() or output.stat().st_size > MAX_RESULT_BYTES:
                raise ExtractionError("DOCUMENT_PROCESS_FAILED")
            raw = output.read_bytes()
            try:
                value = json.loads(raw)
                if isinstance(value, dict) and value.get("error") in {
                    "DOCUMENT_INVALID", "DOCUMENT_ENCRYPTED", "DOCUMENT_LIMIT_EXCEEDED",
                }:
                    raise ExtractionError(value["error"])
                return ExtractionResult.model_validate_json(raw)
            except (ValueError, TypeError):
                raise ExtractionError("DOCUMENT_PROCESS_FAILED") from None
