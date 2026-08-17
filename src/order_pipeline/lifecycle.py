"""Order lifecycle transition table shared by every transition writer."""

from __future__ import annotations

CAUSE_INVALID = "invalid_transition"

# This is the executable form of the design document's lifecycle table.
# Guarded writes provide serialization; this table decides whether an arrow is
# legal before a writer is allowed to attempt that write.
LEGAL_TRANSITIONS = frozenset(
    {
        ("placed", "confirmed"),
        ("confirmed", "being_prepared"),
        ("being_prepared", "ready"),
        ("ready", "out_for_delivery"),
        ("out_for_delivery", "delivered"),
        ("placed", "cancelled"),
        ("confirmed", "cancelled"),
        ("placed", "failed"),
        ("confirmed", "failed"),
    }
)


def is_legal_transition(from_state: str, to_state: str) -> bool:
    return (from_state, to_state) in LEGAL_TRANSITIONS
