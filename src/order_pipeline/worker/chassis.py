"""Thin work-type plugin loop: claim (short txn) → handler HTTP → finalize (short txn)."""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import random
import socket
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from sqlalchemy.engine import Engine
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.orm import Session, sessionmaker

from order_pipeline.worker.backoff import full_jitter_delay_s
from order_pipeline.worker.claim import claim_next
from order_pipeline.worker.classify import classify_exception
from order_pipeline.worker.counters import WorkerCounters
from order_pipeline.worker.deps import DepCaps
from order_pipeline.worker.finalize import finalize_claim
from order_pipeline.worker.log import log_worker_event
from order_pipeline.worker.plugin import ClaimedWork, HandlerResult, WorkHandler
from order_pipeline.worker.settings import WorkerSettings
from order_pipeline.worker.stop_rules import CONFIRM_WORK_TYPES, confirm_deadline_exceeded

# Pool checkout timeout is a SQLAlchemyError, not an OperationalError: a saturated pool
# must back off like any other transient claim failure, never exit the loop.
_TRANSIENT_CLAIM_ERRORS = (OperationalError, InterfaceError, PoolTimeoutError)


class Worker:
    def __init__(
        self,
        settings: WorkerSettings,
        engine: Engine,
        *,
        handlers: dict[str, WorkHandler] | None = None,
        caps: DepCaps | None = None,
        now_fn: Callable[[], datetime] | None = None,
        rng: random.Random | None = None,
        worker_id: str | None = None,
        idle_s: float = 0.25,
        counters: WorkerCounters | None = None,
    ) -> None:
        self.settings = settings
        self.handlers: dict[str, WorkHandler] = dict(handlers or {})
        self.caps = caps or DepCaps(settings)
        self.now_fn = now_fn or (lambda: datetime.now(UTC))
        self.rng = rng or random.Random()
        self.worker_id = worker_id or (
            f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
        )
        self.idle_s = idle_s
        self.counters = counters or WorkerCounters()
        self._sessions: sessionmaker[Session] = sessionmaker(bind=engine, expire_on_commit=False)
        self._tasks: set[asyncio.Task[None]] = set()
        # Separate pools: finalize must never starve claim. Sharing the default
        # executor lets stuck commits fill every thread, wedging run() at its own
        # await so the backoff below can never fire.
        self._claim_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="claim")
        self._finalize_pool = ThreadPoolExecutor(
            max_workers=settings.finalize_workers, thread_name_prefix="finalize"
        )

    async def _in_pool[T](self, pool: ThreadPoolExecutor, fn: Callable[[], T]) -> T:
        return await asyncio.get_running_loop().run_in_executor(pool, fn)

    def register(self, work_type: str, handler: WorkHandler) -> None:
        self.handlers[work_type] = handler

    def claim(
        self,
        *,
        work_item_id: uuid.UUID | None = None,
        work_types: tuple[str, ...] | None = None,
    ) -> ClaimedWork | None:
        types = work_types if work_types is not None else tuple(self.handlers)
        with self._sessions.begin() as session:
            return claim_next(
                session,
                now=self.now_fn(),
                lease_s=self.settings.lease_s,
                worker_id=self.worker_id,
                work_types=types,
                work_item_id=work_item_id,
            )

    def finalize(self, claimed: ClaimedWork, result: HandlerResult) -> None:
        with self._sessions.begin() as session:
            finalize_claim(
                session,
                claimed,
                result,
                settings=self.settings,
                counters=self.counters,
                now=self.now_fn(),
                rng=self.rng,
            )

    async def process(self, claimed: ClaimedWork) -> None:
        now = self.now_fn()
        if claimed.work_type in CONFIRM_WORK_TYPES and confirm_deadline_exceeded(
            claimed.accepted_at, now, self.settings.confirm_deadline_s
        ):
            await self._finalize_off_loop(claimed, HandlerResult(outcome="unknown"))
            return

        handler = self.handlers.get(claimed.work_type)
        if handler is None:
            await self._finalize_off_loop(claimed, HandlerResult(outcome="unknown"))
            return

        try:
            result = await handler(claimed)
        except Exception as exc:
            result = HandlerResult(outcome=classify_exception(exc))

        await self._finalize_off_loop(claimed, result)

    async def _finalize_off_loop(self, claimed: ClaimedWork, result: HandlerResult) -> None:
        await self._in_pool(self._finalize_pool, functools.partial(self.finalize, claimed, result))

    async def run(self) -> None:
        in_flight = asyncio.Semaphore(self.settings.task_capacity)
        registered = tuple(self.handlers)
        claim_failures = 0
        while True:
            eligible = self.caps.eligible_types(registered)
            if not eligible:
                await asyncio.sleep(self.idle_s)
                continue
            await in_flight.acquire()
            eligible = self.caps.eligible_types(registered)
            if not eligible:
                in_flight.release()
                await asyncio.sleep(self.idle_s)
                continue
            try:
                claimed = await self._in_pool(
                    self._claim_pool, functools.partial(self.claim, work_types=eligible)
                )
            except _TRANSIENT_CLAIM_ERRORS as exc:
                in_flight.release()
                claim_failures += 1
                delay = full_jitter_delay_s(claim_failures, self.settings, self.rng)
                log_worker_event(
                    "claim_retry",
                    level=logging.WARNING,
                    worker_id=self.worker_id,
                    error=f"{type(exc).__name__}: {exc}",
                    backoff_s=round(delay, 3),
                    consecutive_failures=claim_failures,
                )
                await asyncio.sleep(delay)
                continue
            claim_failures = 0
            if claimed is None:
                in_flight.release()
                await asyncio.sleep(self.idle_s)
                continue
            self.caps.admit(claimed.work_type)
            task = asyncio.create_task(self._process_and_release(claimed, in_flight))
            self._track_process_task(claimed, task)

    def _track_process_task(self, claimed: ClaimedWork, task: asyncio.Task[None]) -> None:
        self._tasks.add(task)

        def _on_done(done: asyncio.Task[None]) -> None:
            self._tasks.discard(done)
            if done.cancelled():
                return
            exc = done.exception()
            if exc is None:
                return
            log_worker_event(
                "process_failed",
                level=logging.ERROR,
                worker_id=self.worker_id,
                work_item_id=claimed.work_item_id,
                order_id=claimed.order_id,
                work_type=claimed.work_type,
                lease_owner=claimed.lease_owner,
                error=f"{type(exc).__name__}: {exc}",
                exc_info=exc,
            )

        task.add_done_callback(_on_done)

    async def _process_and_release(
        self, claimed: ClaimedWork, in_flight: asyncio.Semaphore
    ) -> None:
        try:
            await self.process(claimed)
        finally:
            self.caps.release_admit(claimed.work_type)
            in_flight.release()
