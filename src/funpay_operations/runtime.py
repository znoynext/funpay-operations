"""Local-only process and background-runtime infrastructure.

The module contains no account composition, credentials, or network client.
Its adapters are explicit async callables, allowing a disabled adapter to be a
healthy task state in development and CI.
"""

from __future__ import annotations

import asyncio
import ctypes
import inspect
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from .database import Database


class DuplicateProcessError(RuntimeError):
    """Raised when another local process holds the application lock."""


class TaskDisposition(StrEnum):
    SUCCEEDED = "succeeded"
    DISABLED = "disabled"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TaskRunResult:
    name: str
    disposition: TaskDisposition
    detail: str
    retry_delay_seconds: float = 0.0


TaskCallable = Callable[[], Awaitable[TaskDisposition | None] | TaskDisposition | None]


@dataclass(frozen=True)
class BackgroundTask:
    name: str
    operation: TaskCallable
    interval_seconds: float
    reconnect_hook: TaskCallable | None = None

    def __post_init__(self) -> None:
        if not self.name.strip() or self.interval_seconds <= 0:
            raise ValueError("background task requires a name and positive interval")


class DisabledAdapter:
    """Explicit no-op adapter used when an external integration is not configured."""

    async def run(self) -> TaskDisposition:
        return TaskDisposition.DISABLED


@dataclass
class ExponentialBackoff:
    initial_seconds: float
    maximum_seconds: float
    _failures: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.initial_seconds <= 0 or self.maximum_seconds < self.initial_seconds:
            raise ValueError("invalid exponential backoff bounds")

    def failure_delay(self, task_name: str) -> float:
        failures = self._failures.get(task_name, 0) + 1
        self._failures[task_name] = failures
        return min(self.maximum_seconds, self.initial_seconds * (2 ** (failures - 1)))

    def reset(self, task_name: str) -> None:
        self._failures.pop(task_name, None)


class SingletonProcessLock:
    """Advisory OS file lock; release is automatic on process termination."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: object | None = None

    def acquire(self) -> None:
        if self._handle is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            handle = self.path.open("a+b")
            handle.seek(0)
            if not handle.read(1):
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if "handle" in locals():
                handle.close()
            raise DuplicateProcessError("another funpay-operations process is already running") from error
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        handle = self._handle
        self._handle = None
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "SingletonProcessLock":
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()


class ProcessPriority(StrEnum):
    LOW = "low"
    BELOW_NORMAL = "below_normal"


def apply_windows_process_priority(priority: ProcessPriority = ProcessPriority.BELOW_NORMAL) -> bool:
    """Best-effort Windows priority adjustment; false means unsupported or denied."""

    if os.name != "nt":
        return False
    classes = {ProcessPriority.LOW: 0x40, ProcessPriority.BELOW_NORMAL: 0x4000}
    kernel32 = ctypes.windll.kernel32
    return bool(kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), classes[priority]))


@dataclass
class RuntimeWatchdog:
    """In-process task heartbeat monitor; it never restarts a process itself."""

    heartbeat: dict[str, float] = field(default_factory=dict)

    def record(self, task_name: str, now: float | None = None) -> None:
        self.heartbeat[task_name] = time.monotonic() if now is None else now

    def stale(self, maximum_age_seconds: float, now: float | None = None) -> tuple[str, ...]:
        current = time.monotonic() if now is None else now
        return tuple(sorted(name for name, value in self.heartbeat.items() if current - value > maximum_age_seconds))


class RecoveryStepCallable(Protocol):
    def __call__(self) -> Awaitable[TaskDisposition | None] | TaskDisposition | None: ...


@dataclass(frozen=True)
class RecoveryStep:
    name: str
    operation: RecoveryStepCallable


@dataclass(frozen=True)
class RecoveryResult:
    reason: str
    steps: tuple[TaskRunResult, ...]


class RecoveryCoordinator:
    """Runs the fixed safe recovery order after resume or network restoration."""

    ORDER = (
        "validate-external-sessions", "catch-up-messages", "refresh-market", "recalculate",
        "verify-own-prices", "restore-raise-scheduling",
    )

    def __init__(self, steps: tuple[RecoveryStep, ...], logger: logging.Logger) -> None:
        names = tuple(step.name for step in steps)
        if names != self.ORDER:
            raise ValueError("recovery steps must use the documented fixed order")
        self.steps, self.logger = steps, logger

    async def recover(self, reason: str) -> RecoveryResult:
        results: list[TaskRunResult] = []
        for step in self.steps:
            try:
                result = await _await_result(step.operation())
                disposition = TaskDisposition.DISABLED if result is TaskDisposition.DISABLED else TaskDisposition.SUCCEEDED
                results.append(TaskRunResult(step.name, disposition, "recovery step completed"))
            except Exception as error:
                self.logger.warning("Recovery step %s failed after %s: %s", step.name, reason, type(error).__name__)
                results.append(TaskRunResult(step.name, TaskDisposition.FAILED, type(error).__name__))
                break
        return RecoveryResult(reason, tuple(results))


class SleepResumeHandler:
    """Platform-neutral hook for a host's sleep/resume notifications."""

    def __init__(self, recovery: RecoveryCoordinator) -> None:
        self.recovery = recovery
        self.sleeping = False

    def on_sleep(self) -> None:
        self.sleeping = True

    async def on_resume(self) -> RecoveryResult:
        self.sleeping = False
        return await self.recovery.recover("resume")


