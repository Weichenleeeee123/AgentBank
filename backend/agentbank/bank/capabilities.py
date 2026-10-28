from __future__ import annotations

from collections.abc import Iterable


def split_bill(total_minor: int, participant_ids: Iterable[str], initiator_id: str) -> dict[str, int]:
    participants = sorted(set(participant_ids))
    if initiator_id not in participants or total_minor < 0 or not participants:
        raise ValueError("invalid participants")
    base, remainder = divmod(total_minor, len(participants))
    allocations = {user_id: base + (index < remainder) for index, user_id in enumerate(participants)}
    allocations.pop(initiator_id)
    return allocations
