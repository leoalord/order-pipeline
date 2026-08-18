"""SQLite effect ledger — independently authoritative for applied sim effects."""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


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

    def mark_voided(self, idempotency_key: str) -> bool:
        """Set payload.voided on an existing accept so occupancy ignores it."""
        with self._lock:
            row = self._conn.execute(
                "SELECT payload_json FROM effects WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
            if row is None:
                return False
            payload = json.loads(str(row["payload_json"]))
            if not isinstance(payload, dict):
                payload = {}
            payload["voided"] = True
            self._conn.execute(
                "UPDATE effects SET payload_json = ? WHERE idempotency_key = ?",
                (json.dumps(payload, separators=(",", ":")), idempotency_key),
            )
            self._conn.commit()
        return True

    def insert(self, effect: Effect) -> bool:
        """Insert the effect. Returns False if the key already exists."""
        payload_json = json.dumps(effect.payload, separators=(",", ":"))
        created_at = _iso(effect.accepted_at)
        with self._lock:
            try:
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
                self._conn.commit()
            except sqlite3.IntegrityError:
                self._conn.rollback()
                return False
        return True

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