class WindowsNetworkRecoveryHandler:
    """A host-facing abstraction; Windows event subscription remains outside this module."""

    def __init__(self, recovery: RecoveryCoordinator) -> None:
        self.recovery = recovery

    async def on_network_change(self, online: bool) -> RecoveryResult | None:
        return await self.recovery.recover("windows-network-recovered") if online else None


class SQLiteMaintenance:
    """Runs local integrity checks and SQLite backups with bounded retention."""

    def __init__(self, database: Database, backups_directory: Path, *, retention_count: int) -> None:
        if retention_count < 1:
            raise ValueError("backup retention_count must be positive")
        self.database, self.backups_directory, self.retention_count = database, backups_directory, retention_count

    def integrity_check(self) -> None:
        with self.database.session() as connection:
            rows = connection.execute("PRAGMA integrity_check").fetchall()
        if len(rows) != 1 or rows[0][0].lower() != "ok":
            raise sqlite3.DatabaseError("SQLite integrity check failed")

    def backup(self, now: datetime | None = None) -> Path:
        self.backups_directory.mkdir(parents=True, exist_ok=True)
        timestamp = (now or datetime.now(UTC)).astimezone(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        destination = self.backups_directory / f"database-{timestamp}.sqlite3"
        source = self.database.connect()
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        self._prune_backups()
        return destination

    def _prune_backups(self) -> None:
        backups = sorted(self.backups_directory.glob("database-*.sqlite3"), key=lambda item: item.stat().st_mtime, reverse=True)
        for obsolete in backups[self.retention_count:]:
            obsolete.unlink()


class BackgroundSupervisor:
    """Isolates async task failures and supports graceful cancellation."""

    def __init__(
        self, tasks: tuple[BackgroundTask, ...], *, logger: logging.Logger, backoff: ExponentialBackoff,
        watchdog: RuntimeWatchdog | None = None, recovery: RecoveryCoordinator | None = None,
    ) -> None:
        names = tuple(task.name for task in tasks)
        if len(names) != len(set(names)):
            raise ValueError("background task names must be unique")
        self.tasks, self.logger, self.backoff = tasks, logger, backoff
        self.watchdog, self.recovery = watchdog or RuntimeWatchdog(), recovery
        self._stop = asyncio.Event()
        self._active: list[asyncio.Task[None]] = []

    async def run_once(self) -> tuple[TaskRunResult, ...]:
        return tuple(await asyncio.gather(*(self._execute(task) for task in self.tasks)))

    async def run_forever(self) -> None:
        self._stop.clear()
        self._active = [asyncio.create_task(self._loop(task), name=task.name) for task in self.tasks]
        try:
            await self._stop.wait()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        self._stop.set()
        active, self._active = self._active, []
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)

    async def recover(self, reason: str) -> RecoveryResult | None:
        return await self.recovery.recover(reason) if self.recovery else None

    async def _loop(self, task: BackgroundTask) -> None:
        while not self._stop.is_set():
            result = await self._execute(task)
            delay = result.retry_delay_seconds if result.disposition is TaskDisposition.FAILED else task.interval_seconds
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                continue

    async def _execute(self, task: BackgroundTask) -> TaskRunResult:
        try:
            outcome = await _await_result(task.operation())
            disposition = TaskDisposition.DISABLED if outcome is TaskDisposition.DISABLED else TaskDisposition.SUCCEEDED
            self.backoff.reset(task.name)
            self.watchdog.record(task.name)
            return TaskRunResult(task.name, disposition, "adapter disabled" if disposition is TaskDisposition.DISABLED else "completed")
        except asyncio.CancelledError:
            return TaskRunResult(task.name, TaskDisposition.CANCELLED, "graceful shutdown")
        except Exception as error:
            delay = self.backoff.failure_delay(task.name)
            self.watchdog.record(task.name)
            self.logger.warning("Background task %s failed: %s; retry in %.2fs", task.name, type(error).__name__, delay)
            if task.reconnect_hook is not None:
                try:
                    await _await_result(task.reconnect_hook())
                except Exception as hook_error:
                    self.logger.warning("Reconnect hook %s failed: %s", task.name, type(hook_error).__name__)
            return TaskRunResult(task.name, TaskDisposition.FAILED, type(error).__name__, delay)


async def _await_result(value: Awaitable[TaskDisposition | None] | TaskDisposition | None) -> TaskDisposition | None:
    return await value if inspect.isawaitable(value) else value
