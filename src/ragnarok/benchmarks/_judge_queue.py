from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path


class JudgeQueueStorageError(RuntimeError):
    """Persistent Judge queue storage is unavailable or unsafe to continue using."""


class DiskJudgeQueue:
    """Crash-safe overflow queue; normal in-memory items are reconstructed from call logs."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        try:
            with self._connect() as db:
                db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=NORMAL;
                CREATE TABLE IF NOT EXISTS judge_queue (
                    item_key TEXT PRIMARY KEY,
                    sequence INTEGER NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    lease_owner TEXT,
                    lease_until TEXT,
                    last_error_type TEXT,
                    last_error TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS judge_queue_pending
                    ON judge_queue(status, sequence);
                """
                )
        except (OSError, sqlite3.Error) as exc:
            raise JudgeQueueStorageError(f"cannot initialize Judge queue at {path}: {exc}") from exc

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA busy_timeout=30000")
        return db

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def enqueue(self, item_key: str, sequence: int, payload: dict) -> None:
        encoded = json.dumps(payload, ensure_ascii=False)
        with self._lock, self._connect() as db:
            db.execute(
                """
                INSERT INTO judge_queue(
                    item_key, sequence, payload_json, status, updated_at
                ) VALUES (?, ?, ?, 'pending', ?)
                ON CONFLICT(item_key) DO UPDATE SET
                    sequence = excluded.sequence,
                    payload_json = excluded.payload_json,
                    status = CASE
                        WHEN judge_queue.status = 'completed' THEN 'completed'
                        ELSE 'pending'
                    END,
                    lease_owner = NULL,
                    lease_until = NULL,
                    updated_at = excluded.updated_at
                """,
                (item_key, sequence, encoded, self._now()),
            )

    def lease_next(self, owner: str, *, lease_seconds: int = 300) -> dict | None:
        now = datetime.now(timezone.utc)
        lease_until = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self._lock, self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """
                UPDATE judge_queue
                SET status = 'pending', lease_owner = NULL, lease_until = NULL, updated_at = ?
                WHERE status = 'in_progress' AND lease_until < ?
                """,
                (now.isoformat(), now.isoformat()),
            )
            row = db.execute(
                """
                SELECT item_key, sequence, payload_json, attempts
                FROM judge_queue
                WHERE status = 'pending'
                ORDER BY sequence
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                db.commit()
                return None
            db.execute(
                """
                UPDATE judge_queue
                SET status = 'in_progress', lease_owner = ?, lease_until = ?,
                    attempts = attempts + 1, updated_at = ?
                WHERE item_key = ? AND status = 'pending'
                """,
                (owner, lease_until, now.isoformat(), row[0]),
            )
            db.commit()
        return {
            "item_key": row[0],
            "sequence": row[1],
            "payload": json.loads(row[2]),
            "attempts": row[3] + 1,
            "from_disk": True,
        }

    def complete(self, item_key: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """
                UPDATE judge_queue
                SET status = 'completed', lease_owner = NULL, lease_until = NULL,
                    last_error_type = NULL, last_error = NULL, updated_at = ?
                WHERE item_key = ?
                """,
                (self._now(), item_key),
            )

    def release(self, item_key: str, error_type: str, error: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """
                UPDATE judge_queue
                SET status = 'pending', lease_owner = NULL, lease_until = NULL,
                    last_error_type = ?, last_error = ?, updated_at = ?
                WHERE item_key = ?
                """,
                (error_type, error[:1000], self._now(), item_key),
            )

    def mark_invalid(self, item_key: str, error: str) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """
                UPDATE judge_queue
                SET status = 'invalid', lease_owner = NULL, lease_until = NULL,
                    last_error_type = 'InvalidJudgment', last_error = ?, updated_at = ?
                WHERE item_key = ?
                """,
                (error[:1000], self._now(), item_key),
            )

    def counts(self) -> dict[str, int]:
        with self._lock, self._connect() as db:
            rows = db.execute(
                "SELECT status, COUNT(*) FROM judge_queue GROUP BY status"
            ).fetchall()
        counts = {str(status): int(count) for status, count in rows}
        return {
            "pending": counts.get("pending", 0),
            "in_progress": counts.get("in_progress", 0),
            "completed": counts.get("completed", 0),
            "invalid": counts.get("invalid", 0),
        }

    def pending_count(self) -> int:
        counts = self.counts()
        return counts["pending"] + counts["in_progress"]

    def reset_leases(self) -> None:
        with self._lock, self._connect() as db:
            db.execute(
                """
                UPDATE judge_queue
                SET status = 'pending', lease_owner = NULL, lease_until = NULL, updated_at = ?
                WHERE status = 'in_progress'
                """,
                (self._now(),),
            )


class BufferedJudgeQueue:
    """Ten-item RAM buffer backed by a durable FIFO overflow queue."""

    def __init__(self, disk: DiskJudgeQueue, *, memory_limit: int = 10):
        self.disk = disk
        self.memory = asyncio.Queue(maxsize=max(memory_limit, 1))
        try:
            self._disk_backlog = disk.pending_count() > 0
        except (OSError, sqlite3.Error) as exc:
            raise JudgeQueueStorageError(
                f"Judge queue storage failed at {disk.path}: {exc}"
            ) from exc
        self._lock = asyncio.Lock()

    async def _disk_call(self, function, *args):
        try:
            return await asyncio.to_thread(function, *args)
        except JudgeQueueStorageError:
            raise
        except (OSError, sqlite3.Error) as exc:
            raise JudgeQueueStorageError(
                f"Judge queue storage failed at {self.disk.path}: {exc}"
            ) from exc

    async def put(self, item: dict) -> None:
        async with self._lock:
            if not self._disk_backlog and not self.memory.full():
                self.memory.put_nowait({**item, "from_disk": False})
                return
            await self._disk_call(
                self.disk.enqueue,
                str(item["item_key"]),
                int(item["sequence"]),
                dict(item["payload"]),
            )
            self._disk_backlog = True

    async def get(self, owner: str) -> dict | None:
        try:
            return self.memory.get_nowait()
        except asyncio.QueueEmpty:
            item = await self._disk_call(self.disk.lease_next, owner)
            if item is None:
                self._disk_backlog = False
            return item

    async def complete(self, item: dict) -> None:
        if item.get("from_disk"):
            await self._disk_call(self.disk.complete, str(item["item_key"]))

    async def release(self, item: dict, error_type: str, error: str) -> None:
        if not item.get("from_disk"):
            await self._disk_call(
                self.disk.enqueue,
                str(item["item_key"]),
                int(item["sequence"]),
                dict(item["payload"]),
            )
            item = {**item, "from_disk": True}
        await self._disk_call(
            self.disk.release,
            str(item["item_key"]),
            error_type,
            error,
        )
        self._disk_backlog = True

    async def mark_invalid(self, item: dict, error: str) -> None:
        if not item.get("from_disk"):
            await self._disk_call(
                self.disk.enqueue,
                str(item["item_key"]),
                int(item["sequence"]),
                dict(item["payload"]),
            )
        await self._disk_call(self.disk.mark_invalid, str(item["item_key"]), error)

    async def flush(self) -> None:
        while True:
            try:
                item = self.memory.get_nowait()
            except asyncio.QueueEmpty:
                break
            await self._disk_call(
                self.disk.enqueue,
                str(item["item_key"]),
                int(item["sequence"]),
                dict(item["payload"]),
            )
        self._disk_backlog = (await self._disk_call(self.disk.pending_count)) > 0

    async def pending_count(self) -> int:
        return self.memory.qsize() + await self._disk_call(self.disk.pending_count)


class JudgePumpFatalError(RuntimeError):
    """A serious Judge configuration or storage error that must stop the suite."""


class AdaptiveJudgePump:
    """Consume a buffered queue without applying backpressure to Subject inference."""

    def __init__(
        self,
        queue: BufferedJudgeQueue,
        handler: Callable[[dict], Awaitable[object]],
        on_result: Callable[[dict, object], Awaitable[None]],
        classify_error: Callable[[BaseException], str],
        *,
        on_invalid: Callable[[dict, BaseException], Awaitable[None]] | None = None,
        workers: int = 2,
        outage_seconds: float = 600.0,
        not_found_seconds: float = 240.0,
        heartbeat_seconds: float = 30.0,
        retry_ceiling_seconds: float = 30.0,
        clock=time.monotonic,
    ):
        self.queue = queue
        self.handler = handler
        self.on_result = on_result
        self.classify_error = classify_error
        self.on_invalid = on_invalid
        self.maximum_workers = min(max(workers, 1), 2)
        self.active_workers = self.maximum_workers
        self.outage_seconds = outage_seconds
        self.not_found_seconds = not_found_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.retry_ceiling_seconds = retry_ceiling_seconds
        self.clock = clock
        self.producer_done = asyncio.Event()
        self.deferred = asyncio.Event()
        self.completed = asyncio.Event()
        self.fatal = asyncio.Event()
        self.fatal_error: BaseException | None = None
        self.last_success_at = self.clock()
        self.outage_started_at: float | None = None
        self.not_found_started_at: float | None = None
        self._success_streak = 0
        self._tasks: list[asyncio.Task] = []
        self._owner = uuid.uuid4().hex
        self.foreground = True
        self.in_flight = 0

    def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [
            asyncio.create_task(self._worker(index), name=f"judge-pump-{index}")
            for index in range(self.maximum_workers)
        ]

    def finish_producing(self) -> None:
        self.producer_done.set()

    def set_foreground(self, foreground: bool) -> None:
        self.foreground = foreground

    async def wait_until_releasable(self) -> str:
        self.start()
        while True:
            if self.fatal.is_set():
                raise JudgePumpFatalError(str(self.fatal_error or "Judge pump failed"))
            if self.completed.is_set():
                return "complete"
            if self.deferred.is_set():
                return "judging_deferred"
            await asyncio.sleep(0.1)

    async def wait_final(self, timeout: float) -> bool:
        self.start()
        deadline = self.clock() + max(timeout, 0.0)
        while self.clock() <= deadline:
            if self.fatal.is_set():
                raise JudgePumpFatalError(str(self.fatal_error or "Judge pump failed"))
            if self.completed.is_set():
                return True
            await asyncio.sleep(min(0.1, max(deadline - self.clock(), 0.0)))
        await self.queue.flush()
        return False

    async def close(self) -> None:
        await self.queue.flush()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.queue._disk_call(self.queue.disk.reset_leases)

    async def _worker(self, index: int) -> None:
        while True:
            if self.fatal.is_set() or self.completed.is_set():
                return
            if index >= self.active_workers:
                await asyncio.sleep(0.25)
                continue
            if not self.foreground:
                if index > 0:
                    await asyncio.sleep(self.heartbeat_seconds)
                    continue
                await asyncio.sleep(self.heartbeat_seconds)
            if self.deferred.is_set():
                if index > 0:
                    await asyncio.sleep(self.heartbeat_seconds)
                    continue
                await asyncio.sleep(self.heartbeat_seconds)
            item = await self.queue.get(f"{self._owner}:{index}")
            if item is None:
                if self.producer_done.is_set() and self.in_flight == 0:
                    self.completed.set()
                    return
                await asyncio.sleep(0.05)
                continue
            self.in_flight += 1
            try:
                result = await self.handler(item["payload"])
                await self.on_result(item, result)
                await self.queue.complete(item)
            except asyncio.CancelledError:
                await self.queue.release(item, "CancelledError", "Judge worker cancelled")
                raise
            except BaseException as exc:
                category = self.classify_error(exc)
                await self.queue.release(item, type(exc).__name__, str(exc))
                if category == "fatal":
                    self.fatal_error = exc
                    self.fatal.set()
                    return
                if category == "invalid" and int(item.get("attempts", 1)) >= 3:
                    await self.queue.mark_invalid(item, str(exc))
                    if self.on_invalid is not None:
                        await self.on_invalid(item, exc)
                    continue
                self._record_failure(category)
                delay = (
                    self.heartbeat_seconds
                    if category == "not_found"
                    else min(
                        2 ** min(int(item.get("attempts", 1)), 5),
                        self.retry_ceiling_seconds,
                    )
                )
                await asyncio.sleep(delay)
                continue
            finally:
                self.in_flight = max(self.in_flight - 1, 0)
            self._record_success()

    def _record_failure(self, category: str) -> None:
        now = self.clock()
        self._success_streak = 0
        self.active_workers = 1
        if self.outage_started_at is None:
            self.outage_started_at = now
        if category == "not_found":
            if self.not_found_started_at is None:
                self.not_found_started_at = now
            if now - self.not_found_started_at >= self.not_found_seconds:
                self.fatal_error = RuntimeError(
                    "Judge provider returned 404 for 4 minutes of 30-second heartbeats"
                )
                self.fatal.set()
                return
        else:
            self.not_found_started_at = None
        if now - self.outage_started_at >= self.outage_seconds:
            self.deferred.set()

    def _record_success(self) -> None:
        self.last_success_at = self.clock()
        self.outage_started_at = None
        self.not_found_started_at = None
        self.deferred.clear()
        self._success_streak += 1
        if self.active_workers < self.maximum_workers and self._success_streak >= 8:
            self.active_workers += 1
            self._success_streak = 0
