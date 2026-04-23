"""Lead lifecycle states and allowed transitions (PRD §6)."""

from __future__ import annotations

from typing import Iterable

VALID_STATES: tuple[str, ...] = (
    "NEW",
    "ENRICHED",
    "QUALIFIED",
    "ASSIGNED_TO_ACCOUNT",
    "INVITED",
    "CONNECTED",
    "MESSAGED",
    "FOLLOW_UP_1",
    "FOLLOW_UP_2",
    "FOLLOW_UP_3",
    "REPLIED",
    "POSITIVE",
    "NEGATIVE",
    "NEUTRAL",
    "HUMAN_REVIEW",
    "FAILED",
)

_ALLOWED: dict[str, set[str]] = {
    "NEW": {"ENRICHED", "FAILED"},
    "ENRICHED": {"QUALIFIED", "ENRICHED", "FAILED"},
    "QUALIFIED": {"ASSIGNED_TO_ACCOUNT", "QUALIFIED", "FAILED"},
    "ASSIGNED_TO_ACCOUNT": {"INVITED", "ASSIGNED_TO_ACCOUNT", "FAILED"},
    "INVITED": {"CONNECTED", "MESSAGED", "INVITED", "FAILED"},
    "CONNECTED": {"MESSAGED", "CONNECTED", "FAILED"},
    "MESSAGED": {"FOLLOW_UP_1", "REPLIED", "MESSAGED", "FAILED"},
    "FOLLOW_UP_1": {"FOLLOW_UP_2", "REPLIED", "FOLLOW_UP_1", "FAILED"},
    "FOLLOW_UP_2": {"FOLLOW_UP_3", "REPLIED", "FOLLOW_UP_2", "FAILED"},
    "FOLLOW_UP_3": {"REPLIED", "FOLLOW_UP_3", "FAILED"},
    "REPLIED": {"POSITIVE", "NEGATIVE", "NEUTRAL", "HUMAN_REVIEW", "REPLIED"},
    "POSITIVE": set(),
    "NEGATIVE": set(),
    "NEUTRAL": set(),
    "HUMAN_REVIEW": set(),
    "FAILED": {"NEW", "ENRICHED", "QUALIFIED"},
}


def can_transition(from_state: str, to_state: str) -> bool:
    if from_state == to_state:
        return True
    return to_state in _ALLOWED.get(from_state, set())


def assert_transition(from_state: str, to_state: str) -> None:
    if not can_transition(from_state, to_state):
        raise ValueError(f"Invalid transition {from_state!r} -> {to_state!r}")


def is_terminal(state: str) -> bool:
    return state in {"POSITIVE", "NEGATIVE", "NEUTRAL"}
