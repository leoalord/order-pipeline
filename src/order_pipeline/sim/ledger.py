"""SQLite effect ledger — independently authoritative for applied sim effects."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


@dataclass(frozen=True)
class Effect:
    idempotency_key: str
    ticket_id: str
    accepted_at: datetime
    estimated_ready_at: datetime
    payload: dict[str, Any]


CounterInsertResult = Literal["inserted", "exists", "unavailable"]


class EffectLedger:
    """One row per idempotency key. Replay must not insert a second effect."""

    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS effects (
                    idempotency_key TEXT PRIMARY KEY,
                    ticket_id TEXT NOT NULL UNIQUE,
                    accepted_at TEXT NOT NULL,
                    estimated_ready_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            self._conn.commit()

    def ping(self) -> None:
        with self._lock:
            self._conn.execute("SELECT 1").fetchone()

    def get_by_key(self, idempotency_key: str) -> Effect | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM effects WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return self._row_to_effect(row) if row is not None else None

    def get_by_ticket(self, ticket_id: str) -> Effect | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM effects WHERE ticket_id = ?",
                (ticket_id,),
            ).fetchone()
        return self._row_to_effect(row) if row is not None else None

    def insert(self, effect: Effect) -> bool:
        """Insert the effect. Returns False if the key already exists."""
        with self._lock:
            try:
                self._insert_effect(effect)
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return False
        return True

    def initialize_counters(self, defaults: Mapping[str, int]) -> None:
        """Create durable named counters without overwriting an existing value."""
        if not defaults or any(count < 0 for count in defaults.values()):
            raise ValueError("counter defaults must be non-negative")
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS counters (
                    name TEXT PRIMARY KEY,
                    count INTEGER NOT NULL CHECK (count >= 0)
                )
                """
            )
            self._conn.executemany(
                "INSERT OR IGNORE INTO counters (name, count) VALUES (?, ?)",
                sorted(defaults.items()),
            )
            self._conn.commit()

    def counter_snapshot(self, names: Sequence[str]) -> dict[str, int]:
        """Read the requested durable counters."""
        if not names:
            return {}
        placeholders = ", ".join("?" for _ in names)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT name, count FROM counters WHERE name IN ({placeholders})",  # noqa: S608
                tuple(names),
            ).fetchall()
        snapshot = {str(row["name"]): int(row["count"]) for row in rows}
        missing = set(names) - set(snapshot)
        if missing:
            raise KeyError(f"counters are not initialized: {sorted(missing)}")
        return snapshot

    def set_counter(self, name: str, count: int) -> None:
        """Set one initialized counter durably."""
        if count < 0:
            raise ValueError("counter must be non-negative")
        with self._lock:
            result = self._conn.execute(
                "UPDATE counters SET count = ? WHERE name = ?",
                (count, name),
            )
            if result.rowcount != 1:
                self._conn.rollback()
                raise KeyError(f"counter is not initialized: {name}")
            self._conn.commit()

    def insert_with_counter_decrements(
        self,
        effect: Effect,
        decrements: Mapping[str, int],
    ) -> CounterInsertResult:
        """Atomically insert an effect and consume its durable counters.

        ``BEGIN IMMEDIATE`` serializes the check/decrement across processes using
        the same SQLite ledger. Any unavailable counter or insert failure rolls
        the entire transaction back, so inventory and effect durability cannot
        diverge.
        """
        if not decrements or any(count < 1 for count in decrements.values()):
            raise ValueError("counter decrements must be positive")
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                existing = self._conn.execute(
                    "SELECT 1 FROM effects WHERE idempotency_key = ?",
                    (effect.idempotency_key,),
                ).fetchone()
                if existing is not None:
                    self._conn.rollback()
                    return "exists"
                for name, count in sorted(decrements.items()):
                    updated = self._conn.execute(
                        """
                        UPDATE counters
                        SET count = count - ?
                        WHERE name = ? AND count >= ?
                        """,
                        (count, name, count),
                    )
                    if updated.rowcount != 1:
                        self._conn.rollback()
                        return "unavailable"
                self._insert_effect(effect)
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return "exists"
            except BaseException:
                self._conn.rollback()
                raise
        return "inserted"

    def counts_by_key(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT idempotency_key, COUNT(*) AS n FROM effects GROUP BY idempotency_key"
            ).fetchall()
        return {str(row["idempotency_key"]): int(row["n"]) for row in rows}

    def list_effects(self) -> list[Effect]:
        """All effects, oldest accept first. Quote occupancy filters to not-yet-ready."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM effects ORDER BY accepted_at, ticket_id"
            ).fetchall()
        return [self._row_to_effect(row) for row in rows]

    def _insert_effect(self, effect: Effect) -> None:
        payload_json = json.dumps(effect.payload, separators=(",", ":"))
        created_at = _iso(effect.accepted_at)
        self._conn.execute(
            """
            INSERT INTO effects (
                idempotency_key, ticket_id, accepted_at,
                estimated_ready_at, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                effect.idempotency_key,
                effect.ticket_id,
                _iso(effect.accepted_at),
                _iso(effect.estimated_ready_at),
                payload_json,
                created_at,
            ),
        )

    @staticmethod
    def _row_to_effect(row: sqlite3.Row) -> Effect:
        payload = json.loads(str(row["payload_json"]))
        if not isinstance(payload, dict):
            payload = {}
        return Effect(
            idempotency_key=str(row["idempotency_key"]),
            ticket_id=str(row["ticket_id"]),
            accepted_at=_parse_iso(str(row["accepted_at"])),
            estimated_ready_at=_parse_iso(str(row["estimated_ready_at"])),
            payload=payload,
        )
