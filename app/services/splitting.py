from collections.abc import Iterable


def equal_split(amount_paise: int, participant_ids: Iterable[int]) -> dict[int, int]:
    if type(amount_paise) is not int or amount_paise <= 0:
        raise ValueError("Amount must be a positive integer in paise")
    participants = list(participant_ids)
    if not participants or any(
        type(user_id) is not int or user_id <= 0 for user_id in participants
    ):
        raise ValueError("Participants must be nonempty positive integer IDs")
    if len(set(participants)) != len(participants):
        raise ValueError("Participants must be unique")
    base, remainder = divmod(amount_paise, len(participants))
    return {
        user_id: base + (index < remainder) for index, user_id in enumerate(sorted(participants))
    }


def validate_custom_split(
    total: int, allocations: list[tuple[int, int]], member_ids: list[int], payer_id: int
) -> dict[int, int]:
    """Validate explicit paise allocations without redistribution or database access."""
    from app.core.money import MAX_AMOUNT_PAISE

    if type(total) is not int or not 0 < total <= MAX_AMOUNT_PAISE:
        raise ValueError("Expense total exceeds supported bounds")
    if any(type(u) is not int or u <= 0 for u in member_ids):
        raise ValueError("Member IDs must be positive integers")
    members = set(member_ids)
    if type(payer_id) is not int or payer_id not in members:
        raise ValueError("Payer must be a group member")
    if not allocations:
        raise ValueError("Custom splits cannot be empty")
    result = {}
    for user_id, share in allocations:
        if type(user_id) is not int or user_id not in members or user_id in result:
            raise ValueError("Custom participants must be unique group members")
        if type(share) is not int or not 0 <= share <= MAX_AMOUNT_PAISE:
            raise ValueError("Custom shares must be nonnegative integer paise within bounds")
        result[user_id] = share
    if sum(result.values()) != total:
        raise ValueError("Custom shares must sum exactly to the expense amount")
    return dict(sorted(result.items()))
