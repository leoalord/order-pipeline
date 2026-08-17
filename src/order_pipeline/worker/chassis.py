"""Thin work-type plugin loop: claim (short txn) → handler HTTP → finalize (short txn)."""

from __future__ import annotations

import asyncio
import os
import random
import socket
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from order_pipeline.worker.claim import claim_next
from order_pipeline.worker.classify import classify_exception
from order_pipeline.worker.counters import WorkerCounters
from order_pipeline.worker.deps import DepCaps
from order_pipeline.worker.finalize import finalize_claim
from order_pipeline.worker.plugin import ClaimedWork, HandlerResult, WorkHandler
from order_pipeline.worker.settings import WorkerSettings
from order_pipeline.worker.stop_rules import CONFIRM_WORK_TYPES, confirm_deadline_exceeded


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
            self.finalize(claimed, HandlerResult(outcome="unknown"))
            return

        handler = self.handlers.get(claimed.work_type)
        if handler is None:
            self.finalize(claimed, HandlerResult(outcome="unknown"))
            return

        try:
            result = await handler(claimed)
        except Exception as exc:
            result = HandlerResult(outcome=classify_exception(exc))

        self.finalize(claimed, result)

    async def run(self) -> None:
        in_flight = asyncio.Semaphore(self.settings.task_capacity)
        registered = tuple(self.handlers)
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
            claimed = await asyncio.to_thread(self.claim, work_types=eligible)
            if claimed is None:
                in_flight.release()
                await asyncio.sleep(self.idle_s)
                continue
            self.caps.admit(claimed.work_type)
            asyncio.create_task(self._process_and_release(claimed, in_flight))

    async def _process_and_release(
        self, claimed: ClaimedWork, in_flight: asyncio.Semaphore
    ) -> None:
        try:
            await self.process(claimed)
        finally:
            self.caps.release_admit(claimed.work_type)
            in_flight.release()
