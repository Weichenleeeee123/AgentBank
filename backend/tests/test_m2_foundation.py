from datetime import datetime, timezone

import pytest

from agentbank.clock import BusinessClock
from agentbank.errors import DomainError
from agentbank.planning.validator import PlanValidator
from agentbank.text.parse import parse_amount_minor, parse_date_expression
from agentbank.bank.capabilities import split_bill


def test_chinese_amount_parser_normalizes_colloquial_amounts() -> None:
    assert parse_amount_minor("两百五") == 25_000
    assert parse_amount_minor("三千二") == 320_000
    assert parse_amount_minor("1.25 元") == 125


def test_chinese_amount_parser_marks_uncertain_phrasing() -> None:
    with pytest.raises(DomainError, match="AMBIGUOUS_AMOUNT"):
        parse_amount_minor("五百多")


def test_date_parser_uses_explicit_reference_date() -> None:
    reference = datetime(2026, 10, 28, tzinfo=timezone.utc)
    assert parse_date_expression("下周五", reference).isoformat() == "2026-11-06"
    assert parse_date_expression("月底", reference).isoformat() == "2026-10-31"


def test_plan_validator_rejects_cycles_and_untrusted_write_args() -> None:
    validator = PlanValidator({"read": {"kind": "query"}, "write": {"kind": "command"}})
    with pytest.raises(DomainError, match="PLAN_CYCLE"):
        validator.validate([
            {"id": "a", "tool": "read", "args": {}, "depends_on": ["b"], "idempotency_key": "a"},
            {"id": "b", "tool": "write", "args": {}, "depends_on": ["a"], "idempotency_key": "b", "compensation": "undo"},
        ])
    with pytest.raises(DomainError, match="UNTRUSTED_WRITE_ARGUMENT"):
        validator.validate([
            {"id": "a", "tool": "write", "args": {"memo": {"source": "untrusted_field"}}, "idempotency_key": "a", "compensation": "undo"},
        ])


def test_business_clock_advances_business_time_only() -> None:
    clock = BusinessClock(datetime(2026, 10, 28, 8, 0, tzinfo=timezone.utc))
    assert clock.now().isoformat().startswith("2026-10-28")
    clock.advance_days(2)
    assert clock.business_date() == "2026-10-30"
    assert clock.security_now().isoformat().startswith("2026-10-28")


def test_split_bill_preserves_cents_and_excludes_initiator() -> None:
    shares = split_bill(10_000, ["u2", "u1", "u3"], "u1")
    assert shares == {"u2": 3_333, "u3": 3_333}
    assert sum(shares.values()) == 6_666
