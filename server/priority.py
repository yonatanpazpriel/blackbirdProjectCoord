"""Map the persona's string priority values to Linear's integer enum.

Linear's `IssuePriorityValue`: 0 = No priority, 1 = Urgent, 2 = High,
3 = Medium, 4 = Low.
"""

from __future__ import annotations

PRIORITY_TO_LINEAR: dict[str, int] = {
    "no_priority": 0,
    "urgent": 1,
    "high": 2,
    "medium": 3,
    "low": 4,
}


def to_linear_priority(value: str) -> int:
    key = (value or "").strip().lower()
    if key not in PRIORITY_TO_LINEAR:
        valid = ", ".join(PRIORITY_TO_LINEAR.keys())
        raise ValueError(f"invalid priority {value!r}; expected one of: {valid}")
    return PRIORITY_TO_LINEAR[key]
