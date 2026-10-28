from agentbank.policy.engine import PolicyContext, decide


def test_balance_is_green() -> None:
    result = decide("account.balance", PolicyContext())
    assert result.tier_display == "G"
    assert result.assurance.value == "none"


def test_small_transfer_is_yellow() -> None:
    result = decide("transfer.create", PolicyContext(projected_today_minor=20_000))
    assert result.tier_display == "Y"
    assert result.assurance.value == "confirm"


def test_daily_limit_upgrade_is_red() -> None:
    result = decide("transfer.create", PolicyContext(projected_today_minor=100_001))
    assert result.tier_display == "R"
    assert result.assurance.value == "mfa"


def test_new_recipient_is_red_even_when_amount_is_small() -> None:
    result = decide(
        "transfer.create",
        PolicyContext(projected_today_minor=20_000, new_recipient=True),
    )
    assert result.tier_display == "R"
    assert "U05" in result.reason_codes
